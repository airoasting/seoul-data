#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""서울 열린데이터광장 Open API 접속층. 표준 라이브러리만 쓴다.

이 파일이 책임지는 것
- 인증키 찾기 (환경변수 → ~/.config/seoul-data/.env → 플러그인 폴더 .env → 샘플 키)
- 경로 기반 URL 조립 (인자는 쿼리스트링이 아니라 경로의 위치로 넘긴다)
- 모양이 제각각인 응답을 한 가지 Parsed로 읽기
- 재시도, 캐시, 오류 문장에서 키 가리기

2026-11-14 실측으로 확인한 사실은 references/api-notes.md에 있다.
"""
from __future__ import annotations

import collections
import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASE = "http://openapi.seoul.go.kr:8088"                   # HTTPS는 지원하지 않는다 (8088 포트 TLS 실패)
SUBWAY_BASE = "http://swopenapi.seoul.go.kr/api/subway"    # 실시간 지하철은 호스트와 키가 따로다
SITE = "https://data.seoul.go.kr"
SAMPLE_KEY = "sample"
SAMPLE_MAX = 5          # 샘플 키는 1~5행만 준다 (ERROR-335)
PAGE_MAX = 1000         # 한 번에 최대 1,000행 (ERROR-336)
USER_AGENT = "seoul-data-mcp/0.1 (+https://data.seoul.go.kr)"
KEY_GUIDE = ("인증키는 https://data.seoul.go.kr 에서 로그인 후 '인증키 신청'으로 무료 발급됩니다. "
             "받은 키를 ~/.config/seoul-data/.env 파일에 SEOUL_API_KEY=키 한 줄로 넣으세요.")

OK, EMPTY = "INFO-000", "INFO-200"

# 공식 메시지 표(명세 화면의 '메시지 설명')에 다음 행동을 덧붙인 것
HINTS = {
    "INFO-100": "인증키가 유효하지 않습니다. " + KEY_GUIDE,
    "ERROR-300": "필수 인자가 빠졌습니다. dataset_describe로 이 데이터셋의 필수 인자를 확인하세요.",
    "ERROR-301": "요청 파일 형식이 잘못됐습니다. 이 서버의 문제이니 그대로 알려 주세요.",
    "ERROR-310": "서비스명을 찾지 못했습니다. dataset_describe로 정확한 서비스명을 확인하세요.",
    "ERROR-331": "시작 위치가 잘못됐습니다. offset은 0 이상의 정수여야 합니다.",
    "ERROR-332": "종료 위치가 잘못됐습니다. limit은 1 이상의 정수여야 합니다.",
    "ERROR-333": "요청 위치는 정수여야 합니다.",
    "ERROR-334": "시작 위치가 종료 위치보다 큽니다.",
    "ERROR-335": "샘플 키는 처음 5행까지만 볼 수 있습니다. " + KEY_GUIDE,
    "ERROR-336": "한 번에 1,000행까지만 받을 수 있습니다. limit을 줄이세요.",
    "ERROR-338": ("이 인증키로는 실시간 지하철 서비스를 쓸 수 없습니다. 일반 인증키와 별도로 '실시간 지하철 인증키'를 "
                  "신청해 key_setup의 subway에 넣어야 합니다."),
    "ERROR-500": ("서울시 서버 오류입니다. 서비스명이 틀려도 이 코드가 나옵니다. "
                  "dataset_describe로 서비스명과 인자를 확인하고, 맞다면 잠시 뒤 다시 시도하세요."),
    "ERROR-600": "서울시 서버의 데이터베이스 연결 오류입니다. 잠시 뒤 다시 시도하세요.",
    "ERROR-601": "서울시 서버의 내부 질의 오류입니다. 인자 값의 형식(날짜 자릿수 등)을 dataset_describe로 확인하세요.",
}


class SeoulError(Exception):
    """호출이 실패했을 때. code는 서울시 코드이거나 NETWORK, PARSE 같은 이 서버의 분류다."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.hint = code, message, hint or HINTS.get(code, "")


@dataclass
class Parsed:
    code: str
    message: str
    total: int | None = None
    rows: list = field(default_factory=list)


# ── 키 ─────────────────────────────────────────────────────────────────────

def _env_files() -> list[Path]:
    home = Path(os.environ.get("SEOUL_DATA_HOME") or Path.home() / ".config" / "seoul-data")
    return [home / ".env", Path(__file__).resolve().parent.parent / ".env"]


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


# 사람마다 다르게 지어 둔 환경변수 이름도 받는다. 앞의 것이 정식 이름이다.
KEY_NAMES = {"SEOUL_API_KEY": ("SEOUL_API_KEY", "SEOUL_OPENAPI_KEY", "SEOUL_OPEN_API_KEY"),
             "SEOUL_SUBWAY_API_KEY": ("SEOUL_SUBWAY_API_KEY", "SEOUL_SUBWAY_KEY")}


def find_key(name: str = "SEOUL_API_KEY") -> str | None:
    """환경변수가 먼저, 그다음 .env 파일. 없으면 None (호출자가 샘플 키로 돈다)."""
    names = KEY_NAMES.get(name, (name,))
    for n in names:
        val = (os.environ.get(n) or "").strip()
        if val:
            return val
    for path in _env_files():
        found = _read_env_file(path)
        for n in names:
            val = found.get(n, "").strip()
            if val:
                return val
    return None


def redact(text: object, *keys: str | None) -> str:
    s = str(text)
    for k in keys:
        if k and k != SAMPLE_KEY:
            s = s.replace(k, "***")
    return s


# ── 캐시 ───────────────────────────────────────────────────────────────────

class Cache:
    """수명이 있는 메모리 캐시. 시계를 주입할 수 있어 테스트가 쉽다."""

    def __init__(self, clock: Callable[[], float] = time.time, max_items: int = 256) -> None:
        self._clock, self._max = clock, max_items
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        if hit[0] < self._clock():
            del self._data[key]
            return None
        return hit[1]

    def set(self, key: str, value: Any, ttl: float) -> Any:
        if len(self._data) >= self._max:          # 가장 먼저 만료될 것부터 버린다
            for k in sorted(self._data, key=lambda x: self._data[x][0])[: self._max // 4]:
                del self._data[k]
        self._data[key] = (self._clock() + ttl, value)
        return value

    def stored_at(self, key: str, ttl: float) -> float | None:
        hit = self._data.get(key)
        return None if hit is None else hit[0] - ttl


def cache_dir() -> Path:
    return Path(os.environ.get("SEOUL_DATA_CACHE_DIR") or Path.home() / ".cache" / "seoul-data")


def disk_get(name: str, ttl: float) -> Any | None:
    """디스크 캐시. 깨졌거나 오래됐으면 없는 것으로 친다."""
    path = cache_dir() / name
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def disk_set(name: str, value: Any) -> None:
    path = cache_dir() / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)                         # 쓰다 죽어도 반쪽 파일이 남지 않게
    except OSError:
        pass                                      # 캐시는 없어도 동작한다


def count_subway(add: int = 0) -> int:
    """오늘(서울 시각) 실시간 지하철 호출 수. 세션마다 서버가 따로 뜨므로 디스크에 센다."""
    import datetime as _dt
    today = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    data = disk_get("subway_calls.json", 3 * 24 * 3600)
    n = (data.get(today, 0) if isinstance(data, dict) else 0) + add
    if add:
        disk_set("subway_calls.json", {today: n})
    return n


# ── HTTP ───────────────────────────────────────────────────────────────────

def http_fetch(url: str, data: dict | None = None, timeout: float = 15, retries: int = 1,
               secrets: tuple = (), deadline: float | None = None) -> bytes:
    """기한이 있으면 별도 스레드에서 받아, 헤더가 한 줄씩 늦게 와도 기한에 끊는다."""
    if deadline is None:
        return _http_fetch(url, data, timeout, retries, secrets, None)
    import threading
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = _http_fetch(url, data, timeout, retries, secrets, deadline)
        except BaseException as e:               # noqa: BLE001  (스레드 밖으로 그대로 넘긴다)
            box["err"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(deadline - time.time(), 0) + 0.5)
    if t.is_alive():
        raise SeoulError("NETWORK", "서울시 서버에 연결하지 못했습니다: 시간 한도를 넘겼습니다",
                         "네트워크 상태를 확인하고 잠시 뒤 다시 시도하세요. 서울시 서버 점검 시간일 수 있습니다.")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _http_fetch(url: str, data: dict | None, timeout: float, retries: int,
                secrets: tuple, deadline: float | None) -> bytes:
    """GET 또는 POST. 네트워크 오류와 5xx는 한 번 더 해 본다. 오류 문장에서 키를 가린다.

    deadline(시각)이 있으면 남은 시간 안에서만 기다린다. 도구 하나가 기한을 넘겨 붙잡히지 않게 한다.
    """
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    last = ""
    for attempt in range(retries + 1):
        left = None if deadline is None else deadline - time.time()
        if left is not None and left < 1:
            last = last or "시간 한도를 넘겼습니다"
            break
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout if left is None else min(timeout, left)) as r:
                chunks = []
                while True:                           # 소켓 타임아웃은 recv 한 번 단위다. 본문이 찔끔찔끔 와도 기한을 지킨다
                    chunk = r.read1(65536)            # read()는 버퍼가 찰 때까지 막힌다. read1은 온 만큼만 돌려준다
                    if not chunk:
                        return b"".join(chunks)
                    chunks.append(chunk)
                    if deadline is not None and time.time() > deadline:
                        raise TimeoutError("응답 본문을 받다가 시간 한도를 넘겼습니다")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise SeoulError("NOT_FOUND", "주소를 찾지 못했습니다 (404).") from None
            last = f"HTTP {e.code}"
            if e.code < 500:
                break
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, http.client.HTTPException) as e:
            last = redact(getattr(e, "reason", e), *secrets)
        if attempt < retries:
            if deadline is not None and deadline - time.time() < 2:
                break                                 # 남은 시간이 없으면 다시 해 보지 않는다
            time.sleep(0.4)
    raise SeoulError("NETWORK", f"서울시 서버에 연결하지 못했습니다: {last}",
                     "네트워크 상태를 확인하고 잠시 뒤 다시 시도하세요. 서울시 서버 점검 시간일 수 있습니다.")


# ── URL과 응답 ─────────────────────────────────────────────────────────────

def _seg(value: object) -> str:
    """경로 조각 하나. 빈 값은 공백 한 칸으로 채워야 뒤 인자의 자리가 밀리지 않는다."""
    s = "" if value is None else str(value).strip()
    return urllib.parse.quote(s, safe="") if s else "%20"


def build_url(key: str, service: str, start: int, end: int, params: list | tuple = (),
              base: str = BASE) -> str:
    params = list(params)
    while params and (params[-1] is None or not str(params[-1]).strip()):
        params.pop()                              # 끝의 빈 인자는 붙이지 않는다
    parts = [base, urllib.parse.quote(key, safe=""), "json", urllib.parse.quote(service, safe="/"),   # realtimeStationArrival/ALL
             str(int(start)), str(int(end))] + [_seg(p) for p in params]
    return "/".join(parts) + ("/" if not params else "")


def _result(d: dict) -> tuple[str, str] | None:
    r = d.get("RESULT")
    if not isinstance(r, dict):
        return None
    code = r.get("CODE") or r.get("RESULT.CODE")
    return (str(code), str(r.get("MESSAGE") or r.get("RESULT.MESSAGE") or "")) if code else None


def _int(v: object) -> int | None:
    try:
        return int(v)       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse(raw: bytes) -> Parsed:
    """서울시 응답의 여러 모양을 하나로 읽는다. 읽지 못하면 PARSE 오류를 낸다 (빈 결과로 답하지 않는다)."""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            raise SeoulError("PARSE", "응답 JSON이 깨져 있습니다.", "잠시 뒤 다시 시도하세요.") from None
        if isinstance(d.get("errorMessage"), dict):           # 실시간 지하철 정상 응답
            em = d["errorMessage"]
            rows = next((v for k, v in d.items() if k != "errorMessage" and isinstance(v, list)), [])
            return Parsed(str(em.get("code")), str(em.get("message", "")), _int(em.get("total")), rows)
        if "code" in d and "message" in d and "status" in d:  # 실시간 지하철 오류 응답
            return Parsed(str(d["code"]), str(d["message"]), _int(d.get("total")))
        if "CITYDATA" in d:                                   # 실시간 도시데이터
            code, msg = _result(d) or (OK, "")
            return Parsed(code, msg, _int(d.get("list_total_count")) or 1, [d["CITYDATA"]])
        top = _result(d)
        if top and len(d) == 1:                               # 결과 코드만 있는 응답 (INFO-200, ERROR-…)
            return Parsed(top[0], top[1], 0)
        for v in d.values():                                  # 표준 봉투: 최상위 키 이름은 서비스명과 다를 수 있다
            if isinstance(v, dict) and ("row" in v or "RESULT" in v):
                code, msg = _result(v) or (OK, "")
                rows = v.get("row") or []
                return Parsed(code, msg, _int(v.get("list_total_count")), rows if isinstance(rows, list) else [rows])
        for v in d.values():                                  # 봉투 없이 목록만 오는 응답 (citydata_ppltn)
            if isinstance(v, list):
                return Parsed(OK, "", len(v), v)
        if top:
            return Parsed(top[0], top[1], 0)
        raise SeoulError("PARSE", "응답에서 데이터 행을 찾지 못했습니다.", "응답 구조가 바뀌었을 수 있습니다.")
    m = re.search(r"<CODE>\s*([A-Z]+-\d+)\s*</CODE>", text)   # JSON을 요청해도 일부 오류는 XML로 온다
    if m:
        msg = re.search(r"<MESSAGE>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</MESSAGE>", text, flags=re.S)
        return Parsed(m.group(1), re.sub(r"\s+", " ", msg.group(1)).strip() if msg else "", 0)
    raise SeoulError("PARSE", "응답을 읽지 못했습니다 (JSON도 XML 오류도 아닙니다).",
                     "서울시 서버가 점검 페이지를 돌려준 것일 수 있습니다. 잠시 뒤 다시 시도하세요.")


# ── 클라이언트 ─────────────────────────────────────────────────────────────

class Client:
    def __init__(self, key: str | None = None, subway_key: str | None = None,
                 fetch: Callable[..., bytes] = http_fetch, cache: Cache | None = None) -> None:
        found = key or find_key("SEOUL_API_KEY")
        self.key = found or SAMPLE_KEY
        # 실시간 지하철은 일반 인증키를 받지 않는다 (ERROR-338). 전용 키가 없으면 샘플 키로 돈다.
        self.subway_key = subway_key or find_key("SEOUL_SUBWAY_API_KEY") or SAMPLE_KEY
        self._fetch = fetch
        self.cache = cache or Cache()
        self.deadline: float | None = None       # 도구 하나가 쓸 수 있는 시각 한계 (run_tool이 정한다)

    @property
    def key_mode(self) -> str:
        return "sample" if self.key == SAMPLE_KEY else "user"

    @property
    def subway_key_mode(self) -> str:
        return "sample" if self.subway_key == SAMPLE_KEY else "user"

    def call(self, service: str, start: int, end: int, params: list | tuple = (),
             subway: bool = False) -> Parsed:
        """한 번 호출한다. INFO-000과 INFO-200(데이터 없음)은 정상, 나머지 코드는 SeoulError."""
        key, base = (self.subway_key, SUBWAY_BASE) if subway else (self.key, BASE)
        url = build_url(key, service, start, end, params, base)
        if subway and key != SAMPLE_KEY:
            count_subway(1)                       # 하루 1,000건 한도. 실패한 호출도 한도를 쓰므로 보내기 전에 센다
        try:
            parsed = parse(self._fetch(url, secrets=(self.key, self.subway_key), deadline=self.deadline))
        except SeoulError as e:
            raise SeoulError(e.code, redact(e.message, self.key, self.subway_key), e.hint) from None
        if parsed.code == EMPTY:
            return Parsed(EMPTY, parsed.message, 0, [])
        if parsed.code != OK:
            raise SeoulError(parsed.code, parsed.message)
        return parsed

    def rows(self, service: str, params: list | tuple = (), offset: int = 0, limit: int = 20,
             ttl: float = 0, stable: bool = False, identity: str | None = None) -> dict:
        """offset·limit 방식으로 행을 받는다. 1,000행이 넘으면 나눠 받고, 샘플 키면 5행 안으로 줄인다.

        stable=True면 여러 장을 받았을 때 겹친 행을 확인한다. 서울시 서버는 일부 데이터셋에서 장마다 행 순서를 바꿔
        돌려줘, 그대로 이으면 같은 행이 두 번 들어오고 그만큼 다른 행이 빠진다 (종로구 음식점 20,613행 중 약 3,500행).
        겹침이 보이면 시간이 되는 만큼 다시 받아 서로 다른 행을 모은다.

        돌려주는 것: {"total", "rows", "sample_capped", "partial", "ended", "dups", "missing", "passes"}
        """
        if stable:
            return self._stable_rows(service, params, offset, limit, ttl, identity)
        offset, limit = max(int(offset), 0), max(int(limit), 1)
        capped = False
        if self.key_mode == "sample":
            if offset >= SAMPLE_MAX:
                raise SeoulError("ERROR-335", "샘플 키로는 처음 5행까지만 볼 수 있습니다.")
            if offset + limit > SAMPLE_MAX:
                limit, capped = SAMPLE_MAX - offset, True
        ckey = f"rows|{service}|{'/'.join(map(str, params))}|{offset}|{limit}"
        if ttl:
            hit = self.cache.get(ckey)
            if hit is not None:
                return hit
        total: int | None = None                  # 믿을 수 있는 총계만 담는다. 모르면 None
        out: list = []
        partial = ended = False
        why = "time"
        start = offset + 1
        took = 0.0
        while len(out) < limit:
            if out and self.deadline and time.time() + took > self.deadline:
                partial = True                    # 다음 장을 받을 시간이 없으면 받은 데까지만 돌려주고 그 사실을 남긴다
                break
            end = start + min(PAGE_MAX, limit - len(out)) - 1
            t0 = time.time()
            try:
                page = self.call(service, start, end, params)
            except SeoulError as e:
                if out and e.code in ("NETWORK", "PARSE"):   # 이미 받은 행을 버리지 않는다 (끊김, 점검 페이지)
                    partial, why = True, "error"
                    break
                raise
            took = time.time() - t0
            out.extend(page.rows)
            # 일부 서비스(bikeList)는 list_total_count에 전체가 아니라 이번 장의 행 수를 넣는다.
            # 이번 장보다 클 때만 총계로 믿는다.
            if page.total is not None and page.total > len(page.rows):
                total = page.total
            if len(page.rows) < end - start + 1:  # 덜 찬 장이 오면 거기가 끝이다
                ended = True
                break
            if total is not None and end >= total:
                ended = True
                break
            start = end + 1
        # 딱 limit만큼 왔고 총계를 모르면, 다음 한 행을 불러 끝인지 확인한다 (25행짜리를 25행으로 받는 경우).
        if out and not ended and not partial and total is None and not capped \
                and not (self.key_mode == "sample" and offset + len(out) >= SAMPLE_MAX):
            try:
                nxt = offset + len(out) + 1
                if not self.call(service, nxt, nxt, params).rows:
                    ended = True
            except SeoulError:
                pass                              # 확인하지 못하면 모르는 채로 둔다
        if ended and total is None and (out or offset == 0):
            total = offset + len(out)             # 끝까지 받았으니 이것이 전체다
        res = {"total": total, "rows": out, "sample_capped": capped, "partial": partial, "ended": ended,
               "partial_why": why if partial else None}
        return self.cache.set(ckey, res, ttl) if ttl and not partial else res

    STABLE_PASSES = 5        # 겹침을 맞추려고 다시 받는 횟수의 상한. 서울시 서버에 주는 부담을 묶어 둔다

    def _stable_rows(self, service: str, params: list | tuple, offset: int, limit: int, ttl: float,
                     identity: str | None = None) -> dict:
        """여러 장을 받을 때 서버가 장마다 행 순서를 바꿔 생기는 겹침과 누락을 다룬다.

        판정 순서
        1. 한 장이거나 일부 구간이면 그대로 돌려준다.
        2. 장 사이에 겹친 행이 없으면 그대로 돌려준다.
        3. 겹친 행이 있으면 한 번 더 받는다. 순서가 처음과 똑같으면 서버는 순서를 바꾸지 않은 것이고,
           겹쳐 보인 행은 내용이 같은 정상 행이다(장 경계에 걸친 경우). 그대로 돌려준다.
        4. 순서가 다르면 흔들리는 데이터다. 한 장 안에서 같은 행이 나온 횟수를 그 행의 개수로 삼아
           (한 장은 한 시점의 일관된 결과다) 서로 다른 행을 모으고, 총계와 같아질 때까지 다시 받는다.
        5. 모은 행이 총계보다 많아지면 받는 사이에 값이 바뀌는 데이터다. 맞출 수 없으니 그렇다고 알린다.

        identity(식별 필드 이름)를 주면 그 값으로 행을 구분하고, 같은 식별값은 나중에 받은 내용으로 덮는다.
        """
        ckey = f"stable|{service}|{'/'.join(map(str, params))}|{offset}|{limit}|{identity}"
        if ttl:
            hit = self.cache.get(ckey)
            if hit is not None:
                return hit
        t0 = time.time()
        got = self.rows(service, params, offset, limit)
        spent = time.time() - t0
        rows, want = got["rows"], len(got["rows"])
        whole = got["ended"] and offset == 0 and not got["partial"]
        key = (lambda r: str(r.get(identity))) if identity else \
              (lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
        clean = {**got, "dups": 0, "missing": 0, "passes": 1, "changing": False}
        if want <= PAGE_MAX or not whole:
            if want > PAGE_MAX:                           # 일부 구간만 받았다. 겹침은 보여도 무엇이 빠졌는지는 알 수 없다
                seen = len({key(r) for r in rows})
                clean.update(dups=want - seen, missing=None if want != seen else 0)
            return self.cache.set(ckey, clean, ttl) if ttl and not got["partial"] else clean
        mult: dict[str, int] = {}
        sample: dict[str, Any] = {}

        def absorb(batch: list) -> None:
            for i in range(0, len(batch), PAGE_MAX):
                for k, n in collections.Counter(key(r) for r in batch[i:i + PAGE_MAX]).items():
                    mult[k] = 1 if identity else max(mult.get(k, 0), n)
            for r in batch:
                if identity or key(r) not in sample:
                    sample[key(r)] = r

        absorb(rows)
        cross = want - sum(mult.values())                 # 장 사이에서 겹친 행 수
        if cross <= 0:
            return self.cache.set(ckey, clean, ttl) if ttl else clean
        first_order = [key(r) for r in rows]
        passes = 1
        while passes < self.STABLE_PASSES and sum(mult.values()) < want:
            if self.deadline and time.time() + spent * 1.1 > self.deadline:
                break
            time.sleep(0.05)                              # 연달아 다시 받을 때 서버에 숨 돌릴 틈을 준다
            try:
                again = self.rows(service, params, offset, limit)
            except SeoulError:
                break
            if again["partial"] or not again["ended"]:
                break
            passes += 1
            if passes == 2 and not identity and [key(r) for r in again["rows"]] == first_order:
                out = {**clean, "passes": 2}              # 순서가 그대로다. 겹쳐 보인 것은 내용이 같은 정상 행이다
                return self.cache.set(ckey, out, ttl) if ttl else out
            absorb(again["rows"])
        found = sum(mult.values())
        if found > want and not identity:                 # 총계보다 많다. 받는 사이 값이 바뀌어 같은 행이 다른 행으로 보인 것이다
            out = {**got, "dups": cross, "missing": None, "passes": passes, "changing": True}
            return out
        out = {**got, "rows": [sample[k] for k, n in mult.items() for _ in range(n)], "passes": passes,
               "dups": cross, "missing": max(want - found, 0), "changing": False}
        return self.cache.set(ckey, out, ttl) if ttl and not out["missing"] else out

    def all_rows(self, service: str, params: list | tuple = (), ttl: float = 60, max_rows: int = 5000,
                 identity: str | None = None) -> dict:
        """전체 행을 받아 캐시한다 (따릉이처럼 전체를 받아 거르는 도구용). 겹친 행은 다시 받아 맞춘다."""
        return self.rows(service, params, 0, max_rows, ttl, stable=True, identity=identity)
