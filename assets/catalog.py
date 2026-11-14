#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""열린데이터광장의 데이터셋을 찾고(검색 화면) 명세를 읽는다(Open API 명세 화면).

서비스명과 인자 순서를 주는 공식 API가 없어서 사이트 화면을 읽는다. 이 서버에서 가장 깨지기 쉬운 곳이므로
읽지 못하면 빈 결과가 아니라 SeoulError("SITE_CHANGED")를 낸다.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Callable

from seoul_client import SITE, SeoulError, disk_get, disk_set, http_fetch

SEARCH_URL = SITE + "/dataList/datasetList.do"
SPEC_URL = SITE + "/dataList/openApiView.do?infId={id}&srvType=A&serviceKind=1"
VIEW_URL = SITE + "/dataList/{id}/S/1/datasetView.do"
PAGE_SIZE = 10                       # 검색 화면은 한 쪽에 10건
SPEC_TTL = 24 * 3600
FIXED_PARAMS = ("KEY", "TYPE", "SERVICE", "START_INDEX", "END_INDEX")
ID_RE = re.compile(r"^OA-\d+$", re.I)

# 2026-11-14에 실제 호출을 확인한 대표 데이터셋. dataset_search를 query 없이 부르면 이것을 준다.
# 자치구별 인허가 데이터는 서비스명이 LOCALDATA_업종코드_구약자 꼴이다 (예: LOCALDATA_072404_JN은 종로구 일반음식점).
FEATURED = [
    {"dataset": "OA-21285", "service": "citydata", "title": "서울시 실시간 도시데이터 (주요 장소 121곳)",
     "use": "place_now 도구를 쓰세요"},
    {"dataset": "OA-12764", "service": "realtimeStationArrival", "title": "서울시 지하철 실시간 도착정보",
     "use": "subway_arrivals 도구를 쓰세요"},
    {"dataset": "OA-15493", "service": "bikeList", "title": "서울시 공공자전거 따릉이 실시간 대여정보",
     "use": "bike_stations 도구를 쓰세요"},
    {"dataset": "OA-2219", "service": "RealtimeCityAir", "title": "서울시 권역별 실시간 대기환경 현황 (자치구 25곳)",
     "use": "dataset_query", "filters": "없이 부르면 25곳 전체. SAREA_NM(권역명), MSRSTN_NM(자치구명)"},
    {"dataset": "OA-15486", "service": "culturalEventInfo", "title": "서울시 문화행사 정보",
     "use": "dataset_query", "filters": "CODENAME(분류), TITLE(행사명, 부분 일치), DATE(YYYY-MM-DD, 그날 끝나는 행사가 온다. 기간은 where STRTDATE, END_DATE로)"},
    {"dataset": "OA-21275", "service": "tbLnOpendataRtmsV", "title": "서울시 부동산 실거래가 정보 (매매)",
     "use": "dataset_query", "filters": "RCPT_YR(접수연도), CGG_NM(자치구명), BLDG_NM(건물명), CTRT_DAY(계약일 YYYYMMDD), BLDG_USG(건물용도)"},
    {"dataset": "OA-21709", "service": "GetParkingInfo", "title": "서울시 시영주차장 실시간 주차대수 정보",
     "use": "dataset_query", "filters": "ADDR(주소, 자치구명). 시영주차장만 있다. PRK_STTS_YN이 1인 곳만 NOW_PRK_VHCL_CNT(현재 주차 차량수)가 실시간 값이다"},
    {"dataset": "OA-20883", "service": "TbPublicWifiInfo", "title": "서울시 공공와이파이 서비스 위치 정보",
     "use": "dataset_query", "filters": "X_SWIFI_WRDOFC(자치구), X_SWIFI_ADRES1(도로명주소)"},
    {"dataset": "OA-21065", "service": "TbGtnHwcwP", "title": "서울시 무더위쉼터",
     "use": "dataset_query", "filters": "없음. 자치구로 좁히려면 where \"R_DETL_ADD~마포구\""},
    {"dataset": "OA-23056", "service": "aiLOCALDATA_072404", "title": "서울시 일반음식점 인허가 정보 (54만 건)",
     "use": "dataset_query", "filters": "TRDSTATEGBN(영업상태코드, 01은 영업/정상), BPLCNM(사업장명), APVPERMYMD(인허가일자). 서울 전체 54만 건이다. "
                "자치구 하나만 볼 때는 dataset_search '○○구 일반음식점'으로 자치구별 데이터셋을 찾아 쓰는 편이 빠르다"},
]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def is_dataset_id(value: str) -> bool:
    return bool(ID_RE.match((value or "").strip()))


# ── 검색 화면 ──────────────────────────────────────────────────────────────

def parse_search(page: str) -> dict:
    m = re.search(r"전체 검색결과(?:&nbsp;|\s)*<strong>\s*([\d,]+)", page)
    if not m:
        raise SeoulError("SITE_CHANGED", "검색 화면에서 결과 건수를 찾지 못했습니다.",
                         "열린데이터광장 화면 구조가 바뀐 것 같습니다. https://data.seoul.go.kr 에서 직접 검색한 뒤 "
                         "OA-번호를 dataset_describe에 넣으세요.")
    total = int(m.group(1).replace(",", ""))
    items = []
    for block in re.findall(r'<dl class="type-[a-z]">(.*?)</dl>', page, flags=re.S):
        head = re.search(r'data-rel="([A-Za-z0-9-]+)/([A-Z])/(\d+)/datasetView\.do"[^>]*class="goView"[^>]*>\s*<strong>(.*?)</strong>',
                         block, flags=re.S)
        if not head:
            continue
        kind = re.search(r'class="stat-ctg">\s*<span[^>]*>(.*?)</span>', block, flags=re.S)
        cat = re.search(r'class="stat-class-\d+">\s*\[(.*?)\]', block, flags=re.S)
        desc = re.search(r'class="list-statistics-info1"[^>]*>(.*?)</dd>', block, flags=re.S)
        info = _text((re.search(r'class="list-statistics-info2"[^>]*>(.*?)</dd>', block, flags=re.S) or [None, ""])[1])
        updated = re.search(r"수정일자\s*:\s*([\d-]+)", info)
        org = re.search(r"제공기관\s*:\s*(.*?)\s*(?:제공부서|$)", info)
        dept = re.search(r"제공부서\s*:\s*(.*)$", info)
        types = [t.strip() for t in re.findall(r'class="btn-data-\d+"[^>]*>([^<]+)</button>', block)]
        title = _text(head.group(4))
        ai_ready = title.endswith("AI친화")            # 제목 옆 배지 글자가 제목에 붙어 나온다
        items.append({
            "dataset": head.group(1),
            "title": title.removesuffix("AI친화").strip(),
            "kind": _text(kind.group(1)) if kind else "",
            "category": _text(cat.group(1)) if cat else "",
            "has_api": "OpenAPI" in types and is_dataset_id(head.group(1)),
            "types": types,
            "ai_ready": ai_ready,
            "url": f"{SITE}/dataList/{head.group(1)}/{head.group(2)}/{head.group(3)}/datasetView.do",
            "updated": updated.group(1) if updated else "",
            "org": " ".join(x for x in [org.group(1).strip() if org else "", dept.group(1).strip() if dept else ""] if x),
            "description": _text(desc.group(1)) if desc else "",
        })
    if total and not items:
        raise SeoulError("SITE_CHANGED", "검색 결과 목록을 읽지 못했습니다.",
                         "열린데이터광장 화면 구조가 바뀐 것 같습니다. OA-번호를 알면 dataset_describe로 바로 가세요.")
    return {"total": total, "items": items}


def search(query: str, page: int = 1, fetch: Callable[..., bytes] = http_fetch) -> dict:
    raw = fetch(SEARCH_URL, data={"basickwd": query, "pageIndex": str(max(int(page), 1))})
    return parse_search(raw.decode("utf-8", "replace"))


# ── 명세 화면 ──────────────────────────────────────────────────────────────

def _tables(page: str) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    for table in re.findall(r"<table.*?</table>", page, flags=re.S):
        cap = re.search(r"<caption>(.*?)</caption>", table, flags=re.S)
        name = _text(cap.group(1)) if cap else ""
        rows = [[_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.S)]
                for r in re.findall(r"<tr.*?</tr>", table, flags=re.S)]
        out.setdefault(name, rows)
    return out


def parse_spec(page: str, dataset: str) -> dict:
    tables = _tables(page)
    req = next((rows for name, rows in tables.items() if "요청인자" in name), None)
    outs = next((rows for name, rows in tables.items() if "출력값" in name), None)
    if not req:
        raise SeoulError("SITE_CHANGED", f"{dataset}의 명세 화면에서 요청인자 표를 찾지 못했습니다.",
                         f"이 데이터셋은 Open API를 제공하지 않거나 화면 구조가 바뀌었습니다. {VIEW_URL.format(id=dataset)} 에서 확인하세요.")
    service, params = "", []
    for row in req:
        if len(row) < 3 or row[0] in ("변수명", ""):
            continue
        name, typ, desc = row[0], row[1], row[2]
        values = row[3] if len(row) > 3 else ""
        if name == "SERVICE":
            service = values.strip()
        if name in FIXED_PARAMS:
            continue
        params.append({"name": name, "required": "필수" in typ, "type": re.sub(r"\(.*?\)", "", typ).strip().lower(),
                       "description": desc, "values": values})
    if not service:
        raise SeoulError("SITE_CHANGED", f"{dataset}의 명세 화면에서 서비스명을 찾지 못했습니다.",
                         f"{VIEW_URL.format(id=dataset)} 의 Open API 탭에서 서비스명을 확인해 dataset_query의 service에 직접 넣으세요.")
    fields = [{"name": r[1], "description": r[2]} for r in (outs or [])
              if len(r) >= 3 and r[0] not in ("No", "공통") and r[1]]
    samples = []
    for name, rows in tables.items():
        if "샘플" in name:
            for r in rows:
                m = re.search(r"https?://\S+", " ".join(r))
                if m:
                    label = " ".join(r).split("http")[0].replace("샘플 URL", "").strip()
                    samples.append({"label": label, "url": html.unescape(m.group(0))})
    host = "subway" if any("swopenapi" in s["url"].lower() for s in samples) else "general"
    first_index = 1
    for s in samples:                                 # 실시간 지하철 계열은 시작 위치가 0부터다
        m = re.search(re.escape(service) + r"/(\d+)/\d+", s["url"])
        if m:
            first_index = int(m.group(1))
            break
    return {"dataset": dataset, "service": service, "host": host, "first_index": first_index,
            "params": params, "fields": fields, "samples": samples[:3],
            "source_url": VIEW_URL.format(id=dataset)}


def get_spec(dataset: str, fetch: Callable[..., bytes] = http_fetch, use_disk: bool = True) -> dict:
    """명세를 읽는다. 하루 동안 디스크에 캐시한다 (사이트 요청 수를 줄이려고)."""
    dataset = dataset.strip().upper()
    if not is_dataset_id(dataset):
        raise SeoulError("BAD_INPUT", f"데이터셋 번호 형식이 아닙니다: {dataset}",
                         "OA-15486처럼 OA-로 시작하는 번호를 넣으세요. 번호는 dataset_search로 찾습니다.")
    name = f"spec/{dataset}.json"
    if use_disk:
        hit = disk_get(name, SPEC_TTL)
        if isinstance(hit, dict) and hit.get("service"):
            return hit
    try:
        raw = fetch(SPEC_URL.format(id=urllib.parse.quote(dataset)))
    except SeoulError as e:
        if e.code == "NOT_FOUND":
            raise SeoulError("NOT_FOUND", f"{dataset} 데이터셋의 Open API 명세가 없습니다.",
                             "번호가 틀렸거나 Open API를 제공하지 않는 데이터셋입니다. dataset_search에서 has_api가 true인 것을 고르세요.") from None
        stale = disk_get(name, float("inf")) if use_disk else None
        if isinstance(stale, dict) and stale.get("service"):
            return {**stale, "stale": "network"}        # 사이트가 안 되면 묵은 명세로 답하고 그 사실을 남긴다
        raise
    try:
        spec = parse_spec(raw.decode("utf-8", "replace"), dataset)
    except SeoulError:
        stale = disk_get(name, float("inf")) if use_disk else None
        if isinstance(stale, dict) and stale.get("service"):
            return {**stale, "stale": "site_changed"}   # 화면이 바뀌어 못 읽었다. 묵은 명세로 답하되 숨기지 않는다
        raise
    if use_disk:
        disk_set(name, spec)
    return spec


def order_params(spec: dict, filters: dict) -> list:
    """이름으로 받은 필터를 명세의 인자 순서대로 늘어놓는다. 빈 자리는 None (URL에서 공백이 된다)."""
    filters = {str(k).strip().upper(): v for k, v in (filters or {}).items() if v is not None and str(v).strip()}
    names = [p["name"].upper() for p in spec["params"]]
    unknown = [k for k in filters if k not in names]
    if unknown:
        raise SeoulError("BAD_INPUT", f"이 데이터셋에 없는 필터입니다: {', '.join(unknown)}",
                         "쓸 수 있는 필터: " + (", ".join(f"{p['name']}({p['description']})" for p in spec["params"]) or "없음"))
    missing = [p["name"] for p in spec["params"] if p["required"] and p["name"].upper() not in filters]
    if missing:
        detail = "; ".join(f"{p['name']}: {p['description']} {p['values']}".strip()
                           for p in spec["params"] if p["name"] in missing)
        raise SeoulError("BAD_INPUT", f"필수 필터가 빠졌습니다: {', '.join(missing)}", detail)
    return [filters.get(n) for n in names]
