#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""서울 열린데이터광장 Open API를 MCP 도구로 내놓는 서버. 표준 라이브러리만으로 돈다.

    python3 mcp_server.py                       # stdio MCP 서버 (클라이언트가 띄운다)
    python3 mcp_server.py --list                # 도구 목록과 입력
    python3 mcp_server.py --help place_now      # 도구 하나의 설명과 입력 상세
    python3 mcp_server.py --call place_now '{"place": "광화문·덕수궁"}'
    python3 mcp_server.py --setup               # 터미널에서 인증키 저장 (키가 대화에 남지 않는다)
    python3 mcp_server.py --selftest [--full]   # 실제 서버를 불러 전체 도구 점검

설계 원칙 (근거는 DESIGN.md)
- 틀린 답을 정상처럼 돌려주지 않는다. 체험 모드가 다른 장소를 주거나, 필터가 먹지 않았거나, 응답을 덜어 냈으면 그 사실을 말한다.
- 실패를 빈 결과로 답하지 않는다. 오류에는 사람이 읽을 문장(message)과 다음 행동(next)을 담는다.
- 원본 필드명과 값을 바꾸지 않는다. 뜻과 단위는 명세의 한글 설명을 fields로 붙여 준다.
- 사용자에게 전해야 할 말은 notices에 문장으로 담는다.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog                                                   # noqa: E402
import places                                                    # noqa: E402
import stations                                                  # noqa: E402
import seoul_client as sc                                        # noqa: E402
from seoul_client import SeoulError                              # noqa: E402

SERVER = {"name": "seoul-data", "version": "1.0.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_CHARS = 40_000     # 응답 JSON 글자 수 상한. 클라이언트가 큰 응답을 자르기 전에 서버가 먼저 줄이고 알리려는 값이다.
                       # dart 서버에서 써 온 경험값이고, 4만 자가 몇 토큰인지는 재지 않았다.
CELL_MAX = 500         # 셀 하나의 글자 수 상한. 구청 공고의 본문처럼 한 셀이 수 KB인 데이터가 있다.
DEADLINE = 20          # 도구 하나가 쓸 수 있는 시간(초). stdio는 한 줄씩 처리하므로 오래 붙잡지 않는다.
SCAN_DEFAULT, SCAN_MAX = 10000, 50000     # 한 번에 훑는 행 수. 1,000행 한 장에 0.2~0.6초다. 열이 많은 데이터는 3만 행 안팎에서
                                           # 기한 20초에 걸리고, 그때는 받은 데까지 계산해 next_scan_offset과 함께 돌려준다
SUBWAY_DAILY = 1000    # 실시간 지하철 Open API의 하루 호출 한도
CITY, CITY_PPLTN, SUBWAY, BIKE = "citydata", "citydata_ppltn", "realtimeStationArrival", "bikeList"

SETUP_CMD = f'python3 "{Path(__file__).resolve()}" --setup'
KEY_NEXT = ("사용자에게 무료 인증키가 필요하다고 알리세요. https://data.seoul.go.kr 에 로그인해 '인증키 신청'을 누르면 바로 나옵니다. "
            "키를 알려 주면 저장해 드린다고 전하세요(key_setup). 대화에 키를 적기 꺼리면 터미널에 다음 명령을 붙여 넣으라고 안내하세요. "
            f"[{SETUP_CMD}]")
SAMPLE_NOTICE = "체험 모드입니다(인증키 없음). 처음 5행만 볼 수 있고 최신순이 아닐 수 있습니다. 무료 인증키를 넣으면 전체를 봅니다."


class ToolError(Exception):
    """모델이 읽고 고쳐 부를 수 있는 오류. message는 사람에게 전할 문장, next는 다음 행동."""

    def __init__(self, code: str, message: str, next: str = "", **data: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.next, self.data = code, message, next, data


# ── 접속 ───────────────────────────────────────────────────────────────────

_client: sc.Client | None = None
site_fetch: Callable[..., bytes] = sc.http_fetch      # 테스트가 바꿔 끼운다


def client() -> sc.Client:
    global _client
    if _client is None:
        _client = sc.Client()
    return _client


def _refresh_keys() -> None:
    """서버가 뜬 뒤에 터미널(--setup)로 키를 넣어도 다음 호출부터 쓰이게, 체험 모드인 키는 호출마다 다시 찾는다."""
    c = _client
    if c is None or c._fetch is not sc.http_fetch:        # 테스트가 끼운 가짜 접속은 건드리지 않는다
        return
    key = sc.find_key("SEOUL_API_KEY") or sc.SAMPLE_KEY
    subway = sc.find_key("SEOUL_SUBWAY_API_KEY") or sc.SAMPLE_KEY
    if (key, subway) != (c.key, c.subway_key):            # 틀린 키를 고쳐 넣은 경우도 여기서 잡힌다
        c.key, c.subway_key = key, subway
        c.cache = sc.Cache()                              # 옛 키로 받은 응답(체험 모드의 다른 장소 등)을 버린다


def set_client(c: sc.Client | None) -> None:
    global _client
    _client = c


def _site(url: str, data: dict | None = None) -> bytes:
    """열린데이터광장 사이트 호출. API 호출과 같은 기한을 쓴다."""
    return site_fetch(url, data=data, deadline=client().deadline)


def _spec(dataset: str) -> dict:
    return catalog.get_spec(dataset, fetch=_site)


def _labels(dataset: str, keys: set[str]) -> dict:
    """필드명 → 명세의 한글 설명. 명세를 못 읽으면 조용히 비운다 (설명은 덤이지 답의 일부가 아니다)."""
    try:
        spec = _spec(dataset)
    except SeoulError:
        return {}
    return {f["name"]: f["description"] for f in spec["fields"] if f["name"] in keys and f["description"]}


# ── 값 다루기 ──────────────────────────────────────────────────────────────

def clean(v: Any) -> Any:
    """앞뒤 공백을 떼고 긴 셀을 줄인다. 숫자로 바꾸지 않는다 (지번 '0016' 같은 값이 망가진다)."""
    if isinstance(v, str):
        v = v.strip()
        return v if len(v) <= CELL_MAX else v[:CELL_MAX] + f"…(+{len(v) - CELL_MAX}자)"
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def _blank(v: Any) -> bool:
    return v is None or v == [] or v == {} or (isinstance(v, str) and v.strip() in ("", "None", "null"))


def prune(d: dict, drop: tuple = ()) -> dict:
    return {k: v for k, v in d.items() if k not in drop and not _blank(v)}


def num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def walk_keys(obj: Any, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            walk_keys(v, out)
    elif isinstance(obj, list):
        for x in obj:
            walk_keys(x, out)
    return out


def hoist_common(rows: list) -> tuple[dict, list]:
    """모든 행에 같은 값은 common으로 올린다. 토큰을 아낀다."""
    if len(rows) < 3 or not all(isinstance(r, dict) for r in rows):
        return {}, rows
    common = {k: v for k, v in rows[0].items()
              if not isinstance(v, (dict, list)) and all(k in r and r[k] == v for r in rows[1:])}
    if not common or len(common) == len(rows[0]):
        return {}, rows
    return common, [{k: v for k, v in r.items() if k not in common} for r in rows]


def now_kst() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).replace(tzinfo=None)


def age_sec(stamp: str) -> int | None:
    """'2026-11-14 20:58:20' 꼴의 서울 시각이 지금부터 몇 초 전인지."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M"):
        try:
            return max(int((now_kst() - _dt.datetime.strptime(stamp.strip(), fmt)).total_seconds()), 0)
        except (ValueError, AttributeError):
            continue
    return None


# ── dataset_search ─────────────────────────────────────────────────────────

def dataset_search(a: dict) -> dict:
    query = (a.get("query") or "").strip()
    api_only, limit = a.get("api_only", True), min(max(a.get("limit", 10), 1), 30)
    if not query:
        return {"meta": {"total": len(catalog.FEATURED), "returned": len(catalog.FEATURED), "kind": "featured"},
                "notices": ["검색어가 없어 호출을 확인해 둔 대표 데이터셋을 보여 줍니다. 다른 데이터는 query에 한글 키워드를 넣어 찾으세요."],
                "rows": catalog.FEATURED}
    category, page = (a.get("category") or "").strip(), max(a.get("page", 1), 1)
    rows, skipped, total, pages = [], 0, 0, 0
    while len(rows) < limit and pages < 3:
        res = catalog.search(query, page + pages, fetch=_site)
        total, pages = res["total"], pages + 1
        for it in res["items"]:
            if category and category not in it["category"]:
                continue
            if api_only and not it["has_api"]:
                skipped += 1
                continue
            rows.append(prune(it, drop=("types",) if it["has_api"] else ()))   # 받은 쪽은 자르지 않는다. 자르면 next_page로 못 찾는다
        if (page + pages - 1) * catalog.PAGE_SIZE >= total:
            break
    notices = []
    if skipped:
        notices.append(f"Open API가 없는 {skipped}건은 뺐습니다. 통계 데이터셋(DT로 시작)은 시트와 차트로만 제공됩니다. "
                       "찾는 것이 인구, 가구, 사업체 같은 통계라면 빠진 쪽에 있을 수 있습니다. "
                       "api_only를 false로 다시 찾아 열람 주소(url)를 사용자에게 안내하세요.")
    if not rows:
        notices.append("맞는 데이터셋이 없습니다. 더 짧은 한글 키워드로 다시 찾아보세요 (예: '주차', '실거래', '도서관').")
    last = page + pages - 1
    if category or skipped:
        notices.append(f"meta.total({total:,})은 검색어에 맞는 전체 건수입니다. 분류와 Open API 유무로 거르기 전의 수입니다.")
    return {"meta": {"query": query, "total": total, "returned": len(rows), "page": page,
                     "next_page": last + 1 if last * catalog.PAGE_SIZE < total else None},
            "notices": notices, "rows": rows}


# ── dataset_describe ───────────────────────────────────────────────────────

def _example(spec: dict) -> dict:
    """명세의 샘플 URL에서 필수 인자 값을 가져와 바로 부를 수 있는 예시를 만든다."""
    filters = {}
    for s in spec["samples"]:
        m = re.search(re.escape(spec["service"]) + r"/\d+/\d+/?(.*)$", s["url"])
        if m:
            for p, val in zip(spec["params"], [x for x in m.group(1).split("/")]):
                if val.strip():
                    filters[p["name"]] = val.strip()
            break
    args: dict = {"dataset": spec["dataset"], "limit": 5}
    if filters:
        args["filters"] = filters
    return {"tool": "dataset_query", "arguments": args}


def _sample_row(spec: dict) -> dict | None:
    """실제 행 하나. 코드 필드에 어떤 값이 들어 있는지(영업상태 '영업/정상' 등) 명세만으로는 알 수 없어서 싣는다."""
    if spec["host"] == "subway":                          # 지하철 서버는 하루 1,000건 한도가 있다. 예시 행 때문에 쓰지 않는다
        return None
    try:
        args = _example(spec)["arguments"].get("filters") or {}
        c = client()
        page = c.call(spec["service"], spec["first_index"], spec["first_index"] + (1 if spec["first_index"] else 0),
                      catalog.order_params(spec, args), subway=spec["host"] == "subway")
        return prune(clean(page.rows[0])) if page.rows else None
    except (SeoulError, ToolError, IndexError):
        return None                                       # 예시는 덤이다. 못 받아도 명세는 돌려준다


def dataset_describe(a: dict) -> dict:
    spec = _spec(a["dataset"])
    notices = []
    if spec.get("stale") == "site_changed":
        notices.append("열린데이터광장의 명세 화면 구조가 바뀌어 새로 읽지 못했습니다. 이전에 저장해 둔 명세를 보여 주니 "
                       "실제와 다를 수 있습니다. 이 사실을 사용자에게 알려 주세요.")
    elif spec.get("stale"):
        notices.append("열린데이터광장에 연결되지 않아 이전에 저장해 둔 명세를 보여 줍니다.")
    hit = _dedicated(spec["service"])
    if hit:
        notices.append(f"이 데이터는 {hit[0]} 도구로 부르세요. dataset_query로는 부를 수 없습니다.")
    if spec["host"] == "subway":
        notices.append("실시간 지하철 계열입니다. 별도 인증키(SEOUL_SUBWAY_API_KEY)를 쓰고 하루 1,000건까지 부를 수 있습니다.")
    return {"meta": {"dataset": spec["dataset"], "service": spec["service"], "source_url": spec["source_url"],
                     "host": spec["host"], "max_rows_per_call": sc.PAGE_MAX},
            "notices": notices,
            "filters": [{"name": p["name"], "required": p["required"], "description": p["description"],
                         **({"values": p["values"]} if p["values"] else {})} for p in spec["params"]],
            "filter_note": ("필터는 경로의 위치 인자라서 대부분 정확히 일치하는 값만 받습니다. "
                            "범위나 부분 일치는 dataset_query의 where를 쓰세요. 값에 '/'는 넣을 수 없습니다."),
            "fields": spec["fields"],
            "sample_row": None if hit else _sample_row(spec),
            "example": {"tool": hit[0], "arguments": json.loads(hit[1])} if hit else _example(spec)}


# ── dataset_query ──────────────────────────────────────────────────────────

SUBWAY_SERVICES = {"realtimeposition", "realtimestationarrival", "realtimestationarrival/all"}   # 지하철 서버로 가는 서비스

# 범용 도구로 부르면 체험 모드 확인과 크기 줄이기를 건너뛰게 되는 서비스. 전용 도구로 돌려보낸다.
DEDICATED = {"citydata": ("place_now", '{"place": "광화문·덕수궁"}'), "citydata_ppltn": ("place_now", '{"place": "광화문·덕수궁", "sections": ["population"]}'),
             "citydata_cmrcl": ("place_now", '{"place": "광화문·덕수궁", "sections": ["commercial"]}'),
             "realtimestationarrival": ("subway_arrivals", '{"station": "서울"}')}


def _dedicated(service: str) -> tuple[str, str] | None:
    """citydata 계열은 영문판(citydata_eng 등)까지 모두 전용 도구로 보낸다. 이름 목록만으로 막으면 변형이 새어 나간다."""
    name = service.strip().lower()
    if name.startswith("citydata"):
        return DEDICATED.get(name, DEDICATED["citydata"])
    return DEDICATED.get(name)


WHERE_RE = re.compile(r"^\s*([^\s<>=!~]+)\s*(>=|<=|!=|=|>|<|~)\s*(.*?)\s*$")
METRIC_FNS = {"avg": statistics.fmean, "sum": math.fsum, "min": min, "max": max, "median": statistics.median}
GROUP_MAX = 100
_DATE_WANT = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_LEADING_ZERO = re.compile(r"^0\d")


def _parse_where(conds: list) -> list[tuple[str, str, str]]:
    out = []
    for c in conds:
        m = WHERE_RE.match(str(c))
        if not m:
            raise ToolError("BAD_INPUT", f"where 조건을 읽지 못했습니다: {c}",
                            "'필드 연산자 값' 꼴로 쓰세요. 연산자는 = != > >= < <= ~(포함). 예: \"CTRT_DAY>=20260801\", \"BLDG_USG=아파트\"")
        out.append((m.group(1), m.group(2), m.group(3).strip("'\"")))
    return out


def _pair(have: str, want: str) -> tuple:
    """비교할 두 값을 같은 모양으로 맞춘다. 날짜는 조건의 길이만큼만 본다.

    '2026-11-15 00:00:00.0' <= '2026-11-15'가 거짓이 되거나, 202611142000 >= 20261114가 엉뚱하게 참이 되는 것을 막는다.
    """
    dw, dh = _date_digits(want), _date_digits(have)
    if dw and dh and len(dh) >= len(dw):                  # 2026-11-14와 20261114처럼 꼴이 달라도 같은 날짜로 본다
        return dh[:len(dw)], dw
    x, y = num(have), num(want)
    if x is not None and y is not None and not _LEADING_ZERO.match(have) and not _LEADING_ZERO.match(want):
        return x, y                                       # '0016' 같은 코드는 숫자로 보지 않는다
    return have, want


_DATE_SHAPES = (re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?(?:[ T].*)?$"),   # 2026-11-14 00:00:00.0
                re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:(?:0[1-9]|[12]\d|3[01])(?:\d{4}(?:\d{2})?)?)?$"))  # 202611, 20261114, 202611142000


_DATE_LOOSE = re.compile(r"^((?:19|20)\d{2})\s*[-./]\s*(\d{1,2})(?:\s*[-./]\s*(\d{1,2}))?\.?((?:[ T].*)?)$")


def _date_digits(text: str) -> str | None:
    """날짜로 읽히는 값이면 숫자만 남겨 돌려준다 (시각이 붙어 있으면 그것까지). 아니면 None.

    2026-11-14, 2026.11.14, 2026/11/14, 2026-8-1, 20261114, 202611142000을 모두 같은 꼴로 맞춘다.
    """
    t = text.strip()
    m_ = _DATE_LOOSE.match(t)
    if m_ and 1 <= int(m_.group(2)) <= 12 and (m_.group(3) is None or 1 <= int(m_.group(3)) <= 31):
        t = m_.group(1) + "-" + m_.group(2).zfill(2) + ("-" + m_.group(3).zfill(2) if m_.group(3) else "") + m_.group(4)
    if any(p.match(t) for p in _DATE_SHAPES):
        return re.sub(r"\D", "", t)
    return None


def _match(row: dict, conds: list[tuple[str, str, str]]) -> bool:
    for f, op, want in conds:
        have = "" if row.get(f) is None else str(row.get(f)).strip()
        if op == "~":
            if want.lower() not in have.lower():
                return False
            continue
        x, y = _pair(have, want)
        if op in ("=", "!="):
            if (x == y) != (op == "="):
                return False
            continue
        if have == "" or type(x) is not type(y):          # 빈 값이나 숫자와 글자를 크기로 견주지 않는다. 범위 밖으로 친다
            return False
        if not {">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op]:
            return False
    return True


def _aggregate(rows: list, group_by: list, metrics: list) -> tuple[list, list]:
    plan, notes = [], []
    for m in metrics or ["count"]:
        if m == "count":
            continue
        fn, _, fld = str(m).partition(":")
        if fn not in METRIC_FNS or not fld:
            raise ToolError("BAD_INPUT", f"metrics를 읽지 못했습니다: {m}",
                            "'count' 또는 '함수:필드' 꼴로 쓰세요. 함수는 avg, sum, min, max, median. 예: \"avg:THING_AMT\"")
        plan.append((fn, fld.strip()))
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault(tuple(str(r.get(g, "")).strip() for g in group_by), []).append(r)
    out = []
    for key, members in groups.items():
        item: dict = dict(zip(group_by, key))
        item["count"] = len(members)
        for fn, fld in plan:
            vals = [v for v in (num(r.get(fld)) for r in members) if v is not None]
            if not vals and fn in ("min", "max"):         # 날짜 같은 글자 값의 처음과 끝
                texts = [str(r[fld]).strip() for r in members if not _blank(r.get(fld))]
                item[f"{fn}_{fld}"] = (min if fn == "min" else max)(texts) if texts else None
                continue
            item[f"{fn}_{fld}"] = round(METRIC_FNS[fn](vals), 2) if vals else None
            if len(vals) < len(members):
                notes.append(f"{fld} 값이 숫자가 아닌 행 {len(members) - len(vals)}건은 {fn} 계산에서 뺐습니다.")
        out.append(item)
    out.sort(key=lambda x: -x["count"])
    return out, sorted(set(notes))


def _sort(rows: list, spec: str) -> list:
    """'-avg_THING_AMT'처럼 앞에 -를 붙이면 큰 것부터. 숫자로 읽히면 숫자로, 아니면 글자로 견준다. 빈 값은 맨 뒤."""
    desc, key = spec.startswith("-"), spec.lstrip("+-").strip()
    if rows and not any(key in r for r in rows):
        raise ToolError("BAD_INPUT", f"sort에 쓴 필드가 결과에 없습니다: {key}",
                        "쓸 수 있는 필드: " + ", ".join(sorted(walk_keys(rows[:20]))))
    have = [r for r in rows if not _blank(r.get(key))]
    nums = [r for r in have if num(r[key]) is not None]
    if len(nums) >= len(have) / 2:                        # 대부분 숫자면 숫자로 견주고, 숫자가 아닌 값은 뒤로 보낸다
        nums.sort(key=lambda r: num(r[key]), reverse=desc)
        have = nums + [r for r in have if num(r[key]) is None]
    else:
        have.sort(key=lambda r: str(r[key]), reverse=desc)
    return have + [r for r in rows if _blank(r.get(key))]


def dataset_query(a: dict) -> dict:
    dataset, service = (a.get("dataset") or "").strip(), (a.get("service") or "").strip()
    if bool(dataset) == bool(service):
        raise ToolError("BAD_INPUT", "dataset과 service 중 하나만 넣어야 합니다.",
                        "보통은 dataset에 OA-번호를 넣고 filters로 조건을 줍니다. 번호는 dataset_search로 찾습니다.")
    filters, path_args = a.get("filters") or {}, a.get("path_args") or []
    spec = None
    if dataset:
        if path_args:
            raise ToolError("BAD_INPUT", "dataset을 쓸 때는 path_args가 아니라 filters(이름: 값)로 조건을 주세요.",
                            "예: {\"dataset\": \"OA-21275\", \"filters\": {\"CGG_NM\": \"마포구\"}}")
        spec = _spec(dataset)
        service = spec["service"]
        params = catalog.order_params(spec, filters)
    else:
        if filters:
            raise ToolError("BAD_INPUT", "filters는 dataset(OA-번호)과 함께만 쓸 수 있습니다.",
                            "service를 직접 줄 때는 path_args에 인자를 순서대로 넣으세요.")
        params = list(path_args)
        if service.lower() in SUBWAY_SERVICES:   # 서비스명만 줘도 지하철 서비스는 지하철 서버로 보낸다
            spec = {"service": service, "host": "subway", "first_index": 0, "params": [], "source_url": None}
    hit = _dedicated(service.split("/")[0])
    if hit:
        raise ToolError("USE_TOOL", f"이 데이터({service})는 dataset_query가 아니라 {hit[0]} 도구로 봐야 합니다.",
                        f"{hit[0]} {hit[1]} 처럼 부르세요. 그 도구가 장소·역 이름을 맞추고, 체험 모드에서 다른 대상의 데이터가 "
                        "오는 것을 막고, 큰 응답을 필요한 부분만 줄여 줍니다.", tool=hit[0])
    for v in params:
        if v is not None and "/" in str(v):
            raise ToolError("BAD_INPUT", f"필터 값에 '/'를 넣을 수 없습니다: {v}",
                            "인자가 경로의 위치로 전달되어 '/'가 있으면 뒤 인자가 밀립니다. "
                            f"'/' 앞부분만 넣어 보세요 (예: {str(v).split('/')[0]}). 앞부분 일치로 동작하는 필터가 많습니다. "
                            "안 되면 그 필터를 빼고 where에 \"필드~일부글자\"로 거르세요.")
    offset = max(a.get("offset", 0), 0)
    where, group_by, metrics = _parse_where(a.get("where") or []), a.get("group_by") or [], a.get("metrics") or []
    sort = (a.get("sort") or "").strip()
    grouping = bool(group_by or metrics)
    scanning = bool(where or grouping or sort)
    limit = min(max(a.get("limit", GROUP_MAX if grouping else 20), 1), sc.PAGE_MAX)
    c, notices = client(), []
    subway = bool(spec and spec["host"] == "subway")
    key_mode = c.subway_key_mode if subway else c.key_mode
    meta: dict = {"dataset": dataset or None, "service": service}

    if subway:                                   # 호스트와 키가 다르다. 한 장(최대 1,000행)만 받는다
        # 실측: /0/N과 /1/N이 같은 1~N행을 준다. 그래서 offset+1부터 offset+limit까지를 부른다
        want = sc.PAGE_MAX if scanning else (sc.SAMPLE_MAX if key_mode == "sample" else limit)
        start = 1 if scanning else offset + 1
        if key_mode == "sample" and (scanning or offset):
            raise ToolError("SAMPLE_LIMIT", "실시간 지하철 인증키가 없어 처음 5행만 볼 수 있습니다.", KEY_NEXT)
        page = c.call(service, start, start + want - 1, params, subway=True)
        total = page.total if page.total is not None else len(page.rows)
        got = {"total": total, "rows": page.rows, "partial": False, "dups": 0, "missing": 0,
               "sample_capped": key_mode == "sample" and limit > want,
               "ended": start - 1 + len(page.rows) >= total or len(page.rows) < want}
        scan_offset = 0
    elif scanning:
        if key_mode == "sample":
            raise ToolError("SAMPLE_LIMIT", "지금은 체험 모드라 처음 5행만 받을 수 있어 조건 검색(where), 정렬, 집계를 할 수 없습니다.",
                            KEY_NEXT + " 체험 모드에서는 where, sort, group_by, metrics를 빼고 부르면 앞 5행을 예시로 볼 수 있습니다. "
                            "그 5행으로 평균이나 건수를 계산해 답하지 마세요.")
        scan = min(max(a.get("scan", SCAN_DEFAULT), 1), SCAN_MAX)
        scan_offset = max(a.get("scan_offset", 0), 0)
        got = c.rows(service, params, scan_offset, scan, ttl=60, stable=True)   # 같은 집계를 곧 다시 물으면 다시 받지 않는다
    else:
        got = c.rows(service, params, offset, limit)

    rows = [clean(r) for r in got["rows"]]
    total = got["total"]
    # 필터가 실제로 먹었는지 받은 행 전체로 확인한다 (집계하기 전에). 위치 인자는 틀려도 오류 없이 다른 조건의
    # 정상 응답이 오는 경우가 있어서, 확인하지 않으면 엉뚱한 범위의 평균이 필터 이름을 달고 나간다.
    checked: dict = {}
    if spec:
        for p, v in zip(spec["params"], params):
            if v is None:
                continue
            seen = [r.get(p["name"]) for r in rows if isinstance(r, dict) and not _blank(r.get(p["name"]))]
            if not seen:
                checked[p["name"]] = "확인 못 함 (결과에 같은 이름의 필드가 없음)" if rows else "확인 못 함 (행 없음)"
                continue
            off = [h for h in seen if str(v).strip().lower() not in str(h).lower()]
            checked[p["name"]] = "반영됨" if not off else f"불일치 {len(off)}/{len(seen)}행"
            if off:
                notices.append(f"필터 {p['name']}={v}가 결과에 반영되지 않은 것 같습니다. 받은 {len(seen)}행 중 {len(off)}행의 값이 다릅니다 "
                               f"(예: {off[0]}). 이 결과를 '{v}'의 값이라고 말하지 마세요. 값의 형식을 dataset_describe로 확인하거나 where로 거르세요.")
    if scanning:
        known = walk_keys(rows[:50])
        named = [("where", f) for f, _, _ in where] + [("group_by", g) for g in group_by] + \
                [("metrics", str(x).partition(":")[2].strip()) for x in metrics if ":" in str(x)]
        bad = sorted({f"{f}({kind})" for kind, f in named if rows and f not in known})
        if bad:                                           # 오타 난 필드로 묶으면 전부 빈 값 한 묶음이 된다. 조용히 넘기지 않는다
            raise ToolError("BAD_INPUT", f"이 데이터에 없는 필드입니다: {', '.join(bad)}",
                            "쓸 수 있는 필드: " + ", ".join(sorted(known)))
        for f, op, want in where:                         # 조건은 날짜인데 데이터가 날짜로 읽히지 않으면 견줄 수 없다. 숨기지 않는다
            if op in ("=", "!=", "~") or not _date_digits(want):
                continue
            vals = [str(r[f]).strip() for r in rows[:200] if not _blank(r.get(f))]
            if vals and not any(_date_digits(v) for v in vals):
                notices.append(f"where의 {f}{op}{want}: 조건은 날짜인데 이 필드의 값(예: {vals[0]})은 날짜 꼴로 읽히지 않아 "
                               "제대로 견주지 못했을 수 있습니다. 값의 형식에 맞춰 조건을 다시 쓰세요.")
        matched = [r for r in rows if _match(r, where)]
        end = scan_offset + len(rows) + (got.get("missing") or 0)
        dups, missing = got.get("dups", 0), got.get("missing")
        unused = [p_ for p_, v in zip(spec["params"], params) if v is None] if spec else []
        if unused:
            narrow = ("서버 쪽 필터(" + ", ".join(f"{p_['name']} {p_['description']}".strip() for p_ in unused[:4]) +
                      ")로 1,000행 이하로 좁힐 수 있으면 정확해집니다. 좁힐 수 없으면 '약 …'으로 말하세요.")
        else:
            narrow = ("더 좁힐 서버 쪽 필터가 없습니다. '약 …'으로 말하고, 더 작은 단위(예: 자치구별)의 데이터셋이 있는지 "
                      "dataset_search로 찾아보세요.")
        if got.get("changing"):
            notices.append(f"이 데이터는 받는 사이에 값이 바뀌어 전체 {total:,}행을 빠짐없이 한 번씩 받았는지 확인하지 못했습니다. "
                           "건수와 합계는 어림값이라고 말하세요. " + narrow)
            meta["rows_missing"] = "확인 못 함"
        elif dups and missing == 0:
            notices.append(f"전체 {len(rows):,}행을 빠짐없이 확인한 값입니다. 서울시 서버가 일부 행을 중복해 보내 {got['passes']}번 받아 "
                           "맞췄고 결과에는 영향이 없습니다. 사용자에게는 '전체를 확인한 값'이라고만 전하면 됩니다.")
        elif dups and missing:
            pct = missing / total * 100 if total else 0
            meta["rows_missing"], meta["rows_missing_pct"] = missing, round(pct, 1)
            if pct > 5:
                notices.append(f"믿기 어려운 값입니다. 전체 {total:,}행 중 {missing:,}행(약 {pct:.0f}%)을 받지 못했습니다(서울시 서버가 "
                               "행을 중복해 보냈고 시간 안에 다 맞추지 못했습니다). 이 숫자를 답으로 쓰지 말고 한 번 더 부르세요. " + narrow)
            else:
                notices.append(f"어림값입니다. 전체 {total:,}행 중 {missing:,}행(약 {pct:.1f}%)을 받지 못했습니다(서울시 서버가 행을 "
                               f"중복해 보냈고 시간 안에 다 맞추지 못했습니다). '약 …' 꼴로 말하고, 실제 건수는 이보다 최대 {missing:,}건 많을 수 있다고 "
                               "전하세요. " + narrow)
        elif dups:
            notices.append("서울시 서버가 일부 행을 중복해 보내, 그만큼 다른 행이 빠졌을 수 있습니다. 어림값이라고 말하세요. " + narrow)
            meta["rows_missing"] = "확인 못 함"
        complete = scan_offset == 0 and got["ended"] and not got["partial"] and not (dups and missing != 0) \
            and not got.get("changing")                   # 끝까지, 빠짐없이 받았을 때만
        meta.update({"total": total, "scanned": len(rows), "matched": len(matched), "scan_complete": complete})
        if not complete:
            more = not got["ended"]
            meta["next_scan_offset"] = end if more else None
            whole = f"전체 {total:,}건 중" if total is not None else "전체 건수를 알 수 없는 데이터에서"
            merge = ("조각끼리는 count와 sum만 더할 수 있습니다. 평균은 sum과 count를 더해 다시 나누고, 중앙값은 조각끼리 합칠 수 없습니다."
                     if grouping else "")
            if (total or end) > sc.PAGE_MAX and (more or scan_offset):
                merge += " 이 서비스는 행 순서가 호출마다 달라질 수 있어, 조각을 더한 값은 겹치거나 빠진 행이 있는 어림값입니다."
            if more:
                notices.append(f"{whole} {scan_offset + 1:,}~{end:,}번째 {len(rows):,}건만 훑었습니다. 이 결과는 전체가 아닙니다. "
                               f"scan을 늘리거나(최대 {SCAN_MAX:,}) scan_offset을 {end}(으)로 주어 이어 훑을 수 있습니다. {narrow} " + merge)
            elif scan_offset:                             # 뒤 조각을 받았을 때만 '앞 조각과 합치라'고 한다
                notices.append(f"{whole} {scan_offset + 1:,}~{end:,}번째를 훑은 마지막 조각입니다. "
                               "앞 조각의 결과와 합쳐야 전체가 됩니다. " + merge)
        if grouping:
            rows, notes = _aggregate(matched, group_by, metrics)
            notices += notes
            meta["groups"] = len(rows)
            rows = _sort(rows, sort) if sort else rows
            if len(rows) > offset + limit or offset:
                notices.append(f"묶음 {len(rows)}개 중 {offset + 1}~{min(offset + limit, len(rows))}번째를 실었습니다 "
                               f"({'sort 기준' if sort else '건수가 많은 순'}). limit과 offset으로 나머지를 봅니다.")
            meta["offset"] = offset
            rows = rows[offset:offset + limit]
            if a.get("fields"):
                notices.append("묶어서 집계할 때는 fields를 쓰지 않습니다. 묶는 필드와 집계 값만 나옵니다.")
        else:
            matched = _sort(matched, sort) if sort else matched
            rows = matched[offset:offset + limit]
            meta["offset"] = offset
            meta["next_offset"] = offset + len(rows) if offset + len(rows) < len(matched) else None
        meta["returned"] = len(rows)
    else:
        meta.update({"total": total, "returned": len(rows), "offset": offset,
                     "next_offset": offset + len(rows) if rows and not got["ended"] else None})
        if total is None and not got["ended"]:    # 일부 서비스는 총계 자리에 받은 행 수를 넣는다. 그 값은 총계로 쓰지 않는다
            notices.append("이 서비스는 전체 건수를 알려 주지 않습니다. next_offset으로 이어 받아 끝을 확인하세요.")

    if got["sample_capped"]:
        notices.append(SAMPLE_NOTICE)
    if got["partial"]:
        notices.append("시간 한도(20초) 안에 받은 데까지만 실었습니다." if got.get("partial_why") != "error" else
                       "받는 도중 서울시 서버가 오류나 점검 화면을 돌려줘, 그때까지 받은 데까지만 실었습니다. 잠시 뒤 다시 부르세요.")
    if not rows and not scanning:
        notices.append("조건에 맞는 데이터가 없습니다. 필터 값의 형식(날짜 자릿수, 정확한 명칭)을 dataset_describe로 확인하세요.")
    if not scanning and offset and (total or 0) > sc.PAGE_MAX:
        notices.append("이 서비스는 행 순서가 호출마다 달라질 수 있어, offset으로 이어 받으면 같은 행이 다시 나오거나 빠질 수 있습니다. "
                       "빠짐없이 세거나 견주려면 where, sort, metrics를 쓰세요(서버가 겹침을 확인합니다).")
    if service == "culturalEventInfo" and any(f in ("STRTDATE", "END_DATE") for f, _, _ in where):
        notices.append("행사 기간이 겹치는 것만 골랐습니다. 그날 실제 공연 회차가 있는지는 ORG_LINK의 원문에서 확인하라고 전하세요.")
    if service == "GetParkingInfo":
        notices.append("시영주차장만 들어 있습니다(구영, 민영 제외). PRK_STTS_YN이 1이 아닌 곳의 NOW_PRK_VHCL_CNT 0은 "
                       "빈 주차장이 아니라 실시간 값이 없다는 뜻입니다.")

    if checked:
        meta["filters_checked"] = checked

    fields = a.get("fields") or []
    if fields and not grouping:
        missing = [f for f in fields if rows and f not in rows[0]]
        if missing:
            raise ToolError("BAD_INPUT", f"fields에 없는 열이 있습니다: {', '.join(missing)}",
                            "쓸 수 있는 열: " + ", ".join(rows[0].keys()))
        rows = [{f: r.get(f) for f in fields} for r in rows]
    elif not grouping:
        rows = [prune(r) for r in rows]                   # 빈 값은 싣지 않는다 (인허가 데이터는 절반이 빈 칸이다)
    common, rows = hoist_common(rows) if not grouping else ({}, rows)
    meta.update({"filters": {p["name"]: v for p, v in zip(spec["params"], params) if v is not None} if spec else None,
                 "key_mode": key_mode, "source_url": spec["source_url"] if spec else None})
    keep_null = {k: meta[k] for k in ("total", "next_offset", "next_scan_offset") if k in meta}
    body: dict = {"meta": {**prune(meta), **keep_null}, "notices": notices}
    if dataset:
        labels = _labels(dataset, walk_keys(rows) | set(common) | {m.partition(":")[2] for m in metrics})
        if labels:
            body["fields"] = labels
    if common:
        body["common"] = common
    body["rows"] = rows
    return body


# ── place_now ──────────────────────────────────────────────────────────────

SAMPLE_PLACE = "광화문·덕수궁"       # 서울시 샘플 키가 무엇을 물어도 돌려주는 장소
PLACE_UNITS = {"AREA_PPLTN_MIN·MAX, FCST_PPLTN_MIN·MAX": "명 (통신 데이터로 추정한 범위)", "…_RATE": "%",
               "TEMP, MAX_TEMP, MIN_TEMP": "℃", "HUMIDITY": "%", "WIND_SPD": "m/s", "PM10, PM25": "㎍/㎥",
               "ROAD_TRAFFIC_SPD, SPD": "km/h", "DIST": "m", "CPCTY": "면(전체 주차면)", "CUR_PRK_CNT": "대(지금 주차된 차량)",
               "RATES, ADD_RATES": "원", "TIME_RATES, ADD_TIME_RATES": "분", "…_PAYMENT_AMT_MIN·MAX": "원",
               "…_PAYMENT_CNT": "건 (신한카드 결제만. 몇 분 동안의 집계인지는 서울시 명세에 없다)",
               "SUB_ARVTIME": "초 (0이면 값 없음. SUB_ARMG1의 문장을 따른다)",
               "PRK_TYPE": "NW는 노외 주차장. 그 밖의 코드(BS, NS, NP, BP)의 뜻은 서울시 명세에 없다"}
SECTION_NAMES = ("population", "weather", "traffic", "events", "parking", "subway", "bus", "bike",
                 "commercial", "charger", "alerts")
DEFAULT_SECTIONS = ["population", "weather", "traffic", "events"]


def _cap(items: list, n: int, notes: list, what: str) -> list:
    if len(items) > n:
        notes.append(f"{what} {len(items)}건 중 {n}건만 실었습니다.")
    return items[:n]


def _sec_population(c: dict, notes: list, cap: int) -> Any:
    src = (c.get("LIVE_PPLTN_STTS") or [c])[0]
    return prune(src, drop=("AREA_NM", "AREA_CD"))


def _sec_weather(c: dict, notes: list, cap: int) -> Any:
    w = dict((c.get("WEATHER_STTS") or [{}])[0])
    if isinstance(w.get("FCST24HOURS"), list):
        w["FCST24HOURS"] = w["FCST24HOURS"][:6]          # 앞으로 6시간만
    return prune(w)


def _sec_traffic(c: dict, notes: list, cap: int) -> Any:
    t = c.get("ROAD_TRAFFIC_STTS") or {}
    links = [x for x in (t.get("ROAD_TRAFFIC_STTS") or []) if isinstance(x, dict)]
    slow = sorted((x for x in links if num(x.get("SPD")) is not None), key=lambda x: num(x["SPD"]))[:5]   # type: ignore[arg-type,return-value]
    keep = ("ROAD_NM", "START_ND_NM", "END_ND_NM", "DIST", "SPD", "IDX")
    return prune({"AVG_ROAD_DATA": prune(t.get("AVG_ROAD_DATA") or {}), "link_count": len(links),
                  "slowest_links": [{k: x[k] for k in keep if k in x} for x in slow],
                  "ACDNT_CNTRL_STTS": _accidents(c)})


def _accidents(c: dict) -> list:
    drop = ("ACDNT_ENG_TYPE", "ACDNT_ENG_DTYPE", "ACDNT_ENG_INFO", "ACDNT_X", "ACDNT_Y")
    return [prune(x, drop) for x in (c.get("ACDNT_CNTRL_STTS") or []) if isinstance(x, dict) and prune(x, drop)]


def _sec_events(c: dict, notes: list, cap: int) -> Any:
    items = [prune(x, ("THUMBNAIL", "EVENT_X", "EVENT_Y")) for x in c.get("EVENT_STTS") or []]
    return _cap(items, min(cap, 10), notes, "행사")


def _sec_parking(c: dict, notes: list, cap: int) -> Any:
    lots = sorted(c.get("PRK_STTS") or [], key=lambda x: str(x.get("CUR_PRK_YN")) != "Y")    # 실시간 현황이 있는 곳부터
    live = sum(1 for x in lots if str(x.get("CUR_PRK_YN")) == "Y")
    if lots:
        notes.append(f"주차장 {len(lots)}곳 중 실시간 값을 주는 곳은 {live}곳입니다 (CUR_PRK_YN이 Y인 곳)."
                     + ("" if live else " 실시간 자리가 필요하면 dataset_query {\"dataset\": \"OA-21709\", \"filters\": {\"ADDR\": \"자치구명\"}}로 "
                        "그 자치구의 시영주차장을 보세요."))
    return _cap([prune(x, ("PRK_CD", "LNG", "LAT")) for x in lots], cap, notes, "주차장")


def _sec_subway(c: dict, notes: list, cap: int) -> Any:
    stations = []
    for s in c.get("SUB_STTS") or []:
        d = prune(s, ("SUB_STN_X", "SUB_STN_Y", "SUB_STN_JIBUN", "SUB_STN_RADDR"))
        d["SUB_DETAIL"] = [prune(x, ("SUB_NT_STN", "SUB_BF_STN", "SUB_ORD")) for x in s.get("SUB_DETAIL") or []]
        stations.append(prune(d))
    return prune({"SUB_STTS": stations, "LIVE_SUB_PPLTN": prune(c.get("LIVE_SUB_PPLTN") or {})})


def _sec_bus(c: dict, notes: list, cap: int) -> Any:
    stops = [prune(x, ("BUS_STN_X", "BUS_STN_Y", "BUS_RESULT_MSG")) for x in c.get("BUS_STN_STTS") or []]
    return prune({"BUS_STN_STTS": _cap(stops, cap, notes, "버스 정류소"),
                  "LIVE_BUS_PPLTN": prune(c.get("LIVE_BUS_PPLTN") or {})})


def _sec_bike(c: dict, notes: list, cap: int) -> Any:
    return _cap([prune(x, ("SBIKE_X", "SBIKE_Y")) for x in c.get("SBIKE_STTS") or []], cap, notes, "따릉이 대여소")


def _sec_commercial(c: dict, notes: list, cap: int) -> Any:
    return prune(c.get("LIVE_CMRCL_STTS") or {})


def _sec_charger(c: dict, notes: list, cap: int) -> Any:
    out = []
    for s in c.get("CHARGER_STTS") or []:
        d = prune(s, ("STAT_X", "STAT_Y", "STAT_ID", "CHARGER_DETAILS"))
        tally: dict[str, int] = {}
        for ch in s.get("CHARGER_DETAILS") or []:
            k = f"{ch.get('CHARGER_TYPE', '')} {ch.get('CHARGER_STAT', '')}".strip()
            tally[k] = tally.get(k, 0) + 1
        d["chargers"] = tally                            # 충전기별 상세 대신 '종류 상태: 대수'로 줄인다
        out.append(d)
    return _cap(out, cap, notes, "충전소")


def _sec_alerts(c: dict, notes: list, cap: int) -> Any:
    return prune({"LIVE_DST_MESSAGE": c.get("LIVE_DST_MESSAGE") or [], "ACDNT_CNTRL_STTS": _accidents(c),
                  "LIVE_YNA_NEWS": (c.get("LIVE_YNA_NEWS") or [])[:5]})


SECTIONS: dict[str, Callable[[dict, list, int], Any]] = {
    "population": _sec_population, "weather": _sec_weather, "traffic": _sec_traffic, "events": _sec_events,
    "parking": _sec_parking, "subway": _sec_subway, "bus": _sec_bus, "bike": _sec_bike,
    "commercial": _sec_commercial, "charger": _sec_charger, "alerts": _sec_alerts,
}


def _resolve_place(query: str) -> str:
    name, cands = places.resolve(query)
    if name:
        return name
    if cands:
        hint = "candidates 중 하나를 골라 place에 그대로 넣으세요. 어느 곳인지 불분명하면 사용자에게 물으세요."
        if client().key_mode == "sample":
            hint += f" 지금은 체험 모드라 이 중 '{SAMPLE_PLACE}'만 볼 수 있습니다." if SAMPLE_PLACE in cands else \
                    f" 지금은 체험 모드라 후보 어느 곳도 볼 수 없습니다('{SAMPLE_PLACE}'만 됩니다)."
        what = f"'{query}'에 맞는 장소가 여럿입니다" if len(cands) > 1 else f"'{query}': 정확히 같은 이름은 없고 비슷한 곳이 있습니다"
        raise ToolError("PLACE_AMBIGUOUS", f"{what}: {', '.join(cands)}", hint, candidates=cands)
    extra = " 자치구 단위는 제공하지 않습니다. 그 구 안의 역이나 상권 이름으로 물어야 합니다." if str(query).strip().endswith("구") else ""
    raise ToolError("PLACE_UNKNOWN", f"'{query}': 서울시가 지정한 실시간 도시데이터 장소 121곳에 없는 이름입니다." + extra,
                    "place 없이 place_now를 부르면 121곳 목록이 나옵니다. 가까운 곳을 골라 다시 부르거나 사용자에게 어디가 가까운지 물으세요.")


def _fetch_place(name: str, light: bool) -> dict:
    c = client()
    service = CITY_PPLTN if light else CITY
    got = c.cache.get(f"place|{service}|{name}")
    if got is None:
        page = c.call(service, 1, 1, [name])
        if not page.rows:
            raise ToolError("NO_DATA", f"'{name}'의 실시간 데이터가 지금 비어 있습니다.", "잠시 뒤 다시 시도하세요.")
        got, fresh = page.rows[0], True
    else:
        fresh = False
    back = str(got.get("AREA_NM", "")).strip()
    if back and places.norm(back) != places.norm(name):
        if c.key_mode == "sample":
            raise ToolError("SAMPLE_PLACE_ONLY",
                            f"지금은 체험 모드라 '{back}'만 볼 수 있습니다. '{name}'의 실시간 데이터는 무료 인증키가 있어야 합니다.",
                            KEY_NEXT + f" 체험 삼아 '{back}'은 지금 바로 물어볼 수 있다고 알려 주세요.",
                            requested=name, available=back)
        raise ToolError("PLACE_MISMATCH", f"'{name}'을 물었는데 서울시 서버가 '{back}'을 돌려줬습니다.",
                        "장소 목록이 바뀐 것 같습니다. place 없이 place_now를 불러 목록을 확인하세요.")
    if fresh:
        c.cache.set(f"place|{service}|{name}", got, 60)   # 요청한 장소가 맞을 때만 캐시한다
    return got


def _stamp(item: dict) -> str | None:
    return ((item.get("population") or {}).get("PPLTN_TIME") or (item.get("weather") or {}).get("WEATHER_TIME")
            or ((item.get("traffic") or {}).get("AVG_ROAD_DATA") or {}).get("ROAD_TRAFFIC_TIME")
            or _cmrcl_time((item.get("commercial") or {}).get("CMRCL_TIME")))


def _cmrcl_time(v: Any) -> str | None:
    """상권 기준 시각은 '20261114 2040' 꼴이다. 다른 섹션과 같은 꼴로 바꾼다."""
    m_ = re.fullmatch(r"(\d{4})(\d{2})(\d{2}) (\d{2})(\d{2})", str(v or "").strip())
    return "{}-{}-{} {}:{}".format(*m_.groups()) if m_ else None


def place_now(a: dict) -> dict:
    asked = a.get("places") or ([a["place"]] if a.get("place") else [])
    if not asked:
        return {"meta": {"total": len(places.ALL), "returned": len(places.ALL), "kind": "place_list"},
                "notices": ["실시간 도시데이터는 서울시 지정 121곳만 제공합니다. 이름을 place에 그대로 넣으세요."],
                "rows": [{"category": cat, "places": names} for cat, names in places.PLACES.items()]}
    if len(asked) > 5:
        raise ToolError("BAD_INPUT", "한 번에 5곳까지 비교할 수 있습니다.", "places를 5곳 이하로 나눠 부르세요.")
    sections = list(a.get("sections") or DEFAULT_SECTIONS)
    bad = [s for s in sections if s not in SECTIONS]
    if bad:
        raise ToolError("BAD_INPUT", f"없는 섹션입니다: {', '.join(bad)}", "쓸 수 있는 섹션: " + ", ".join(SECTION_NAMES))
    light = set(sections) == {"population"}
    many = len(asked) > 1
    caps = [15] if not many else [max(4, 15 // len(asked)), 2]       # 여러 곳이면 곳마다 목록을 짧게. 그래도 크면 2건까지
    for cap in caps:
        rows, notices, as_of, failed = _build_places(asked, sections, light, cap)
        if _size({"rows": rows}) <= MAX_CHARS - 8000:
            break
    if failed and len(failed) == len(asked):
        first = failed[0]
        raise ToolError(first.code, first.message, first.next, **first.data)
    if many and set(sections) & {"parking", "bus", "bike", "charger", "events"}:
        notices.append(f"여러 곳을 견주느라 주차장, 정류소, 대여소, 충전소, 행사 목록은 곳마다 {cap}건까지만 실었습니다. "
                       "한 곳의 전체 목록은 그곳만 따로 부르세요.")
    return _finish_places(rows, notices, as_of, sections)


def _build_places(asked: list, sections: list, light: bool, cap: int) -> tuple[list, list, list, list]:
    many = len(asked) > 1
    rows, notices, as_of, failed = [], [], [], []
    for q in asked:
        tag = (lambda n, q=q: f"[{q}] {n}") if len(asked) > 1 else (lambda n: n)
        try:
            name = _resolve_place(str(q))
            c = _fetch_place(name, light)
        except ToolError as e:
            if len(asked) == 1:
                raise
            failed.append(e)                             # 한 곳이 안 돼도 나머지는 돌려준다
            rows.append({"place": q, "error": prune({"code": e.code, "message": e.message, "next": e.next}), **e.data})
            notices.append(tag(e.message))
            continue
        if places.norm(str(q)) != places.norm(name):
            notices.append(tag(f"장소 해석: '{q}' → '{name}' (서울시 공식 장소명). 다른 곳을 뜻했다면 place 없이 불러 목록에서 고르세요."))
        item: dict = {"place": name, "category": places.CATEGORY_OF.get(name, "")}
        for s in sections:
            notes: list = []
            item[s] = clean(SECTIONS[s](c, notes, cap))
            if _blank(item[s]):
                item[s] = None
                notes.append(f"{s} 정보는 이 장소에 지금 없습니다.")
            # 안내에 섹션 이름을 달아 둔다. 그 섹션을 빼게 되면 안내도 같이 뺀다
            notices += [(s, tag(n)) for n in notes if not (many and "건만 실었습니다" in n)]
        if _stamp(item):
            as_of.append(_stamp(item))
        rows.append(item)
    return rows, notices, as_of, failed


def _finish_places(rows: list, notices: list, as_of: list, sections: list) -> dict:
    sections = list(sections)
    dropped = []
    while _size({"rows": rows}) > MAX_CHARS - 8000 and len(sections) > 1:    # 장소를 버리지 않고 가장 큰 섹션부터 뺀다
        pool = [x for x in sections if x not in ("population", "weather")] or sections   # 혼잡도와 날씨는 끝까지 남긴다
        biggest = max(pool, key=lambda s: sum(_size({"x": r.get(s)}) for r in rows))
        sections.remove(biggest)
        dropped.append(biggest)
        for r in rows:
            r.pop(biggest, None)
    notices = [n if isinstance(n, str) else n[1] for n in notices if isinstance(n, str) or n[0] not in dropped]
    if "parking" in sections:
        notices.append("주차장의 CUR_PRK_CNT는 지금 주차된 차량 수입니다. 서울시 명세는 '주차 가능 면수'라고 적었지만, 시영주차장 데이터의 "
                       "'현재 주차 차량수'와 같은 값입니다. 남은 자리는 CPCTY에서 CUR_PRK_CNT를 뺀 값입니다.")
    if "subway" in sections:
        notices.append("지하철의 SUB_ARVTIME이 0이면 '지금 도착'이 아니라 값이 없다는 뜻입니다. 그때는 SUB_ARMG1의 문장을 따르세요.")
    if dropped:
        notices.append(f"응답이 커서 {', '.join(dropped)} 섹션을 뺐습니다. 장소 수나 sections를 줄여 따로 부르세요.")
    meta = {"returned": len(rows), "sections": sections, "as_of": min(as_of) if as_of else None,
            "key_mode": client().key_mode, "source_url": catalog.VIEW_URL.format(id="OA-21285"), "units": PLACE_UNITS}
    if meta["as_of"]:
        old = (age_sec(meta["as_of"]) or 0) // 60
        notices.insert(0, f"{meta['as_of']} 기준 서울시 실시간 도시데이터입니다."
                       + (f" 약 {old}분 전 값이니 '지금'이 아니라 그 시각 기준으로 말하세요." if old >= 15 else "")
                       + (" 인구는 통신 데이터로 추정한 범위(최소~최대)입니다." if "population" in sections else ""))
    else:
        notices.insert(0, f"{now_kst():%Y-%m-%d %H:%M} 조회 기준 서울시 실시간 도시데이터입니다.")
    if not meta["as_of"]:
        meta["as_of"] = f"{now_kst():%Y-%m-%d %H:%M}"     # 받은 섹션에 기준 시각이 없으면 조회 시각을 싣는다
        meta["as_of_is"] = "조회 시각"
    body = {"meta": meta, "notices": notices, "rows": rows}
    labels = _labels("OA-21285", walk_keys(rows))
    if "CUR_PRK_CNT" in labels:                           # 명세의 설명이 실제 값과 다르다. 그대로 실으면 응답 안에서 말이 엇갈린다
        labels["CUR_PRK_CNT"] = "지금 주차된 차량 수 (서울시 명세의 '주차 가능 면수'는 실제 값과 다름)"
    if labels:
        body["fields"] = labels
    return body


# ── subway_arrivals ────────────────────────────────────────────────────────

LINES = {"1001": "1호선", "1002": "2호선", "1003": "3호선", "1004": "4호선", "1005": "5호선", "1006": "6호선",
         "1007": "7호선", "1008": "8호선", "1009": "9호선", "1032": "GTX-A", "1061": "중앙선", "1063": "경의중앙선",
         "1065": "공항철도", "1067": "경춘선", "1075": "수인분당선", "1077": "신분당선", "1081": "경강선",
         "1092": "우이신설선", "1093": "서해선", "1094": "신림선"}
LINE_ALIASES = {"분당선": "수인분당선", "수인선": "수인분당선", "경의선": "경의중앙선", "공항선": "공항철도",
                "gtx": "GTX-A", "gtxa": "GTX-A", "우이선": "우이신설선"}
SUBWAY_SAMPLE_STATION = "서울"
STALE_SEC = 90               # 이보다 오래 갱신되지 않은 행은 이미 지나간 열차일 수 있다고 표시한다
SUBWAY_TERMS = {
    "status": "열차 위치를 서울시가 문장으로 적은 것. '전역'은 바로 앞 역, '[3]번째 전역'은 세 정거장 전",
    "eta_sec": "as_of 기준 도착까지 남은 초. 0이거나 없으면 status의 문장을 따른다",
    "eta_now_sec": "지금 기준으로 남은 초(eta_sec에서 정보가 갱신된 뒤 지난 시간을 뺀 값)",
    "direction": "상행·하행, 2호선은 내선(시계 방향)·외선(반시계 방향). 사용자에게는 heading의 '○○방면'으로 말한다",
    "heading": "행선지와 방면. 2호선은 순환선이라 행선지(예: 성수행)보다 방면이 정확하다",
    "stations_away": "몇 정거장 전에 있는지. 0이면 이 역에 들어오는 중이거나 서 있다",
    "last_train": "막차일 때만 true로 나온다. 없으면 막차가 아니다. 막차 시각표는 이 데이터에 없다",
    "stale": "갱신된 지 90초가 넘어 이미 지나갔을 수 있는 행",
}


def _count_subway(add: int = 0) -> int:
    """오늘 실시간 지하철 호출 수 (세는 일은 Client.call이 한다)."""
    return sc.count_subway(add)


def _via_place(station: str) -> str:
    """실시간 지하철 키가 없어도, 그 역이 주요 장소 121곳 안에 있으면 place_now의 subway 섹션이 도착 정보를 준다."""
    base = station.split("(")[0]
    name, cands = places.resolve(base + "역")
    if name and name.endswith("역"):
        return (f" 일반 인증키만 있다면 place_now {{\"place\": \"{name}\", \"sections\": [\"subway\"]}}로 "
                "이 역 주변의 도착 정보를 볼 수 있습니다.")
    near, more = places.resolve(base)
    guess = near or (more[0] if more else (cands[0] if cands else None))
    return (" 일반 인증키만 있다면, 이 역이 주요 장소 121곳 중 한 곳의 주변 역일 때 place_now의 subway 섹션에 도착 정보가 나옵니다"
            + (f" (예: place_now {{\"place\": \"{guess}\", \"sections\": [\"subway\"]}})." if guess else "."))


def _line_target(line: str) -> str:
    t = re.sub(r"\s+", "", line)
    if t.isdigit():
        return f"{int(t)}호선"
    return LINE_ALIASES.get(t.lower(), t)


def _stations_away(r: dict) -> int:
    """ordkey의 셋째~다섯째 글자가 '몇 정거장 전'이다 (예: 11001서동탄0 → 1). 읽지 못하면 뒤로 보낸다."""
    m_ = re.match(r"^\d\d(\d{3})", str(r.get("ordkey") or ""))
    return int(m_.group(1)) if m_ else 999


def _fetch_station(c: sc.Client, api_name: str, sample: bool) -> list:
    hit = c.cache.get(f"subway|{api_name}")
    if hit is not None:
        return hit
    try:
        page = c.call(SUBWAY, 0, sc.SAMPLE_MAX if sample else 50, [api_name], subway=True)
    except SeoulError as e:
        if e.code == "ERROR-336":
            # 샘플 키는 서울역 말고는 무엇에나 이 코드를 준다. 실제 키에서 역 이름이 틀리면 INFO-200(빈 결과)이 온다.
            raise ToolError("ERROR-336", f"서울시 지하철 서버가 '{api_name}'역 요청을 받아들이지 않았습니다 (ERROR-336).",
                            "역 이름이 서울시 표기와 다를 수 있습니다. 이름이 맞다면 잠시 뒤 다시 시도하세요.") from None
        if e.code in ("INFO-100", "ERROR-338"):
            raise ToolError("SUBWAY_KEY_NEEDED", "실시간 지하철 인증키가 확인되지 않았습니다.",
                            "실시간 지하철은 일반 인증키와 별도의 키를 씁니다. 열린데이터광장에서 '실시간 지하철 인증키'를 신청해 "
                            "key_setup의 subway에 넣으세요." + _via_place(api_name)) from None
        raise
    return c.cache.set(f"subway|{api_name}", page.rows, 15)


def subway_arrivals(a: dict) -> dict:
    c = client()
    asked = re.sub(r"\s+", "", a["station"])
    names = stations.resolve(asked)
    name = names[0]
    limit = min(max(a.get("limit", 20), 1), 50)
    sample = c.subway_key_mode == "sample"
    if sample and name != SUBWAY_SAMPLE_STATION:
        have_general = c.key_mode != "sample"
        lead = ("일반 인증키는 들어 있습니다. " if have_general else "지금은 체험 모드입니다. ") + \
            "지하철 도착 정보는 별도의 '실시간 지하철 인증키'가 필요합니다. "
        raise ToolError("SAMPLE_STATION_ONLY",
                        lead + f"이 키가 없으면 '{SUBWAY_SAMPLE_STATION}'역의 앞 5건만 볼 수 있어 '{name}'역은 답할 수 없습니다.",
                        (_via_place(name).strip() + " " if have_general else "")
                        + "실시간 지하철 인증키는 열린데이터광장의 같은 화면에서 따로 신청합니다(무료). 받은 키는 key_setup의 subway에 넣으세요."
                        + ("" if have_general else _via_place(name)),
                        requested=name, available=SUBWAY_SAMPLE_STATION)
    raw = []
    for n in (names[:1] if sample else names):          # 서울역처럼 이름이 둘로 나뉜 역은 둘 다 부른다
        raw += _fetch_station(c, n, sample)
    line = _line_target(str(a.get("line") or "")) if a.get("line") else ""
    direction = re.sub(r"\s+|순환", "", str(a.get("direction") or ""))
    present: dict[str, set] = {}
    rows = []
    for r in raw:
        ln = LINES.get(str(r.get("subwayId")), str(r.get("subwayId")))
        present.setdefault(ln, set()).add(f"{r.get('updnLine')} {r.get('trainLineNm') or ''}".strip())
        if line and ln != line:
            continue
        if direction and direction not in str(r.get("updnLine", "")) and direction not in str(r.get("trainLineNm", "")) \
                and direction not in str(r.get("btrainSttus", "")):
            continue
        eta = num(r.get("barvlDt"))
        age = age_sec(str(r.get("recptnDt") or ""))
        rows.append(prune({"line": ln, "direction": r.get("updnLine"), "heading": r.get("trainLineNm"),
                           "status": r.get("arvlMsg2"), "train_at": r.get("arvlMsg3"),
                           "stations_away": _stations_away(r) if _stations_away(r) < 999 else None,
                           "eta_sec": int(eta) if eta else None,
                           "eta_now_sec": max(int(eta) - (age or 0), 0) if eta else None,
                           "destination": r.get("bstatnNm"), "train_type": r.get("btrainSttus"),
                           "last_train": str(r.get("lstcarAt")) == "1" or None,
                           "updated": r.get("recptnDt"), "age_sec": age,
                           "stale": (age or 0) > STALE_SEC or None}) | {"_rank": _stations_away(r)})
    # 가까운 열차부터. 이 역에 들어오는 중이거나 서 있는 열차(0정거장)가 맨 앞이다. 같은 거리면 남은 시간 순
    rows.sort(key=lambda x: (x["line"], x.get("direction", ""), bool(x.get("stale")), x["_rank"], x.get("eta_sec") or 0))
    for x in rows:
        del x["_rank"]
    notices = []
    stamps = [x["updated"] for x in rows if x.get("updated")]
    now = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    as_of = min(max(stamps), now) if stamps else None     # 일부 노선은 갱신 시각이 몇 분 앞서 찍혀 온다. 지금을 넘기지 않는다
    if as_of:
        notices.append(f"{as_of} 기준 실시간 도착 정보입니다. 행은 노선과 방향별로 가까운 열차부터 놓았습니다. "
                       "남은 시간은 eta_now_sec(지금 기준)를, 없으면 status의 문장을 따르세요.")
    stale = sum(1 for x in rows if x.get("stale"))
    if stale:
        notices.append(f"{stale}건은 갱신된 지 90초가 넘어 이미 지나갔을 수 있습니다(stale 표시, 같은 방향에서 뒤로 보냈습니다). "
                       "종착역에서 대기 중인 열차일 수도 있습니다.")
    if line == "2호선" or any(x["line"] == "2호선" for x in rows[:limit]):
        notices.append("2호선은 순환선이라 양방향 열차의 행선지가 같을 수 있습니다. 행선지보다 heading의 '○○방면'으로 말하세요.")
    if sample and not rows:
        notices.append("실시간 지하철 인증키가 없어 이 역의 도착 정보 중 앞 5건만 받았습니다. 그 안에 조건에 맞는 "
                       "열차가 없었을 뿐, 열차가 없다는 뜻이 아닙니다. 실시간 지하철 인증키를 넣으면 전체를 봅니다.")
    elif not raw:
        sug = [x for x in stations.suggest(asked) if x not in names]
        notices.append(f"'{name}'역의 도착 정보가 비어 있습니다. 운행 시간이 아니거나 역 이름이 서울시 표기와 다를 수 있습니다."
                       + (f" 비슷한 이름: {', '.join(sug)}. 이 중 하나로 다시 부르세요." if sug else
                          " '역'을 뺀 공식 역명인지 확인하세요."))
    elif not rows:
        have = "; ".join(f"{ln}({', '.join(sorted(d))})" for ln, d in sorted(present.items()))
        notices.append(f"이 역의 도착 정보 {len(raw)}건 중 line, direction 조건에 맞는 열차가 없습니다. 지금 이 역에 있는 것: {have}. "
                       "이 중 하나로 다시 부르세요. 행선지가 바뀐 노선도 있습니다(예: 4호선 당고개행은 진접·불암산행으로 운행).")
    elif sample:
        notices.append("실시간 지하철 인증키가 없어 앞 5건만 받았습니다. 이 역의 열차가 더 있을 수 있습니다. 키를 넣으면 전체가 나옵니다.")
    if len(rows) > limit:
        notices.append(f"{len(rows)}건 중 가까운 {limit}건만 실었습니다. line이나 direction으로 좁히면 나머지를 볼 수 있습니다.")
    if any(x.get("last_train") for x in rows[:limit]):
        notices.append("last_train 표시가 있는 열차는 그 방향의 막차입니다.")
    used = _count_subway()
    if used >= SUBWAY_DAILY * 0.8:
        notices.append(f"오늘 실시간 지하철 호출이 {used}건입니다. 하루 한도는 {SUBWAY_DAILY:,}건이고, 넘으면 서울시 서버가 요청을 거절합니다.")
    total = len(rows)
    meta = {"station": " + ".join(names) if len(names) > 1 and not sample else name, "total": total,
            "returned": min(total, limit), "as_of": as_of, "key_mode": c.subway_key_mode,
            "source_url": catalog.VIEW_URL.format(id="OA-12764"), "units": SUBWAY_TERMS}
    if asked.removesuffix("역") not in names and asked not in names:
        meta["station_note"] = f"'{asked}'을(를) API 이름 '{' + '.join(names)}'(으)로 불렀습니다."
    return {"meta": meta, "notices": notices, "rows": rows[:limit]}


# ── bike_stations ──────────────────────────────────────────────────────────

def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return int(2 * 6371000 * math.asin(math.sqrt(h)))


def _bike_row(r: dict) -> dict:
    return {"station": str(r.get("stationName", "")).strip(), "id": r.get("stationId"),
            "bikes": int(num(r.get("parkingBikeTotCnt")) or 0), "racks": int(num(r.get("rackTotCnt")) or 0),
            "lat": num(r.get("stationLatitude")), "lng": num(r.get("stationLongitude"))}


def bike_stations(a: dict) -> dict:
    query, lat, lng = (a.get("query") or "").strip(), a.get("lat"), a.get("lng")
    if not query and (lat is None or lng is None):
        raise ToolError("BAD_INPUT", "대여소 이름의 일부(query)나 좌표(lat, lng) 중 하나는 있어야 합니다.",
                        "예: {\"query\": \"여의나루\"} 또는 {\"lat\": 37.5271, \"lng\": 126.9326}. "
                        "장소 이름만 알면 place_now의 bike 섹션이 그 장소 주변 대여소를 줍니다.")
    c = client()
    if c.key_mode == "sample":
        preview = [_bike_row(r) for r in c.rows(BIKE, (), 0, sc.SAMPLE_MAX)["rows"]]
        raise ToolError("SAMPLE_LIMIT", "지금은 체험 모드라 전체 대여소를 찾을 수 없습니다. 앞 5곳만 미리보기로 실었습니다.",
                        KEY_NEXT + " preview는 요청한 위치와 무관한 예시이니 답으로 쓰지 마세요.", preview=preview)
    got = c.all_rows(BIKE, (), ttl=60, identity="stationId")   # 재고가 계속 바뀌므로 내용이 아니라 대여소 번호로 구분한다
    stations = [_bike_row(r) for r in got["rows"]]
    if query:
        q = places.norm(query)
        stations = [s for s in stations if q in places.norm(s["station"])]
    radius = max(a.get("radius_m", 500), 50)
    if lat is not None and lng is not None:
        for s in stations:
            s["distance_m"] = _distance_m(lat, lng, s["lat"], s["lng"]) if s["lat"] and s["lng"] else None
        stations = sorted((s for s in stations if s.get("distance_m") is not None and s["distance_m"] <= radius),
                          key=lambda s: s["distance_m"])
    limit = min(max(a.get("limit", 10), 1), 50)
    notices = [f"{now_kst():%Y-%m-%d %H:%M} 조회 기준입니다. 따릉이 재고는 수시로 바뀌니 출발 직전에 다시 확인하세요."]
    if not stations:
        notices.append("조건에 맞는 대여소가 없습니다. query를 짧게 하거나 radius_m을 넓혀 보세요." )
    if got["partial"]:
        notices.append("시간 한도 안에 받은 대여소만 검색했습니다. 다시 부르면 전체를 봅니다.")
    if got.get("missing"):
        notices.append(f"서울시 서버가 대여소 목록을 중복해 보내 약 {got['missing']}곳을 받지 못했습니다. 찾는 대여소가 없으면 다시 불러 보세요.")
    return {"meta": {"total": len(stations), "returned": min(len(stations), limit), "stations_searched": len(got["rows"]),
                     "key_mode": c.key_mode, "source_url": catalog.VIEW_URL.format(id="OA-15493"),
                     "units": {"bikes": "지금 빌릴 수 있는 자전거 수", "racks": "거치대 수", "distance_m": "기준 좌표에서의 직선거리(m)"}},
            "notices": notices, "rows": stations[:limit]}


# ── key_setup ──────────────────────────────────────────────────────────────

def _mask(key: str) -> str:
    return "설정 안 됨(체험 모드)" if key == sc.SAMPLE_KEY else f"설정됨(끝 4자리 {key[-4:]})"


def _key_status() -> dict:
    c = client()
    notices = []
    if c.key_mode == "sample":
        notices.append("인증키가 없어 체험 모드입니다. 일반 인증키 하나면 지하철 실시간 도착을 뺀 모든 기능이 열립니다. "
                       "발급은 무료이고 https://data.seoul.go.kr 로그인 뒤 '인증키 신청'에서 바로 됩니다.")
    if c.subway_key_mode == "sample":
        notices.append("지하철 실시간 도착은 '실시간 지하철 인증키'를 따로 신청해야 합니다(같은 사이트, 무료, 하루 1,000건).")
    return {"meta": {"general": _mask(c.key), "subway": _mask(c.subway_key),
                     "env_file": str(sc._env_files()[0])},
            "notices": notices, "rows": []}


def write_keys(general: str | None, subway: str | None) -> Path:
    path = sc._env_files()[0]
    new = {k: v for k, v in (("SEOUL_API_KEY", general), ("SEOUL_SUBWAY_API_KEY", subway)) if v}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out = []
    for line in lines:                                   # 주석과 다른 줄은 그대로 두고 해당 키의 줄만 바꾼다
        name = line.strip().removeprefix("export ").split("=", 1)[0].strip()
        if name in new and "=" in line and not line.lstrip().startswith("#"):
            out.append(f"{name}={new.pop(name)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in new.items()]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)    # 처음부터 본인만 읽게 만든다
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)                                # 쓰다 죽어도 반쪽 파일이 남지 않게
    return path


def key_setup(a: dict) -> dict:
    general, subway = (a.get("general") or "").strip(), (a.get("subway") or "").strip()
    if not general and not subway:
        return _key_status()
    bad = "인증키가 확인되지 않았습니다. 복사할 때 앞뒤 공백이 들어갔거나 글자가 빠지지 않았는지 봐 주세요."
    if general:
        try:
            probe = sc.Client(key=general, fetch=client()._fetch)
            probe.deadline = client().deadline
            probe.call(BIKE, 1, 1)
        except SeoulError as e:
            if e.code == "INFO-100":
                raise ToolError("KEY_INVALID", "일반 " + bad, "키를 다시 받아 넣어 주세요. 발급 직후에는 몇 분 뒤에 활성화되기도 합니다.") from None
            raise
    if subway:
        try:
            probe = sc.Client(key=general or client().key, subway_key=subway, fetch=client()._fetch)
            probe.deadline = client().deadline
            probe.call(SUBWAY, 0, 1, [SUBWAY_SAMPLE_STATION], subway=True)
        except SeoulError as e:
            if e.code == "ERROR-338":
                raise ToolError("KEY_INVALID", "넣어 주신 키는 일반 인증키라서 실시간 지하철에는 쓸 수 없습니다.",
                                "열린데이터광장의 인증키 신청 화면에서 '실시간 지하철' 종류로 따로 받은 키를 subway에 넣어 주세요. "
                                "지금 키는 general에 넣으면 됩니다.") from None
            if e.code == "INFO-100":
                raise ToolError("KEY_INVALID", "실시간 지하철 " + bad,
                                "일반 인증키와 실시간 지하철 인증키는 서로 다른 키입니다. 신청 화면에서 종류를 확인해 주세요.") from None
            raise
    path = write_keys(general or None, subway or None)
    set_client(None)                                     # 새 키로 다시 만든다
    out = _key_status()
    shadowed = [n for n, v in (("일반", general), ("실시간 지하철", subway)) if v and
                (client().key if n == "일반" else client().subway_key) != v]
    if shadowed:                                         # 환경변수가 파일보다 먼저라, 저장한 키가 쓰이지 않을 수 있다
        out["notices"].insert(0, f"인증키를 확인하고 {path} 에 저장했습니다. 다만 {', '.join(shadowed)} 키는 환경변수에 다른 값이 "
                              "잡혀 있어 그쪽이 먼저 쓰입니다. 저장한 키를 쓰려면 환경변수(SEOUL_API_KEY, SEOUL_OPENAPI_KEY, "
                              "SEOUL_SUBWAY_API_KEY)를 지우고 Claude를 다시 켜세요.")
    else:
        out["notices"].insert(0, f"인증키를 확인하고 {path} 에 저장했습니다. 지금부터 바로 적용됩니다.")
    return out


# ── 도구 명세 ──────────────────────────────────────────────────────────────

def _schema(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
OA = "^[Oo][Aa]-[0-9]+$"
ARR_S = {"type": "array", "items": {"type": "string"}}

TOOLS: list[tuple[str, str, str, dict, Callable[[dict], dict], bool]] = [
    ("dataset_search", "데이터셋 찾기",
     "서울 열린데이터광장의 데이터셋을 한글 키워드로 찾는다. 결과의 dataset(OA-번호)을 dataset_query에 넣는다. "
     f"query를 비우면 호출을 확인해 둔 대표 데이터셋 {len(catalog.FEATURED)}건(문화행사, 부동산 실거래, 주차장, 무더위쉼터 등)과 각 필터를 준다. "
     "키워드는 짧은 명사가 잘 맞는다 (예: '도서관', '실거래', '무더위쉼터').",
     _schema({"query": {**S, "description": "한글 키워드. 비우면 대표 데이터셋 목록"},
              "category": {**S, "description": "분류로 좁히기 (예: 교통, 보건, 문화/관광, 환경)"},
              "api_only": {**B, "description": "Open API가 있는 것만 (기본 true). 통계 데이터셋은 API가 없다"},
              "limit": {**I, "minimum": 1, "maximum": 30, "description": "이만큼 모일 때까지 쪽을 넘긴다 (기본 10). 받은 쪽은 자르지 않아 조금 더 올 수 있다"},
              "page": {**I, "minimum": 1, "description": "검색 결과 쪽 번호 (기본 1). meta.next_page로 이어 본다"}}),
     dataset_search, True),
    ("dataset_describe", "데이터셋 명세",
     "데이터셋 하나의 필터(이름, 필수 여부, 값 형식)와 출력 필드의 한글 설명, 바로 부를 수 있는 호출 예시를 준다. "
     "처음 보는 데이터셋에 필터를 걸거나 필드 뜻을 알아야 할 때 쓴다. 필터 없이 앞부분만 볼 때는 건너뛰고 dataset_query를 바로 불러도 된다.",
     _schema({"dataset": {**S, "pattern": OA, "description": "OA-번호 (예: OA-15486)"}}, ["dataset"]),
     dataset_describe, True),
    ("dataset_query", "데이터 조회와 집계",
     "데이터셋의 행을 가져오거나 집계한다. dataset에 OA-번호, filters에 {필터이름: 값}을 준다. "
     "filters는 서울시 서버가 거르는 조건이라 빠르지만 정확 일치(일부는 앞부분 일치)만 된다. "
     "범위, 부분 일치, 빈 값은 where를 쓴다 (예: \"CTRT_DAY>=20260801\", \"TITLE~재즈\", \"RTRCN_DAY=\"). "
     "평균, 합계, 건수는 직접 계산하지 말고 metrics와 group_by를 쓴다 (예: metrics [\"count\", \"avg:THING_AMT\"], group_by [\"BLDG_USG\"]). "
     "where, sort, group_by, metrics를 쓰면 서버가 한 번에 10,000행(scan으로 50,000행까지, 20초 안에서)을 훑어 계산하고 "
     "meta.scan_complete로 전체를 다 봤는지 알려 준다. 다 못 봤으면 filters로 먼저 좁히거나 scan을 늘린다. "
     "자주 쓰는 것: OA-21275 부동산 매매 실거래(필터 RCPT_YR, CGG_NM / THING_AMT는 만원, 취소 건 제외는 where \"RTRCN_DAY=\"), "
     "OA-15486 문화행사(CODENAME, TITLE은 부분 일치 / 기간은 where STRTDATE·END_DATE), OA-21709 시영주차장 실시간(ADDR), "
     "OA-2219 실시간 대기(자치구 25곳, sort \"-PM\"으로 나쁜 순).",
     _schema({"dataset": {**S, "pattern": OA, "description": "OA-번호. service와 둘 중 하나만"},
              "filters": {"type": "object", "description": "{필터이름: 값}. 이름은 dataset_describe의 filters에 있다",
                          "additionalProperties": {"type": ["string", "number"]}},
              "where": {**ARR_S, "maxItems": 10, "description": "받은 행에 거는 조건. '필드 연산자 값', 연산자는 = != > >= < <= ~(포함). 모두 만족해야 한다. 빈 값은 범위 조건에 맞지 않는다"},
              "group_by": {**ARR_S, "description": "집계할 때 묶는 필드"},
              "metrics": {**ARR_S, "description": "'count' 또는 '함수:필드'. 함수는 avg, sum, min, max, median"},
              "sort": {**S, "description": "정렬할 필드. 앞에 -를 붙이면 큰 것부터 (예: -avg_THING_AMT, -PM)"},
              "fields": {**ARR_S, "description": "돌려받을 열만 고르기 (응답이 클 때)"},
              "limit": {**I, "minimum": 1, "maximum": 1000, "description": "최대 행 수 (기본 20, 묶음은 100)"},
              "offset": {**I, "minimum": 0, "description": "건너뛸 행 수 (기본 0). meta.next_offset을 넣으면 이어 받는다"},
              "scan": {**I, "minimum": 1, "maximum": 50000, "description": "where·정렬·집계 때 훑을 최대 행 수 (기본 10000, 최대 50000)"},
              "scan_offset": {**I, "minimum": 0, "description": "훑기 시작 위치. meta.next_scan_offset을 넣으면 다음 구간을 훑는다"},
              "service": {**S, "description": "서비스명을 직접 줄 때 (명세 화면을 못 읽는 경우의 우회로)"},
              "path_args": {**ARR_S, "description": "service와 함께 쓰는 위치 인자 배열"}}),
     dataset_query, True),
    ("place_now", "장소의 지금",
     "서울 주요 장소 121곳의 실시간 상황을 준다. '지금 ○○ 붐벼?', '○○ 날씨·주차·행사' 같은 질문에 쓴다. "
     "place에 장소 이름을 그대로 넣으면 공식 이름으로 맞춘다 (예: '성수동', '코엑스', '강남역'). 여럿이 맞으면 후보를 돌려준다. "
     "places에 5곳까지 넣으면 한 번에 비교한다. place를 비우면 121곳 목록을 준다. "
     "sections 기본값은 population(혼잡도, 추정 인구, 12시간 예측), weather(기온, 강수, 미세먼지), traffic(도로 속도, 사고·통제), events(행사). "
     "그 밖에 parking, subway, bus, bike, commercial(카드 결제 기반 상권), charger(전기차 충전소), alerts(재난문자). "
     "장소 단위 요약이다. 특정 역의 도착 열차는 subway_arrivals, 좌표나 이름으로 따릉이를 찾을 때는 bike_stations를 쓴다.",
     _schema({"place": {**S, "description": "장소 이름. 비우면 121곳 목록"},
              "places": {**ARR_S, "maxItems": 5, "description": "여러 곳 비교 (최대 5곳). place 대신 쓴다"},
              "sections": {"type": "array", "items": {"type": "string", "enum": list(SECTION_NAMES)},
                           "description": "받을 섹션. 혼잡도만 물으면 [\"population\"]이 가장 가볍다"}}),
     place_now, True),
    ("subway_arrivals", "지하철 실시간 도착",
     "지하철역 하나의 실시간 도착 예정 열차를 준다. station은 '강남', '서울역'처럼 넣는다. line(예: '2호선', '신분당선')과 "
     "direction(상행, 하행, 내선, 외선, 또는 '○○행')으로 좁힌다. 일반 인증키와 별도의 실시간 지하철 인증키가 필요하고 하루 1,000건까지다.",
     _schema({"station": {**S, "description": "역 이름 (예: 강남, 서울역, 홍대입구)"},
              "line": {**S, "description": "호선으로 좁히기 (예: 2호선, 9호선, 경의중앙선)"},
              "direction": {**S, "description": "방향으로 좁히기 (상행, 하행, 내선, 외선, 또는 행선지 일부)"},
              "limit": {**I, "minimum": 1, "maximum": 50, "description": "최대 열차 수 (기본 20)"}}, ["station"]),
     subway_arrivals, True),
    ("bike_stations", "따릉이 대여소",
     "따릉이 대여소를 이름 일부나 좌표로 찾아 지금 빌릴 수 있는 자전거 수를 준다. query(대여소 이름의 일부)와 lat·lng(주변 검색) 중 "
     "하나는 있어야 한다. 좌표를 주면 가까운 순으로 정렬한다. 전체 대여소를 받아 거르므로 인증키가 필요하다.",
     _schema({"query": {**S, "description": "대여소 이름의 일부 (예: 여의나루, 시청)"},
              "lat": {"type": "number", "minimum": 37.0, "maximum": 38.0, "description": "위도 (WGS84)"},
              "lng": {"type": "number", "minimum": 126.0, "maximum": 128.0, "description": "경도 (WGS84)"},
              "radius_m": {**I, "minimum": 50, "maximum": 5000, "description": "반경 미터 (기본 500)"},
              "limit": {**I, "minimum": 1, "maximum": 50, "description": "최대 대여소 수 (기본 10)"}}),
     bike_stations, True),
    ("key_setup", "인증키 설정",
     "인증키 상태를 보거나 저장한다. 인자 없이 부르면 지금 상태와 발급 방법을 알려 준다. 사용자가 키를 알려 주면 general(일반 인증키) "
     "또는 subway(실시간 지하철 인증키)에 넣는다. 실제 호출로 확인한 뒤 사용자 컴퓨터의 설정 파일에 저장한다. 응답에 키를 다시 적지 않는다.",
     _schema({"general": {**S, "description": "열린데이터광장 일반 인증키"},
              "subway": {**S, "description": "실시간 지하철 인증키 (일반 키와 다른 키)"}}),
     key_setup, False),
]
HANDLERS = {name: fn for name, _t, _d, _s, fn, _r in TOOLS}
SCHEMAS = {name: s for name, _t, _d, s, _f, _r in TOOLS}

INSTRUCTIONS = (
    "서울 열린데이터광장(data.seoul.go.kr) 공공데이터 도구. "
    "실시간 상황은 place_now(장소 121곳), subway_arrivals(역), bike_stations(따릉이)를 쓰고, "
    "그 밖의 데이터는 dataset_search로 OA-번호를 찾아 dataset_query로 조회한다. 평균·합계는 dataset_query의 metrics로 계산한다. "
    "응답의 notices는 사용자에게 전해야 할 말이니 답변에 빠짐없이 반영한다(체험 모드, 일부만 훑음, 필터 미반영 등). "
    "실시간 값은 meta.as_of의 기준 시각과 함께 말한다. 필드 뜻과 단위는 응답의 fields와 meta.units를 따른다. "
    "오류의 error.next는 다음에 할 일이다. 후보(candidates)가 오면 고르거나 사용자에게 묻는다. "
    "인증키가 없으면 체험 모드로 돌며 일부 장소와 5행만 보인다. 그때는 요청한 대상과 다른 데이터를 답으로 쓰지 말고 key_setup으로 안내한다."
)


def tool_specs() -> list[dict]:
    return [{"name": n, "title": t, "description": d, "inputSchema": s,
             "annotations": {"title": t, "readOnlyHint": ro, "destructiveHint": False, "idempotentHint": True,
                             "openWorldHint": True}} for n, t, d, s, _f, ro in TOOLS]


# ── 실행 ───────────────────────────────────────────────────────────────────

def _redact(text: object) -> str:
    c = _client
    return sc.redact(text, c.key if c else None, c.subway_key if c else None)


def _size(body: dict) -> int:
    return len(json.dumps(body, ensure_ascii=False))


NARROW = {"dataset_query": "fields로 열을 줄이거나 limit을 낮추고, 이어 보려면 offset을 쓰세요.",
          "place_now": "places나 sections를 줄여 다시 부르세요.",
          "bike_stations": "limit이나 radius_m을 줄이세요.", "subway_arrivals": "line이나 direction으로 좁히세요.",
          "dataset_search": "limit을 줄이거나 검색어를 더 구체적으로 주세요."}


def fit(body: dict, tool: str = "") -> dict:
    """응답이 MAX_CHARS를 넘으면 가장 긴 목록(rows, 없으면 fields)의 뒤쪽을 덜고 notices에 남긴다."""
    if _size(body) <= MAX_CHARS:
        return body
    for key in ("rows", "fields"):
        items = body.get(key)
        if not isinstance(items, list) or len(items) < 2:
            continue
        before = len(items)
        while _size(body) > MAX_CHARS and len(items) > 1:
            del items[max(len(items) * 3 // 4, 1) if len(items) > 4 else len(items) - 1:]
        if len(items) < before:
            what = "행" if key == "rows" else "필드 설명"
            body.setdefault("notices", []).append(
                f"응답이 커서 {what} {before}건 중 앞 {len(items)}건만 실었습니다. "
                + (NARROW.get(tool, "조건을 좁혀 다시 부르세요.") if key == "rows" else ""))
            if isinstance(body.get("meta"), dict) and key == "rows":
                body["meta"]["returned"] = len(items)
                body["meta"]["truncated"] = True
                if "offset" in body["meta"]:
                    body["meta"]["next_offset"] = body["meta"]["offset"] + len(items)   # 덜어 낸 뒤의 위치로 고친다
        if _size(body) <= MAX_CHARS:
            return body
    if _size(body) > MAX_CHARS:                           # 한 행이 통째로 큰 경우
        body.pop("fields", None)
        body.setdefault("notices", []).append("응답이 커서 필드 설명을 뺐습니다. " + NARROW.get(tool, "조건을 좁혀 다시 부르세요."))
    if _size(body) > MAX_CHARS and isinstance(body.get("rows"), list):      # 그래도 크면 행 안쪽을 줄인다
        if isinstance(body.get("common"), dict):
            body["common"] = _shrink(body["common"])
        body["rows"] = _shrink(body["rows"])
        body["notices"].append("행 하나가 너무 커서 안쪽 목록은 5건까지, 긴 글은 200자까지만 실었습니다. 필요한 열만 fields로 골라 다시 부르세요.")
    if _size(body) > MAX_CHARS:                           # 어떤 경우에도 상한을 넘겨 보내지 않는다
        body.pop("common", None)
        body["rows"] = []
        if isinstance(body.get("meta"), dict):
            body["meta"]["returned"] = 0
        body["notices"].append("줄여도 응답이 상한을 넘어 행을 싣지 못했습니다. fields로 열을 고르거나 limit을 1로 줄여 다시 부르세요.")
    return body


def _shrink(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= 200 else v[:200] + "…"
    if isinstance(v, dict):
        return {k: _shrink(x) for k, x in v.items()}
    if isinstance(v, list):
        head = [_shrink(x) for x in v[:5]]
        return head + [f"…(+{len(v) - 5}건 생략)"] if len(v) > 5 else head
    return v


def coerce(name: str, args: dict) -> dict:
    """모델이 흔히 틀리게 주는 형식을 받아 준다. 모르는 인자는 조용히 버리지 않고 알려 준다."""
    props = SCHEMAS[name]["properties"]
    unknown = [k for k in args if k not in props]
    if unknown:
        raise ToolError("BAD_INPUT", f"{name}에 없는 인자입니다: {', '.join(unknown)}",
                        "쓸 수 있는 인자: " + ", ".join(props) +
                        (". 조회 조건은 filters 안에 {이름: 값}으로 넣습니다." if name == "dataset_query" else ""))
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        typ = props[k].get("type")
        try:
            if typ == "integer" and not isinstance(v, bool):
                v = int(float(v))
            elif typ == "number" and not isinstance(v, bool):
                v = float(v)
            elif typ == "boolean" and isinstance(v, str):
                v = v.strip().lower() in ("true", "1", "yes", "y")
            elif typ == "array" and isinstance(v, str):
                v = json.loads(v) if v.strip().startswith("[") else [x.strip() for x in v.split(",") if x.strip()]
            elif typ == "object" and isinstance(v, str):
                v = json.loads(v)
            elif typ == "string" and not isinstance(v, str):
                if isinstance(v, (list, dict)):
                    raise ValueError("문자열 자리에 목록이나 객체")
                v = str(v)
        except (ValueError, TypeError, OverflowError):
            raise ToolError("BAD_INPUT", f"{k}의 형식이 맞지 않습니다: {v}",
                            f"{k}: {props[k].get('description', typ)}") from None
        if typ == "array" and not isinstance(v, list) or typ == "object" and not isinstance(v, dict):
            raise ToolError("BAD_INPUT", f"{k}의 형식이 맞지 않습니다.", f"{k}: {props[k].get('description', typ)}")
        if typ == "number" and (not math.isfinite(v) or not props[k].get("minimum", v) <= v <= props[k].get("maximum", v)):
            raise ToolError("BAD_INPUT", f"{k}의 값이 범위를 벗어났습니다: {v}",
                            f"{k}: {props[k].get('description', '')} ({props[k].get('minimum')}~{props[k].get('maximum')})")
        if typ == "array":
            if any(isinstance(x, (list, dict)) for x in v):
                raise ToolError("BAD_INPUT", f"{k}에는 글자나 숫자만 넣을 수 있습니다.", f"{k}: {props[k].get('description', '')}")
            allowed = (props[k].get("items") or {}).get("enum")
            if allowed and any(x not in allowed for x in v):
                raise ToolError("BAD_INPUT", f"{k}에 쓸 수 없는 값이 있습니다: {', '.join(str(x) for x in v if x not in allowed)}",
                                "쓸 수 있는 값: " + ", ".join(allowed))
            if len(v) > props[k].get("maxItems", 50):
                raise ToolError("BAD_INPUT", f"{k}에 넣을 수 있는 개수를 넘었습니다 (최대 {props[k].get('maxItems', 50)}개).",
                                f"{k}: {props[k].get('description', '')}")
        out[k] = v
    return out


def _error(code: str, message: str, next: str = "", **data: Any) -> dict:
    cut = lambda t, n: t if len(t) <= n else t[:n] + f"…(+{len(t) - n}자 생략)"      # noqa: E731
    body = {"error": prune({"code": code, "message": cut(_redact(message), 1500), "next": cut(_redact(next), 3000)}), **data}
    if _size(body) > MAX_CHARS:                           # 오류 응답도 상한을 넘기지 않는다
        body = {"error": body["error"]}
    return body


def run_tool(name: str, args: dict | None) -> tuple[dict, bool]:
    """(응답 본문, 오류 여부). 오류도 본문에 담아 돌려준다: 모델이 읽고 고쳐 부를 수 있게."""
    fn = HANDLERS.get(name)
    if fn is None:
        return _error("NO_TOOL", f"없는 도구입니다: {name}", "쓸 수 있는 도구: " + ", ".join(HANDLERS)), True
    try:
        a = coerce(name, dict(args or {}))
        missing = [k for k in SCHEMAS[name]["required"] if _blank(a.get(k))]
        if missing:
            raise ToolError("BAD_INPUT", f"필수 인자가 빠졌습니다: {', '.join(missing)}",
                            "; ".join(f"{k}: {SCHEMAS[name]['properties'][k].get('description', '')}" for k in missing))
        _refresh_keys()
        client().deadline = time.time() + DEADLINE
        body = fit(fn(a), name)
        body["notices"] = [n for i, n in enumerate(body.get("notices") or []) if n not in body["notices"][:i]]
        if not body["notices"]:
            del body["notices"]
        return body, False
    except ToolError as e:
        return _error(e.code, e.message, e.next, **e.data), True
    except SeoulError as e:
        return _error(e.code, e.message, e.hint), True
    except Exception as e:                                # 예상 못 한 오류. 키는 가린다
        return _error("INTERNAL", f"{type(e).__name__}: {e}",
                      "seoul-data 서버 안의 오류입니다. 같은 호출을 다시 해 보고, 반복되면 이 문장을 그대로 사용자에게 알려 주세요."), True


def _rpc_error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: Any) -> dict | None:
    """JSON-RPC 메시지 하나 → 응답 (알림이면 None). 모양이 틀린 메시지에도 죽지 않고 오류로 답한다."""
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    mid, method = msg.get("id"), msg.get("method")
    if mid is None or method is None:
        return None                                       # 알림(notifications/initialized 등)과 응답 메시지에는 답하지 않는다
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(mid, -32602, "Invalid params: params는 객체여야 합니다")
    msg = {**msg, "params": params}
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        res: dict[str, Any] = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": SERVER, "instructions": INSTRUCTIONS}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": tool_specs()}
    elif method == "tools/call":
        p = msg.get("params") or {}
        if not isinstance(p.get("name"), str) or not isinstance(p.get("arguments") or {}, dict):
            return _rpc_error(mid, -32602, "Invalid params: name은 문자열, arguments는 객체여야 합니다")
        if p["name"] not in HANDLERS:
            return _rpc_error(mid, -32602, f"Unknown tool: {p['name']}. 쓸 수 있는 도구: {', '.join(HANDLERS)}")
        body, err = run_tool(p["name"], p.get("arguments"))
        res = {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": err}
    else:
        return _rpc_error(mid, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def safe_handle(msg: Any) -> dict | None:
    try:
        return handle(msg)
    except Exception as e:                                # 어떤 메시지에도 프로세스가 죽지 않게
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32603,
                          f"Internal error: {type(e).__name__}: {_redact(e)}")


def serve() -> None:
    for stream in (sys.stdin, sys.stdout):                # 로캘이 C여도 한글이 깨지지 않게, 깨진 바이트에도 죽지 않게
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    out = sys.stdout
    sys.stdout = sys.stderr                               # 어디선가 print해도 프로토콜 스트림이 깨지지 않게
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(msg, list):                     # 배치. 2025-03-26 클라이언트가 보낼 수 있다
                answers = [r for r in (safe_handle(x) for x in msg) if r is not None]
                resp = (answers or None) if msg else _rpc_error(None, -32600, "Invalid Request")   # type: ignore[assignment]
            else:
                resp = safe_handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


# ── CLI ────────────────────────────────────────────────────────────────────

def _setup_cli() -> int:
    import getpass
    print("서울 열린데이터광장 인증키를 저장합니다. 입력한 글자는 화면에 보이지 않습니다. 없는 키는 그냥 Enter.")
    general = getpass.getpass("일반 인증키: ").strip()
    subway = getpass.getpass("실시간 지하철 인증키: ").strip()
    if not general and not subway:
        print("입력된 키가 없어 저장하지 않았습니다. 키를 복사해 붙여 넣은 뒤 Enter를 누르세요(글자는 화면에 보이지 않습니다).")
        return 1
    body, err = run_tool("key_setup", prune({"general": general, "subway": subway}))
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if not err:
        print("\n저장했습니다. Claude에서 다음 질문부터 새 키가 쓰입니다. 그래도 체험 모드라고 나오면 Claude를 완전히 껐다 켜세요.")
    return 1 if err else 0


def selftest(full: bool = False) -> int:
    """실제 서버를 불러 도구를 점검한다. 기대값은 키 유무에 따라 미리 정해 둔다 (통과 기준을 반증할 수 있게)."""
    sample = client().key_mode == "sample"
    sub_sample = client().subway_key_mode == "sample"
    ok = lambda b, e: not e                                                           # noqa: E731
    code = lambda want: (lambda b, e: e and b["error"]["code"] == want)               # noqa: E731
    cases: list[tuple[str, str, dict, Callable[[dict, bool], Any]]] = [
        ("대표 목록", "dataset_search", {}, lambda b, e: not e and len(b["rows"]) == len(catalog.FEATURED)),
        ("검색 화면 파서", "dataset_search", {"query": "따릉이"}, lambda b, e: not e and b["rows"] and b["meta"]["total"] > 0),
        ("명세 읽기", "dataset_describe", {"dataset": "OA-15486"},
         lambda b, e: not e and b["meta"]["service"] == "culturalEventInfo" and len(b["fields"]) > 10),
        ("필수 인자 조립", "dataset_query", {"dataset": "OA-2218", "filters": {"MSRMT_DT": "20121225"}, "limit": 3},
         lambda b, e: not e and b["rows"] and b["meta"]["filters_checked"].get("MSRMT_DT") == "반영됨"),
        ("필수 인자 누락 거절", "dataset_query", {"dataset": "OA-2218"}, code("BAD_INPUT")),
        ("전용 도구로 안내", "dataset_query", {"dataset": "OA-21285", "filters": {"AREA_NM": "성수카페거리"}}, code("USE_TOOL")),
        ("위치 인자 조립", "dataset_query", {"dataset": "OA-21275", "filters": {"CGG_NM": "강남구"}, "limit": 3},
         lambda b, e: not e and b["rows"] and (b.get("common", {}).get("CGG_NM") or b["rows"][0].get("CGG_NM")) == "강남구"),
        ("집계", "dataset_query", {"dataset": "OA-21275", "filters": {"CGG_NM": "강남구"}, "group_by": ["BLDG_USG"],
                                 "metrics": ["count", "avg:THING_AMT"]},
         code("SAMPLE_LIMIT") if sample else (lambda b, e: not e and b["rows"] and "avg_THING_AMT" in b["rows"][0])),
        ("없는 필터 거절", "dataset_query", {"dataset": "OA-21275", "filters": {"NOPE": "x"}}, code("BAD_INPUT")),
        ("장소 후보", "place_now", {"place": "광화문"}, code("PLACE_AMBIGUOUS")),
        ("장소 조회", "place_now", {"place": "광화문·덕수궁"},
         lambda b, e: not e and b["rows"][0]["population"]["AREA_CONGEST_LVL"] and b["meta"]["as_of"]),
        ("다른 장소", "place_now", {"place": "성수동", "sections": ["population"]},
         code("SAMPLE_PLACE_ONLY") if sample else (lambda b, e: not e and b["rows"][0]["place"] == "성수카페거리")),
        ("지하철 도착", "subway_arrivals", {"station": "서울역"},
         lambda b, e: not e and b["meta"]["station"] in ("서울", "서울 + 서울역") and b["rows"]),
        ("지하철 다른 역", "subway_arrivals", {"station": "강남"}, code("SAMPLE_STATION_ONLY") if sub_sample else ok),
        ("따릉이", "bike_stations", {"query": "망원"},
         code("SAMPLE_LIMIT") if sample else (lambda b, e: not e and b["meta"]["stations_searched"] > 1000)),
        ("키 상태", "key_setup", {}, ok),
    ]
    failed = 0
    try:                                                  # 디스크 캐시가 아니라 지금의 명세 화면을 읽는다
        fresh = catalog.get_spec("OA-15486", fetch=_site, use_disk=False)
        live_spec = fresh["service"] == "culturalEventInfo" and len(fresh["params"]) >= 3
    except SeoulError as e:
        live_spec = False
        print(f"  명세 화면을 지금 읽지 못했습니다: [{e.code}] {e.message}")
    failed += not live_spec
    print(f"seoul-data selftest · 일반 키 {'체험 모드' if sample else '있음'} · 지하철 키 {'체험 모드' if sub_sample else '있음'}\n")
    print(f"  {'PASS' if live_spec else 'FAIL'}  {'명세 화면 파서':<12} (캐시 없이 지금 화면)")
    for label, tool, args, check in cases:
        t = time.time()
        body, err = run_tool(tool, args)
        try:
            passed = bool(check(body, err))
        except (KeyError, IndexError, TypeError):
            passed = False
        failed += not passed
        detail = body["error"]["code"] if err else f"{body.get('meta', {}).get('returned', '-')}행"
        print(f"  {'PASS' if passed else 'FAIL'}  {label:<12} {tool:<17} {time.time() - t:5.2f}s  {detail}")
        if not passed:
            print("        " + json.dumps(body, ensure_ascii=False)[:400])
    if full:
        if sample:
            print("\n--full은 인증키가 있어야 돕니다 (체험 모드는 장소를 하나만 줍니다).")
            return 1
        print("\n121개 장소 이름 전수 확인")
        wrong = []
        for name in places.ALL:
            body, err = run_tool("place_now", {"place": name, "sections": ["population"]})
            if err:
                wrong.append((name, body["error"]["code"]))
        failed += len(wrong)
        print(f"  {'PASS' if not wrong else 'FAIL'}  {len(places.ALL) - len(wrong)}/{len(places.ALL)}곳 일치" +
              (f"  불일치: {wrong}" if wrong else ""))
    print(f"\n{'전부 통과' if not failed else f'{failed}건 실패'}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--list"]:
        for i, (n, t, _d, s, _f, _r) in enumerate(TOOLS, 1):
            args = ", ".join(k + ("" if k in s["required"] else "?") for k in s["properties"])
            print(f"{i}. {n:<17} {t:<12} ({args})")
        print("\n?는 생략 가능. 입력 상세: --help <도구>")
        return 0
    if argv[:1] == ["--help"] and len(argv) == 2 and argv[1] in HANDLERS:
        _n, t, d, s, _f, _r = next(x for x in TOOLS if x[0] == argv[1])
        print(f"{argv[1]} · {t}\n{d}\n")
        for k, v in s["properties"].items():
            print(f"  {k}{'' if k in s['required'] else '?'}: {v.get('type', '')}  {v.get('description', '')}".rstrip())
        return 0
    if argv[:1] == ["--call"] and len(argv) >= 2:
        try:
            args = json.loads(argv[2]) if len(argv) > 2 else {}
        except ValueError:
            print(f"인자는 JSON이어야 합니다. 예: --call {argv[1]} '{{\"place\": \"덕수궁\"}}'")
            return 2
        body, err = run_tool(argv[1], args)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1 if err else 0
    if argv[:1] == ["--setup"]:
        return _setup_cli()
    if argv[:1] == ["--selftest"]:
        return selftest(full="--full" in argv)
    if argv:
        print(__doc__)
        return 2
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
