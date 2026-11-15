#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seoul-data 단위 테스트. 네트워크 없이 돈다.

    python3 -m unittest discover -s tests -v

fixtures/ 의 파일은 2026-11-14에 샘플 키로 받은 실제 응답이다 (citydata.json만 긴 목록을 앞 2~3건으로 줄였다).
샘플 키로는 볼 수 없는 동작(1,000행 넘는 이어 받기, 집계, 121개 장소)은 가짜 응답으로 검증한다.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "assets"))

_tmp = tempfile.mkdtemp(prefix="seoul-data-test-")
os.environ["SEOUL_DATA_CACHE_DIR"] = str(Path(_tmp) / "cache")
os.environ["SEOUL_DATA_HOME"] = str(Path(_tmp) / "home")
for _name in ("SEOUL_API_KEY", "SEOUL_OPENAPI_KEY", "SEOUL_OPEN_API_KEY", "SEOUL_SUBWAY_API_KEY", "SEOUL_SUBWAY_KEY"):
    os.environ.pop(_name, None)                    # 테스트는 사용자의 실제 키를 쓰지 않는다

import catalog                      # noqa: E402
import mcp_server as m              # noqa: E402
import places                       # noqa: E402
import seoul_client as sc           # noqa: E402

KEY, SUBKEY = "REALKEY1234567890", "SUBWAYKEY0987654321"
REAL_AGE = m.age_sec


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def envelope(service: str, rows: list, total: int | None = None) -> bytes:
    return json.dumps({service: {"list_total_count": len(rows) if total is None else total,
                                 "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"}, "row": rows}},
                      ensure_ascii=False).encode()


class FakeApi:
    """경로를 풀어 서비스별 처리기로 넘기는 가짜 서울시 서버. 부른 URL을 calls에 남긴다."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handlers: dict = {}

    def __call__(self, url: str, data=None, secrets=(), **kw) -> bytes:
        self.calls.append(url)
        if "data.seoul.go.kr" in url:
            return self.site(url, data)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(url).path.strip("/").split("/")]
        if parts[:2] == ["api", "subway"]:
            parts = parts[2:]
        key, _typ, service, start, end, *params = parts
        if key not in (KEY, SUBKEY, "sample"):
            return fx("info100.xml")
        if key == "sample" and int(end) > 5:
            return fx("error335.xml")
        h = self.handlers.get(service)
        return h(int(start), int(end), params) if h else fx("error500.json")

    def site(self, url: str, data) -> bytes:
        if getattr(self, "site_handler", None):
            return self.site_handler(url, data)
        if "openApiView" in url:
            for ds in ("OA-21275", "OA-15486", "OA-21285"):
                if ds in url:
                    return fx(f"spec_{ds}.html")
            raise sc.SeoulError("NOT_FOUND", "404")
        kw = (data or {}).get("basickwd", "")
        return fx({"따릉이": "search_bike.html", "주민등록인구": "search_stat.html"}.get(kw, "search_empty.html"))


def table(service: str, rows: list, honest_total: bool = True):
    """rows를 start~end로 잘라 주는 처리기. honest_total=False면 bikeList처럼 총계 자리에 받은 행 수를 넣는다."""
    def h(start: int, end: int, params: list) -> bytes:
        page = rows[start - 1:end]
        if not page:
            return fx("info200.json")
        return envelope(service, page, len(rows) if honest_total else len(page))
    return h


class Base(unittest.TestCase):
    key: str | None = KEY

    def setUp(self) -> None:
        self.api = FakeApi()
        m.set_client(sc.Client(key=self.key, subway_key=SUBKEY if self.key else None, fetch=self.api))
        m.site_fetch = self.api
        for f in (Path(_tmp) / "cache").rglob("*.json"):
            f.unlink()

    def tearDown(self) -> None:
        m.set_client(None)
        m.age_sec = REAL_AGE

    def call(self, tool: str, **args):
        return m.run_tool(tool, args)

    def ok(self, tool: str, **args) -> dict:
        body, err = self.call(tool, **args)
        self.assertFalse(err, body)
        return body

    def bad(self, tool: str, code: str, **args) -> dict:
        body, err = self.call(tool, **args)
        self.assertTrue(err, body)
        self.assertEqual(body["error"]["code"], code, body)
        self.assertTrue(body["error"].get("next"), "오류에는 다음 행동이 있어야 한다")
        return body


class SampleBase(Base):
    key = None


# ── 응답 파서와 URL ────────────────────────────────────────────────────────

class ParseTest(unittest.TestCase):
    def test_real_responses(self) -> None:
        want = {"bikeList.json": ("INFO-000", 5), "RealtimeCityAir.json": ("INFO-000", 5),
                "tbLnOpendataRtmsV_filtered.json": ("INFO-000", 5), "culturalEventInfo.json": ("INFO-000", 3),
                "citydata.json": ("INFO-000", 1), "subway_arrival.json": ("INFO-000", 5),
                "info200.json": ("INFO-200", 0), "error500.json": ("ERROR-500", 0), "error334.json": ("ERROR-334", 0),
                "subway_error.json": ("ERROR-336", 0)}
        for name, (code, n) in want.items():
            p = sc.parse(fx(name))
            self.assertEqual((p.code, len(p.rows)), (code, n), name)

    def test_xml_errors_when_json_requested(self) -> None:
        self.assertEqual(sc.parse(fx("error335.xml")).code, "ERROR-335")
        p = sc.parse(fx("info100.xml"))
        self.assertEqual(p.code, "INFO-100")
        self.assertIn("인증키", p.message)

    def test_envelope_key_differs_from_service(self) -> None:
        self.assertIn(b"rentBikeStatus", fx("bikeList.json"))          # 서비스명은 bikeList
        self.assertEqual(len(sc.parse(fx("bikeList.json")).rows), 5)

    def test_total(self) -> None:
        self.assertEqual(sc.parse(fx("tbLnOpendataRtmsV_filtered.json")).total, 4295)
        self.assertEqual(sc.parse(fx("subway_arrival.json")).total, 21)     # errorMessage.total

    def test_garbage_is_an_error_not_empty(self) -> None:
        for raw in (b"<html>maintenance</html>", b"", b"{broken"):
            with self.assertRaises(sc.SeoulError) as cm:
                sc.parse(raw)
            self.assertEqual(cm.exception.code, "PARSE")

    def test_build_url(self) -> None:
        self.assertEqual(sc.build_url("K", "bikeList", 1, 5), "http://openapi.seoul.go.kr:8088/K/json/bikeList/1/5/")
        url = sc.build_url("K", "svc", 1, 5, ["2026", None, "강남구", None, ""])
        self.assertTrue(url.endswith("/svc/1/5/2026/%20/%EA%B0%95%EB%82%A8%EA%B5%AC"), url)   # 빈 자리는 공백, 끝의 빈 인자는 버림
        self.assertIn("%2F", sc.build_url("K", "svc", 1, 5, ["a/b"]))

    def test_redact(self) -> None:
        self.assertEqual(sc.redact(f"http://x/{KEY}/json", KEY), "http://x/***/json")
        self.assertEqual(sc.redact("http://x/sample/json", "sample"), "http://x/sample/json")


class PagingTest(Base):
    def rows(self, n: int) -> list:
        return [{"i": str(i)} for i in range(1, n + 1)]

    def test_pages_over_1000(self) -> None:
        self.api.handlers["big"] = table("big", self.rows(2345))
        got = m.client().rows("big", (), 0, 5000)
        self.assertEqual((len(got["rows"]), got["total"]), (2345, 2345))
        self.assertEqual(len(self.api.calls), 3)
        self.assertIn("/big/1/1000", self.api.calls[0])
        self.assertIn("/big/2001/3000", self.api.calls[2])

    def test_total_that_lies(self) -> None:
        """bikeList는 list_total_count에 이번 장의 행 수를 넣는다. 그 값으로 멈추면 1,000건에서 끊긴다."""
        self.api.handlers["bikeList"] = table("bikeList", self.rows(2100), honest_total=False)
        got = m.client().rows("bikeList", (), 0, 5000)
        self.assertEqual(len(got["rows"]), 2100)
        self.assertEqual(got["total"], 2100)

    def test_offset_limit(self) -> None:
        self.api.handlers["big"] = table("big", self.rows(50))
        got = m.client().rows("big", (), 10, 5)
        self.assertEqual([r["i"] for r in got["rows"]], ["11", "12", "13", "14", "15"])
        self.assertIn("/big/11/15", self.api.calls[0])

    def test_sample_key_caps_at_5(self) -> None:
        m.set_client(sc.Client(fetch=self.api))
        self.api.handlers["big"] = table("big", self.rows(50))
        got = m.client().rows("big", (), 0, 20)
        self.assertEqual(len(got["rows"]), 5)
        self.assertTrue(got["sample_capped"])
        with self.assertRaises(sc.SeoulError) as cm:
            m.client().rows("big", (), 5, 5)
        self.assertEqual(cm.exception.code, "ERROR-335")

    def test_deadline_returns_partial(self) -> None:
        self.api.handlers["big"] = table("big", self.rows(5000))
        m.client().deadline = 1.0                       # 이미 지난 시각
        got = m.client().rows("big", (), 0, 5000)
        self.assertTrue(got["partial"])
        self.assertEqual(len(got["rows"]), 1000)

    def test_key_never_leaks_in_network_error(self) -> None:
        def boom(url, **kw):
            raise sc.SeoulError("NETWORK", f"연결 실패 {url}")
        m.set_client(sc.Client(key=KEY, fetch=boom))
        body, err = m.run_tool("dataset_query", {"service": "bikeList"})
        self.assertTrue(err)
        self.assertNotIn(KEY, json.dumps(body, ensure_ascii=False))
        self.assertIn("***", body["error"]["message"])


# ── 카탈로그 ───────────────────────────────────────────────────────────────

class CatalogTest(unittest.TestCase):
    def test_search(self) -> None:
        r = catalog.parse_search(fx("search_bike.html").decode())
        self.assertEqual((r["total"], len(r["items"])), (21, 10))
        first = r["items"][0]
        self.assertEqual((first["dataset"], first["category"], first["has_api"]), ("OA-21235", "교통", True))
        self.assertFalse(any(i["title"].endswith("AI친화") for i in r["items"]))

    def test_search_empty_and_stat(self) -> None:
        self.assertEqual(catalog.parse_search(fx("search_empty.html").decode()), {"total": 0, "items": []})
        stat_items = [i for i in catalog.parse_search(fx("search_stat.html").decode())["items"] if i["kind"] == "통계"]
        self.assertTrue(stat_items)
        self.assertTrue(all(not i["has_api"] and i["dataset"].startswith("DT") for i in stat_items))

    def test_search_page_changed(self) -> None:
        with self.assertRaises(sc.SeoulError) as cm:
            catalog.parse_search("<html>새 디자인</html>")
        self.assertEqual(cm.exception.code, "SITE_CHANGED")

    def test_spec(self) -> None:
        s = catalog.parse_spec(fx("spec_OA-21275.html").decode(), "OA-21275")
        self.assertEqual((s["service"], s["host"], s["first_index"]), ("tbLnOpendataRtmsV", "general", 1))
        self.assertEqual([p["name"] for p in s["params"]][:3], ["RCPT_YR", "CGG_CD", "CGG_NM"])
        self.assertEqual({f["name"]: f["description"] for f in s["fields"]}["THING_AMT"], "물건금액(만원)")
        city = catalog.parse_spec(fx("spec_OA-21285.html").decode(), "OA-21285")
        self.assertEqual([(p["name"], p["required"]) for p in city["params"]], [("AREA_NM", True)])

    def test_spec_page_changed(self) -> None:
        with self.assertRaises(sc.SeoulError) as cm:
            catalog.parse_spec("<html><table><caption>다른 표</caption></table></html>", "OA-1")
        self.assertEqual(cm.exception.code, "SITE_CHANGED")

    def test_order_params(self) -> None:
        s = catalog.parse_spec(fx("spec_OA-21275.html").decode(), "OA-21275")
        got = catalog.order_params(s, {"cgg_nm": "강남구", "RCPT_YR": 2026})
        self.assertEqual(got[:3], [2026, None, "강남구"])
        with self.assertRaises(sc.SeoulError):
            catalog.order_params(s, {"NOPE": "x"})
        city = catalog.parse_spec(fx("spec_OA-21285.html").decode(), "OA-21285")
        with self.assertRaises(sc.SeoulError):
            catalog.order_params(city, {})                # 필수 AREA_NM 누락

    def test_stale_spec_when_site_down(self) -> None:
        api = FakeApi()
        fresh = catalog.get_spec("OA-15486", fetch=api)
        self.assertNotIn("stale", fresh)
        path = sc.cache_dir() / "spec" / "OA-15486.json"
        os.utime(path, (1, 1))                            # 캐시를 아주 오래된 것으로 만든다

        def down(url, **kw):
            raise sc.SeoulError("NETWORK", "연결 실패")
        again = catalog.get_spec("OA-15486", fetch=down)
        self.assertTrue(again["stale"])
        self.assertEqual(again["service"], "culturalEventInfo")

    def test_broken_cache_is_a_miss(self) -> None:
        path = sc.cache_dir() / "spec" / "OA-21275.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{반쪽", encoding="utf-8")
        self.assertEqual(catalog.get_spec("OA-21275", fetch=FakeApi())["service"], "tbLnOpendataRtmsV")


class PlacesTest(unittest.TestCase):
    def test_list(self) -> None:
        self.assertEqual((len(places.ALL), len(set(places.ALL))), (121, 121))
        self.assertTrue(set(places.ALIASES.values()) <= set(places.ALL))

    def test_resolve(self) -> None:
        for q, want in {"강남역": "강남역", "성수동": "성수카페거리", "코엑스": "강남 MICE 관광특구", "ddp": "DDP(동대문디자인플라자)",
                        "광화문 덕수궁": "광화문·덕수궁", "서울숲": "서울숲공원", "홍대입구역": "홍대입구역(2호선)",
                        "홍대": "홍대 관광특구", "덕수궁": "광화문·덕수궁", "올림픽": "올림픽공원"}.items():
            self.assertEqual(places.resolve(q)[0], want, q)
        name, cands = places.resolve("광화문")
        self.assertIsNone(name)
        self.assertEqual(set(cands), {"광화문·덕수궁", "광화문광장"})
        self.assertEqual(places.resolve("판교"), (None, []))

    def test_never_confirms_a_wrong_place(self) -> None:
        """자치구나 동 이름이 글자가 겹친다는 이유로 엉뚱한 장소로 확정되면 안 된다."""
        for q in ("양천구", "강서구", "영등포구", "마포구", "종로구", "강남구"):
            self.assertEqual(places.resolve(q), (None, []), q)          # 양천구가 안양천으로 읽히면 안 된다
        for q, cand in {"망원동": "망원한강공원", "뚝섬공원": "뚝섬한강공원", "양천": "안양천", "강서": "강서한강공원",
                        "망원": "망원한강공원", "서대문": "서대문독립공원", "김포": "김포공항", "성수둥": "성수카페거리"}.items():
            name, cands = places.resolve(q)
            self.assertIsNone(name, q)                                   # 짧게 겹치거나 꼬리를 떼야 맞거나 오타면 후보로만
            self.assertIn(cand, cands, q)
        self.assertTrue(places.resolve("성수카페거린")[1])               # 오타에는 비슷한 이름을 후보로 준다


# ── 도구 ───────────────────────────────────────────────────────────────────

def deals() -> list:
    """부동산 실거래 모양의 가짜 행 1,200건. 아파트 800건(대부분 10억), 오피스텔 400건(3억). 24건은 취소 거래."""
    out = []
    for i in range(1200):
        apt = i % 3 != 0
        price = (200000 if i % 100 == 1 else 100000) if apt else 30000      # 아파트 8건만 20억: 평균과 중앙값이 갈린다
        out.append({"RCPT_YR": "2026", "CGG_NM": "마포구", "BLDG_NM": f"건물{i}", "CTRT_DAY": f"2026{(i % 9) + 1:02d}15",
                    "THING_AMT": str(price), "ARCH_AREA": 84.9, "BLDG_USG": "아파트" if apt else "오피스텔",
                    "MNO": "0016", "RTRCN_DAY": "20260301" if i % 50 == 0 else ""})
    return out


class QueryTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.api.handlers["tbLnOpendataRtmsV"] = table("tbLnOpendataRtmsV", deals())

    def test_filters_become_path_positions(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", filters={"CGG_NM": "마포구"}, limit=3)
        self.assertTrue(self.api.calls[-1].endswith("/tbLnOpendataRtmsV/1/3/%20/%20/%EB%A7%88%ED%8F%AC%EA%B5%AC"))
        self.assertEqual((b["meta"]["total"], b["meta"]["returned"], b["meta"]["next_offset"]), (1200, 3, 3))
        self.assertEqual(b["meta"]["filters"], {"CGG_NM": "마포구"})
        self.assertEqual(b["fields"]["THING_AMT"], "물건금액(만원)")              # 단위는 명세의 설명으로 붙는다
        self.assertEqual(b["common"]["CGG_NM"], "마포구")                         # 모든 행에 같은 값은 한 번만
        self.assertEqual(b["common"]["MNO"], "0016")                              # 문자열 숫자를 바꾸지 않는다
        self.assertEqual(b["meta"]["filters_checked"], {"CGG_NM": "반영됨"})
        self.assertIn("RTRCN_DAY", b["rows"][0])                                  # 0번 행은 취소 거래라 값이 있다
        self.assertNotIn("RTRCN_DAY", b["rows"][1])                               # 빈 값은 싣지 않는다

    def test_aggregate(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", filters={"CGG_NM": "마포구"}, group_by=["BLDG_USG"],
                    metrics=["count", "avg:THING_AMT", "median:THING_AMT"])
        got = {r["BLDG_USG"]: r for r in b["rows"]}
        apts = [int(d["THING_AMT"]) for d in deals() if d["BLDG_USG"] == "아파트"]
        self.assertEqual((got["아파트"]["count"], got["아파트"]["avg_THING_AMT"]), (800, round(sum(apts) / 800, 2)))
        self.assertEqual(got["아파트"]["median_THING_AMT"], 100000.0)
        self.assertNotEqual(got["아파트"]["avg_THING_AMT"], got["아파트"]["median_THING_AMT"])     # 두 함수를 구분하는 데이터
        self.assertEqual((got["오피스텔"]["count"], got["오피스텔"]["median_THING_AMT"]), (400, 30000.0))
        self.assertEqual(b["fields"]["THING_AMT"], "물건금액(만원)")                                 # 집계 값의 단위도 붙는다
        self.assertTrue(b["meta"]["scan_complete"])
        self.assertEqual(b["meta"]["scanned"], 1200)

    def test_where_range_and_contains(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", where=["CTRT_DAY>=20260801", "BLDG_USG~아파"], metrics=["count"])
        want = sum(1 for d in deals() if d["CTRT_DAY"] >= "20260801" and d["BLDG_USG"] == "아파트")
        self.assertEqual(b["rows"][0]["count"], want)
        b = self.ok("dataset_query", dataset="OA-21275", where=["BLDG_USG=오피스텔"], limit=2)
        self.assertEqual((b["meta"]["matched"], b["meta"]["returned"], b["meta"]["next_offset"]), (400, 2, 2))

    def test_where_dates_codes_and_blanks(self) -> None:
        rows = [{"T": "가", "STRTDATE": "2026-11-15 00:00:00.0", "STAMP": "202611142000", "MNO": "0016", "CANCEL": ""},
                {"T": "나", "STRTDATE": "2026-11-16 00:00:00.0", "STAMP": "202611152000", "MNO": "16", "CANCEL": "20261101"}]
        self.api.handlers["x"] = table("x", rows)
        q = lambda *w: [r["T"] for r in self.ok("dataset_query", service="x", where=list(w), fields=["T"])["rows"]]   # noqa: E731
        self.assertEqual(q("STRTDATE<=2026-11-15"), ["가"])              # 날짜는 조건 길이만큼만 본다
        self.assertEqual(q("STRTDATE=2026-11"), ["가", "나"])
        self.assertEqual(q("STAMP>=20261115"), ["나"])                   # 12자리 시각과 8자리 날짜
        self.assertEqual(q("MNO=16"), ["나"])                            # '0016'은 16이 아니다
        self.assertEqual(q("CANCEL="), ["가"])                           # 빈 값 찾기 (취소되지 않은 거래)
        self.assertEqual(q("CANCEL!="), ["나"])

    def test_korean_field_names_in_where(self) -> None:
        self.api.handlers["x"] = table("x", [{"자치구": "마포구", "값": "3"}, {"자치구": "중구", "값": "5"}])
        self.assertEqual(self.ok("dataset_query", service="x", where=["자치구=중구"])["meta"]["matched"], 1)

    def test_sort(self) -> None:
        air = [{"MSRSTN_NM": n, "PM": pm} for n, pm in (("중구", 18.0), ("강남구", 41.0), ("마포구", 7.0), ("은평구", ""))]
        self.api.handlers["RealtimeCityAir"] = table("RealtimeCityAir", air)
        b = self.ok("dataset_query", service="RealtimeCityAir", sort="-PM")
        self.assertEqual([r["MSRSTN_NM"] for r in b["rows"]], ["강남구", "중구", "마포구", "은평구"])   # 큰 순, 빈 값은 맨 뒤
        b = self.ok("dataset_query", dataset="OA-21275", group_by=["BLDG_USG"], metrics=["avg:THING_AMT"], sort="avg_THING_AMT")
        self.assertEqual([r["BLDG_USG"] for r in b["rows"]], ["오피스텔", "아파트"])
        self.bad("dataset_query", "BAD_INPUT", service="RealtimeCityAir", sort="-NOPE")

    def test_many_groups_are_cut_with_notice(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", group_by=["BLDG_NM"], limit=10)
        self.assertEqual((b["meta"]["groups"], len(b["rows"])), (1200, 10))
        self.assertTrue(any("묶음 1200개 중 1~10번째" in n for n in b["notices"]))
        nxt = self.ok("dataset_query", dataset="OA-21275", group_by=["BLDG_NM"], limit=10, offset=10)
        self.assertNotEqual(nxt["rows"][0], b["rows"][0])                           # 묶음에도 offset이 먹는다

    def test_typo_in_group_by_or_metric_is_rejected(self) -> None:
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", group_by=["BLDG_USGG"])
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", metrics=["avg:THING_AMTT"])

    def test_range_conditions_exclude_blanks(self) -> None:
        """빈 값이 글자 비교로 넘어가 '20260401보다 작다'에 걸리면 취소 24건이 1,200건이 된다."""
        b = self.ok("dataset_query", dataset="OA-21275", where=["RTRCN_DAY<20260401"], metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 24)
        b = self.ok("dataset_query", dataset="OA-21275", where=["RTRCN_DAY>=20260101"], metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 24)

    def test_exact_page_is_probed_for_the_end(self) -> None:
        """25행짜리를 limit 25로 받으면 총계 자리가 25라 믿을 수 없다. 한 행 더 불러 끝임을 확인한다."""
        self.api.handlers["RealtimeCityAir"] = table("RealtimeCityAir", [{"N": str(i)} for i in range(25)])
        b = self.ok("dataset_query", service="RealtimeCityAir", limit=25)
        self.assertEqual((b["meta"]["total"], b["meta"]["next_offset"]), (25, None))
        self.assertNotIn("notices", b)
        self.assertTrue(self.ok("dataset_query", service="RealtimeCityAir", metrics=["count"], scan=25)["meta"]["scan_complete"])

    def test_last_chunk_wording_and_merge_rule(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", metrics=["avg:THING_AMT"], scan=1000, scan_offset=1000)
        self.assertTrue(any("마지막 조각" in n and "중앙값은 조각끼리 합칠 수 없습니다" in n for n in b["notices"]), b["notices"])

    def test_slow_server_keeps_what_it_got(self) -> None:
        rows = [{"i": str(i)} for i in range(5000)]
        calls = {"n": 0}

        def flaky(start, end, params):
            calls["n"] += 1
            if calls["n"] >= 4:
                raise sc.SeoulError("NETWORK", "시간 한도를 넘겼습니다")
            return envelope("big", rows[start - 1:end], len(rows))
        self.api.handlers["big"] = flaky
        b = self.ok("dataset_query", service="big", metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 3000)                               # 받은 3,000행을 버리지 않는다
        self.assertFalse(b["meta"]["scan_complete"])
        self.assertTrue(any("그때까지 받은 데까지만" in n for n in b["notices"]))        # 시간 탓이 아니라 서버 오류였다고 말한다

    def test_english_variants_of_citydata_are_blocked_too(self) -> None:
        for svc in ("citydata_eng", "citydata_ppltn_eng", "CITYDATA_CMRCL", "citydata_anything"):
            self.bad("dataset_query", "USE_TOOL", service=svc, path_args=["성수카페거리"])
        self.assertEqual(self.api.calls, [])

    def test_dedicated_services_are_not_served_by_the_generic_tool(self) -> None:
        """citydata를 범용 도구로 부르면 체험 모드 차단과 크기 줄이기를 건너뛴다. 전용 도구로 돌려보낸다."""
        b = self.bad("dataset_query", "USE_TOOL", dataset="OA-21285", filters={"AREA_NM": "성수카페거리"})
        self.assertEqual(b["tool"], "place_now")
        self.bad("dataset_query", "USE_TOOL", service="CityData", path_args=["강남역"])
        self.bad("dataset_query", "USE_TOOL", service="realtimeStationArrival", path_args=["서울"])
        self.assertFalse(any("/citydata/" in u.lower() for u in self.api.calls))
        d = self.ok("dataset_describe", dataset="OA-21285")
        self.assertEqual(d["example"]["tool"], "place_now")

    def test_unknown_total_never_claims_complete(self) -> None:
        """총계 자리에 받은 행 수를 넣는 서비스에서 '다 봤다'거나 '전체 N건'이라고 말하면 안 된다."""
        self.api.handlers["bikeList"] = table("bikeList", [{"i": str(i)} for i in range(2500)], honest_total=False)
        b = self.ok("dataset_query", service="bikeList", metrics=["count"], scan=1000)
        self.assertFalse(b["meta"]["scan_complete"])
        self.assertIsNone(b["meta"]["total"])
        self.assertTrue(any("전체 건수를 알 수 없는" in n for n in b["notices"]))
        b = self.ok("dataset_query", service="bikeList", offset=2, limit=3)
        self.assertEqual((b["meta"]["total"], b["meta"]["next_offset"]), (None, 5))
        b = self.ok("dataset_query", service="bikeList", metrics=["count"])          # 끝까지 받으면 그때는 안다
        self.assertEqual((b["meta"]["scan_complete"], b["meta"]["total"], b["rows"][0]["count"]), (True, 2500, 2500))

    def test_trimmed_where_result_points_to_the_right_offset(self) -> None:
        fat = [{"N": str(i), "MEMO": f"{i}" + "가" * 480} for i in range(200)]
        self.api.handlers["x"] = table("x", fat)
        b = self.ok("dataset_query", service="x", where=["N>=0"], limit=200)
        self.assertTrue(b["meta"]["truncated"])
        self.assertEqual(b["meta"]["next_offset"], b["meta"]["returned"])           # 200이 아니라 실제로 실은 곳 다음
        nxt = self.ok("dataset_query", service="x", where=["N>=0"], limit=200, offset=b["meta"]["next_offset"])
        self.assertEqual(nxt["rows"][0]["N"], str(b["meta"]["returned"]))

    def test_one_huge_row_still_respects_the_limit(self) -> None:
        huge = [{"NAME": "하나", "LINKS": [{"XY": "1" * 300, "ID": i} for i in range(400)]}]
        self.api.handlers["x"] = table("x", huge)
        b = self.ok("dataset_query", service="x")
        self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), m.MAX_CHARS)
        self.assertTrue(any("안쪽 목록은 5건까지" in n for n in b["notices"]))
        self.assertEqual(b["rows"][0]["NAME"], "하나")

    def test_incomplete_scan_is_announced(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", metrics=["count"], scan=1000)
        self.assertFalse(b["meta"]["scan_complete"])
        self.assertEqual((b["meta"]["next_scan_offset"], b["rows"][0]["count"]), (1000, 1000))
        self.assertTrue(any("전체 1,200건 중 1~1,000번째" in n and "scan_offset을 1000(으)로" in n for n in b["notices"]), b["notices"])
        rest = self.ok("dataset_query", dataset="OA-21275", metrics=["count"], scan=1000, scan_offset=1000)
        self.assertEqual(rest["rows"][0]["count"], 200)                 # 이어 훑으면 나머지가 온다
        self.assertFalse(rest["meta"]["scan_complete"])                 # 앞을 건너뛴 호출은 '다 봤다'고 하지 않는다
        self.assertIsNone(rest["meta"]["next_scan_offset"])

    def test_default_scan_covers_a_district_year(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", metrics=["count"])       # 기본값으로 1,200건을 다 본다
        self.assertTrue(b["meta"]["scan_complete"])
        self.assertNotIn("notices", b)

    def test_filter_that_did_not_apply_is_announced(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", filters={"CGG_NM": "강남구"}, limit=3)   # 가짜 서버는 필터를 무시한다
        self.assertTrue(any("CGG_NM=강남구" in n and "반영되지 않은" in n for n in b["notices"]), b["notices"])
        self.assertEqual(b["meta"]["filters_checked"]["CGG_NM"], "불일치 3/3행")

    def test_ignored_filter_is_announced_even_when_aggregating(self) -> None:
        """필터가 무시된 채 집계하면 전체 평균이 '강남구'라는 이름을 달고 나간다. 가장 위험한 경로다."""
        b = self.ok("dataset_query", dataset="OA-21275", filters={"CGG_NM": "강남구"}, metrics=["avg:THING_AMT"])
        self.assertTrue(any("반영되지 않은" in n and "말하지 마세요" in n for n in b["notices"]), b.get("notices"))
        self.assertEqual(b["meta"]["filters_checked"]["CGG_NM"], "불일치 1200/1200행")

    def test_filter_without_matching_output_field_is_marked_unchecked(self) -> None:
        self.api.handlers["culturalEventInfo"] = table("culturalEventInfo", [{"TITLE": "재즈의 밤", "STRTDATE": "2026-11-15 00:00:00.0"}])
        b = self.ok("dataset_query", dataset="OA-15486", filters={"DATE": "2026-11-15", "TITLE": "재즈"})
        self.assertEqual(b["meta"]["filters_checked"]["TITLE"], "반영됨")
        self.assertIn("확인 못 함", b["meta"]["filters_checked"]["DATE"])            # 확인하지 못한 것도 숨기지 않는다

    def test_bad_inputs(self) -> None:
        self.assertIn("filters 안에", self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", CGG_NM="마포구")["error"]["next"])
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", filters={"NOPE": "x"})
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-15486", filters={"CODENAME": "전시/미술"})
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", service="x")
        self.bad("dataset_query", "BAD_INPUT")
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", where=["THING_AMT 크다 3"])
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", where=["NOPE>3"])
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", metrics=["mean:THING_AMT"])
        self.bad("dataset_query", "BAD_INPUT", dataset="OA-21275", fields=["NOPE"])
        self.bad("dataset_query", "NOT_FOUND", dataset="OA-99999")

    def test_coercion(self) -> None:
        b = self.ok("dataset_query", dataset="OA-21275", limit="2", filters='{"CGG_NM": "마포구"}', fields="BLDG_NM, THING_AMT")
        self.assertEqual(b["meta"]["returned"], 2)
        self.assertEqual(list(b["rows"][0]), ["BLDG_NM", "THING_AMT"])     # 문자열로 온 limit, filters, fields를 받아 준다

    def test_service_with_path_args(self) -> None:
        b = self.ok("dataset_query", service="tbLnOpendataRtmsV", path_args=["2026"], limit=2)
        self.assertIn("/tbLnOpendataRtmsV/1/2/2026", self.api.calls[-1])
        self.assertIsNone(b["meta"].get("dataset"))

    def test_no_data_is_not_an_error(self) -> None:
        self.api.handlers["tbLnOpendataRtmsV"] = lambda s, e, p: fx("info200.json")
        b = self.ok("dataset_query", dataset="OA-21275", filters={"CGG_NM": "없는구"})
        self.assertEqual(b["rows"], [])
        self.assertTrue(b["notices"])

    def test_total_unknown_when_service_lies(self) -> None:
        self.api.handlers["bikeList"] = table("bikeList", [{"i": str(i)} for i in range(50)], honest_total=False)
        b = self.ok("dataset_query", service="bikeList", limit=10)
        self.assertIsNone(b["meta"]["total"])
        self.assertEqual(b["meta"]["next_offset"], 10)

    def test_big_response_is_trimmed_and_announced(self) -> None:
        fat = [{"BLDG_NM": f"건물{i}", "MEMO": f"{i}" + "가" * 480} for i in range(300)]
        self.api.handlers["tbLnOpendataRtmsV"] = table("tbLnOpendataRtmsV", fat)
        b = self.ok("dataset_query", dataset="OA-21275", limit=300)
        self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), m.MAX_CHARS)
        self.assertTrue(b["meta"]["truncated"])
        self.assertEqual(b["meta"]["next_offset"], b["meta"]["returned"])
        self.assertTrue(any("300건 중 앞" in n for n in b["notices"]))

    def test_long_cell_is_capped(self) -> None:
        self.api.handlers["tbLnOpendataRtmsV"] = table("tbLnOpendataRtmsV", [{"MEMO": "나" * 3000, "PAD": "  x  "}])
        row = self.ok("dataset_query", dataset="OA-21275")["rows"][0]
        self.assertTrue(row["MEMO"].endswith("…(+2500자)"))
        self.assertEqual(row["PAD"], "x")


class SampleModeTest(SampleBase):
    def test_no_aggregation_on_5_rows(self) -> None:
        self.api.handlers["tbLnOpendataRtmsV"] = table("tbLnOpendataRtmsV", deals())
        self.bad("dataset_query", "SAMPLE_LIMIT", dataset="OA-21275", metrics=["avg:THING_AMT"])
        self.bad("dataset_query", "SAMPLE_LIMIT", dataset="OA-21275", where=["BLDG_USG=아파트"])
        b = self.ok("dataset_query", dataset="OA-21275", limit=20)
        self.assertEqual(b["meta"]["returned"], 5)
        self.assertTrue(any("체험 모드" in n for n in b["notices"]))

    def test_place_other_than_sample_place(self) -> None:
        self.api.handlers["citydata"] = lambda s, e, p: fx("citydata.json")       # 샘플 키는 뭘 물어도 광화문을 준다
        b = self.bad("place_now", "SAMPLE_PLACE_ONLY", place="성수동")
        self.assertEqual((b["requested"], b["available"]), ("성수카페거리", "광화문·덕수궁"))
        self.assertNotIn("rows", b)
        self.assertEqual(self.ok("place_now", place="광화문·덕수궁")["rows"][0]["place"], "광화문·덕수궁")

    def test_subway_other_station(self) -> None:
        self.bad("subway_arrivals", "SAMPLE_STATION_ONLY", station="강남역")
        self.assertEqual(self.api.calls, [])                                       # 실패할 호출로 한도를 쓰지 않는다

    def test_bike_needs_key(self) -> None:
        self.api.handlers["bikeList"] = lambda s, e, p: fx("bikeList.json")
        b = self.bad("bike_stations", "SAMPLE_LIMIT", query="여의나루")
        self.assertEqual(len(b["preview"]), 5)


class PlaceTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.api.handlers["citydata"] = lambda s, e, p: fx("citydata.json")

    def test_default_sections(self) -> None:
        b = self.ok("place_now", place="광화문·덕수궁")
        row = b["rows"][0]
        self.assertEqual(row["population"]["AREA_CONGEST_LVL"], "여유")
        self.assertEqual(set(row) - {"place", "category"}, {"population", "weather", "traffic", "events"})
        self.assertEqual(b["meta"]["as_of"], row["population"]["PPLTN_TIME"])
        self.assertIn("기준", b["notices"][0])
        self.assertEqual(b["fields"]["AREA_CONGEST_LVL"], "장소 혼잡도 지표")
        self.assertNotIn("XYLIST", json.dumps(b, ensure_ascii=False))             # 좌표 목록은 싣지 않는다
        self.assertLess(len(json.dumps(b, ensure_ascii=False)), 12000)

    def test_all_sections_fit(self) -> None:
        b = self.ok("place_now", place="광화문·덕수궁", sections=list(m.SECTION_NAMES))
        self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), m.MAX_CHARS)
        self.assertIn("chargers", b["rows"][0]["charger"][0])

    def test_population_only_uses_light_endpoint(self) -> None:
        light = json.dumps({"SeoulRtd.citydata_ppltn": json.loads(fx("citydata.json"))["CITYDATA"]["LIVE_PPLTN_STTS"]},
                           ensure_ascii=False).encode()
        self.api.handlers["citydata_ppltn"] = lambda s, e, p: light
        b = self.ok("place_now", place="광화문·덕수궁", sections=["population"])
        self.assertTrue(any("/citydata_ppltn/" in u for u in self.api.calls))
        self.assertFalse(any("/citydata/" in u for u in self.api.calls))
        self.assertEqual(b["rows"][0]["population"]["AREA_CONGEST_LVL"], "여유")

    def test_ambiguous_unknown_and_list(self) -> None:
        self.assertEqual(set(self.bad("place_now", "PLACE_AMBIGUOUS", place="광화문")["candidates"]),
                         {"광화문·덕수궁", "광화문광장"})
        self.assertIn("자치구 단위", self.bad("place_now", "PLACE_UNKNOWN", place="양천구")["error"]["message"])
        self.bad("place_now", "PLACE_UNKNOWN", place="판교")
        self.bad("place_now", "BAD_INPUT", place="강남역", sections=["없는섹션"])
        b = self.ok("place_now")
        self.assertEqual(sum(len(r["places"]) for r in b["rows"]), 121)

    def test_server_returns_another_place(self) -> None:
        self.bad("place_now", "PLACE_MISMATCH", place="강남역")                  # 키가 있는데도 다른 장소가 오면 목록이 바뀐 것

    def test_compare_places(self) -> None:
        city = json.loads(fx("citydata.json"))

        def by_name(s, e, p):
            d = json.loads(json.dumps(city))
            d["CITYDATA"]["AREA_NM"] = p[0]
            return json.dumps(d, ensure_ascii=False).encode()
        self.api.handlers["citydata"] = by_name
        b = self.ok("place_now", places=["강남역", "성수동", "코엑스"], sections=["traffic"])
        self.assertEqual([r["place"] for r in b["rows"]], ["강남역", "성수카페거리", "강남 MICE 관광특구"])
        self.bad("place_now", "BAD_INPUT", places=["강남역"] * 6)


class SubwayTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: (
            fx("subway_arrival.json") if p == ["서울"] else fx("info200.json") if p == ["서울역"] else fx("subway_error.json"))
        m.age_sec = lambda stamp: 30                    # 픽스처는 오래전 시각이다. 방금 갱신된 것으로 본다

    def test_station_name_and_fields(self) -> None:
        b = self.ok("subway_arrivals", station="서울역")
        self.assertEqual(b["meta"]["station"], "서울 + 서울역")                     # 1·4호선은 '서울', GTX-A는 '서울역'으로 나뉘어 있다
        self.assertTrue(any(f"/api/subway/{SUBKEY}/json/realtimeStationArrival/0/50/%EC%84%9C%EC%9A%B8%EC%97%AD" in u
                            for u in self.api.calls))                                # 전용 키, 0부터, 두 이름 모두
        row = b["rows"][0]
        self.assertTrue({"line", "direction", "heading", "status", "updated", "age_sec"} <= set(row))
        self.assertTrue(row["line"].endswith("선"))
        self.assertTrue(b["meta"]["as_of"])

    def test_line_and_direction_filter(self) -> None:
        allrows = self.ok("subway_arrivals", station="서울")["rows"]
        one = self.ok("subway_arrivals", station="서울", line="1호선")["rows"]
        self.assertTrue(one and all(r["line"] == "1호선" for r in one))
        self.assertLessEqual(len(one), len(allrows))
        up = self.ok("subway_arrivals", station="서울", direction="상행")["rows"]
        self.assertTrue(all(r["direction"] == "상행" for r in up))

    def test_common_station_nicknames(self) -> None:
        seen = []
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: seen.append(p[0]) or fx("subway_arrival.json")
        for q in ("이수역", "홍대", "총신대입구"):
            self.ok("subway_arrivals", station=q)
        self.assertEqual(seen, ["총신대입구(이수)", "홍대입구"])                     # 같은 역은 캐시에서 온다

    def test_cached_for_15s(self) -> None:
        self.ok("subway_arrivals", station="서울")
        self.ok("subway_arrivals", station="서울", line="1호선")
        self.assertEqual(len(self.api.calls), 2)                                   # 서울, 서울역 한 번씩. 두 번째 질문은 캐시에서

    def test_unknown_station(self) -> None:
        b = self.bad("subway_arrivals", "ERROR-336", station="없는역")              # 역 이름 오류라고 단정하지 않는다
        self.assertIn("역 이름이 서울시 표기와 다를 수", b["error"]["next"])
        self.bad("subway_arrivals", "BAD_INPUT")

    def test_separate_key_explained(self) -> None:
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("info100.xml")
        self.assertIn("별도", self.bad("subway_arrivals", "SUBWAY_KEY_NEEDED", station="강남")["error"]["next"])


class BikeTest(Base):
    def setUp(self) -> None:
        super().setUp()
        stations = [{"stationName": f"{100 + i}. 대여소{i}", "stationId": f"ST-{i}", "parkingBikeTotCnt": str(i % 20),
                     "rackTotCnt": "15", "stationLatitude": str(37.50 + i * 0.0001), "stationLongitude": "126.9300"}
                    for i in range(2500)]
        stations[7].update(stationName="207. 여의나루역 1번출구 앞", stationLatitude="37.5271", stationLongitude="126.9326")
        self.api.handlers["bikeList"] = table("bikeList", stations, honest_total=False)

    def test_by_name(self) -> None:
        b = self.ok("bike_stations", query="여의나루")
        self.assertEqual([r["station"] for r in b["rows"]], ["207. 여의나루역 1번출구 앞"])
        self.assertEqual(b["meta"]["stations_searched"], 2500)                     # 1,000건에서 끊기지 않았다
        self.assertEqual(b["rows"][0]["bikes"], 7)

    def test_nearby_sorted_by_distance(self) -> None:
        b = self.ok("bike_stations", lat=37.5271, lng=126.9326, radius_m=300)
        d = [r["distance_m"] for r in b["rows"]]
        self.assertEqual(d[0], 0)
        self.assertEqual(d, sorted(d))
        self.assertTrue(all(x <= 300 for x in d))

    def test_needs_query_or_coords(self) -> None:
        self.bad("bike_stations", "BAD_INPUT")
        self.bad("bike_stations", "BAD_INPUT", lat=37.5)

    def test_cached_for_60s(self) -> None:
        self.ok("bike_stations", query="대여소1")
        n = len(self.api.calls)
        self.ok("bike_stations", query="대여소2")
        self.assertEqual(len(self.api.calls), n)


class SearchDescribeTest(Base):
    def test_featured_without_query(self) -> None:
        b = self.ok("dataset_search")
        self.assertEqual(len(b["rows"]), len(catalog.FEATURED))
        self.assertTrue(all(catalog.is_dataset_id(r["dataset"]) for r in b["rows"]))

    def test_search(self) -> None:
        b = self.ok("dataset_search", query="따릉이", limit=3)
        self.assertEqual((b["meta"]["total"], b["meta"]["returned"], b["meta"]["next_page"]), (21, 7, 2))    # 1쪽 10건 중 API가 있는 7건. 받은 쪽은 limit에서 자르지 않는다
        self.assertTrue(all(r["has_api"] for r in b["rows"]))

    def test_stat_datasets_are_excluded_and_explained(self) -> None:
        b = self.ok("dataset_search", query="주민등록인구")
        self.assertTrue(all(r["has_api"] for r in b["rows"]))
        self.assertTrue(any("통계 데이터셋" in n for n in b["notices"]))
        shown = self.ok("dataset_search", query="주민등록인구", api_only=False)["rows"]
        self.assertTrue(any(r["dataset"].startswith("DT") and r["url"].startswith("https://") for r in shown))

    def test_nothing_found(self) -> None:
        b = self.ok("dataset_search", query="없는말")
        self.assertEqual(b["rows"], [])
        self.assertTrue(b["notices"])

    def test_describe(self) -> None:
        b = self.ok("dataset_describe", dataset="oa-15486")
        self.assertEqual(b["meta"]["service"], "culturalEventInfo")
        self.assertEqual([f["name"] for f in b["filters"]], ["CODENAME", "TITLE", "DATE"])
        self.assertEqual(b["example"]["arguments"]["dataset"], "OA-15486")
        self.bad("dataset_describe", "BAD_INPUT", dataset="DT201004O020004")
        self.bad("dataset_describe", "NOT_FOUND", dataset="OA-99999")

    def test_describe_example_fills_required(self) -> None:
        spec = catalog.get_spec("OA-21285", fetch=self.api)
        self.assertEqual(m._example(spec)["arguments"]["filters"], {"AREA_NM": "광화문·덕수궁"})   # 샘플 URL의 값으로 필수 인자를 채운다


class KeySetupTest(Base):
    def test_status_never_shows_key(self) -> None:
        b = self.ok("key_setup")
        self.assertNotIn(KEY, json.dumps(b, ensure_ascii=False))
        self.assertIn(KEY[-4:], b["meta"]["general"])

    def test_invalid_key(self) -> None:
        self.api.handlers["bikeList"] = lambda s, e, p: fx("bikeList.json")
        b = self.bad("key_setup", "KEY_INVALID", general="WRONGKEY")
        self.assertIn("공백", b["error"]["message"])
        self.assertFalse((Path(os.environ["SEOUL_DATA_HOME"]) / ".env").exists())

    def test_save_and_apply(self) -> None:
        self.api.handlers["bikeList"] = lambda s, e, p: fx("bikeList.json")
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("subway_arrival.json")
        b = self.ok("key_setup", general=KEY, subway=SUBKEY)
        path = Path(os.environ["SEOUL_DATA_HOME"]) / ".env"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sc.find_key("SEOUL_API_KEY"), KEY)
        self.assertEqual(sc.find_key("SEOUL_SUBWAY_API_KEY"), SUBKEY)
        text = json.dumps(b, ensure_ascii=False)
        self.assertNotIn(KEY, text)
        self.assertNotIn(SUBKEY, text)
        self.assertIn("저장했습니다", b["notices"][0])
        path.unlink()


# ── 프로토콜 ───────────────────────────────────────────────────────────────

class ProtocolTest(Base):
    def rpc(self, method: str, params: dict | None = None, mid=1):
        return m.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize_negotiates_version(self) -> None:
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-03-26")
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], m.PROTOCOLS[0])
        self.assertEqual(r["serverInfo"]["name"], "seoul-data")
        self.assertIn("notices", r["instructions"])

    def test_tools_list(self) -> None:
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["dataset_search", "dataset_describe", "dataset_query", "place_now",
                                                     "subway_arrivals", "bike_stations", "key_setup"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])
            self.assertLess(len(t["description"]), 1100, t["name"])
            self.assertEqual(t["annotations"]["readOnlyHint"], t["name"] != "key_setup")

    def test_call_and_error_shape(self) -> None:
        r = self.rpc("tools/call", {"name": "place_now", "arguments": {"place": "판교"}})["result"]
        self.assertTrue(r["isError"])
        body = json.loads(r["content"][0]["text"])
        self.assertEqual(set(body["error"]), {"code", "message", "next"})
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        body, err = m.run_tool("dataset_query", {"service": "x", "limit": 1e400})
        self.assertEqual(body["error"]["code"], "BAD_INPUT")                        # 넘치는 숫자에 죽지 않는다
        body, err = m.run_tool("subway_arrivals", {"station": ["강남"]})
        self.assertEqual(body["error"]["code"], "BAD_INPUT")

    def test_notifications_and_unknown_methods(self) -> None:
        self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)

    def test_stdio_roundtrip(self) -> None:
        """서버를 실제 프로세스로 띄워 한 줄씩 주고받는다. 네트워크가 필요 없는 호출만 쓴다."""
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "place_now", "arguments": {"place": "광화문"}}},
                 "깨진 줄",
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "dataset_search", "arguments": {}}}]
        text = "\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
        env = {**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": ""}                # 로캘이 C여도 한글이 깨지지 않아야 한다
        p = subprocess.run([sys.executable, str(ROOT / "assets" / "mcp_server.py")], input=text.encode("utf-8"),
                           capture_output=True, timeout=30, env=env)
        out = [json.loads(x) for x in p.stdout.decode("utf-8").splitlines()]
        self.assertEqual([o.get("id") for o in out], [1, 2, 3, None, 4])
        self.assertEqual(len(out[1]["result"]["tools"]), 7)
        self.assertIn("광화문광장", json.loads(out[2]["result"]["content"][0]["text"])["candidates"])
        self.assertEqual(out[3]["error"]["code"], -32700)
        self.assertFalse(out[4]["result"]["isError"])



def big_city(name: str) -> bytes:
    """실제 크기(15만 자 안팎)에 가까운 도시데이터. 픽스처의 목록을 실제 건수만큼 불린다."""
    d = json.loads(fx("citydata.json"))
    c = d["CITYDATA"]
    c["AREA_NM"] = name
    c["ROAD_TRAFFIC_STTS"]["ROAD_TRAFFIC_STTS"] = c["ROAD_TRAFFIC_STTS"]["ROAD_TRAFFIC_STTS"] * 60
    for key, times in (("CHARGER_STTS", 20), ("PRK_STTS", 10), ("BUS_STN_STTS", 11), ("SBIKE_STTS", 4), ("EVENT_STTS", 3)):
        c[key] = c[key] * times
    lot = dict(c["PRK_STTS"][0], PRK_NM="세종로 공영주차장", CUR_PRK_YN="Y", CUR_PRK_CNT="444")
    c["PRK_STTS"].append(lot)                              # 실시간 현황이 있는 주차장을 맨 뒤에 둔다
    return json.dumps(d, ensure_ascii=False).encode()


class BigPlaceTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.api.handlers["citydata"] = lambda s, e, p: big_city(p[0])
        self.api.handlers["citydata_ppltn"] = lambda s, e, p: json.dumps(
            {"SeoulRtd.citydata_ppltn": [dict(json.loads(big_city(p[0]))["CITYDATA"]["LIVE_PPLTN_STTS"][0], AREA_NM=p[0])]},
            ensure_ascii=False).encode()

    def test_fixture_is_realistically_big(self) -> None:
        self.assertGreater(len(big_city("강남역")), 120_000)

    def test_five_places_all_sections_keeps_every_place(self) -> None:
        five = ["강남역", "성수동", "코엑스", "서울역", "여의도"]
        b = self.ok("place_now", places=five, sections=list(m.SECTION_NAMES))
        self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), m.MAX_CHARS)
        self.assertEqual(len(b["rows"]), 5)                                        # 장소를 버리지 않는다
        self.assertTrue(all("population" in r for r in b["rows"]))
        self.assertTrue(any("섹션을 뺐습니다" in n for n in b["notices"]), b["notices"])
        self.assertFalse(any("fields" in n or "offset" in n for n in b["notices"]))  # 이 도구에 없는 인자를 권하지 않는다

    def test_single_place_all_sections_fit_at_real_size(self) -> None:
        b = self.ok("place_now", place="강남역", sections=list(m.SECTION_NAMES))
        self.assertLessEqual(len(json.dumps(b, ensure_ascii=False)), m.MAX_CHARS)
        self.assertEqual(set(b["rows"][0]) - {"place", "category"}, set(m.SECTION_NAMES))

    def test_parking_with_live_count_comes_first(self) -> None:
        b = self.ok("place_now", place="강남역", sections=["parking"])
        lots = b["rows"][0]["parking"]
        self.assertEqual((lots[0]["PRK_NM"], lots[0]["CUR_PRK_CNT"]), ("세종로 공영주차장", "444"))   # 맨 뒤에 있어도 잘리지 않는다
        self.assertTrue(any("실시간 값을 주는 곳은 1곳" in n for n in b["notices"]))
        self.assertEqual(sum("지금 주차된 차량 수입니다" in n for n in b["notices"]), 1)
        self.assertIn("지금 주차된 차량 수", b["fields"]["CUR_PRK_CNT"])              # 명세의 틀린 설명을 그대로 싣지 않는다

    def test_one_bad_place_does_not_sink_the_rest(self) -> None:
        b = self.ok("place_now", places=["강남역", "광화문", "판교"], sections=["population"])
        self.assertEqual(b["rows"][0]["place"], "강남역")
        self.assertEqual(b["rows"][1]["error"]["code"], "PLACE_AMBIGUOUS")
        self.assertIn("광화문광장", b["rows"][1]["candidates"])
        self.assertEqual(b["rows"][2]["error"]["code"], "PLACE_UNKNOWN")
        self.bad("place_now", "PLACE_UNKNOWN", places=["판교", "분당"])              # 전부 안 되면 오류

    def test_interpretation_is_announced(self) -> None:
        b = self.ok("place_now", place="성수동", sections=["population"])
        self.assertEqual(b["rows"][0]["place"], "성수카페거리")
        self.assertTrue(any("장소 해석: '성수동' → '성수카페거리'" in n for n in b["notices"]))
        b = self.ok("place_now", place="강남역", sections=["population"])
        self.assertFalse(any("장소 해석" in n for n in b["notices"]))

    def test_units_and_stale_notice(self) -> None:
        b = self.ok("place_now", place="강남역")
        self.assertIn("℃", json.dumps(b["meta"]["units"], ensure_ascii=False))
        self.assertTrue(any("분 전 값" in n for n in b["notices"]))                 # 픽스처는 오래전 시각이다


class SubwayMoreTest(Base):
    ROWS = [{"subwayId": "1002", "updnLine": d, "trainLineNm": f"{dest}행 - 방면", "arvlMsg2": "2분 후", "arvlMsg3": "역삼",
             "barvlDt": str(sec), "bstatnNm": dest, "btrainSttus": "일반", "recptnDt": "2026-11-14 21:00:00", "lstcarAt": "0"}
            for d, dest, sec in (("내선", "성수", 120), ("외선", "신도림", 60), ("내선", "성수", 300))] + \
           [{"subwayId": "1077", "updnLine": "하행", "trainLineNm": "광교행 - 양재방면", "arvlMsg2": "전역 출발", "arvlMsg3": "신논현",
             "barvlDt": "90", "bstatnNm": "광교", "btrainSttus": "일반", "recptnDt": "2026-11-14 21:00:10", "lstcarAt": "1"}]

    def setUp(self) -> None:
        super().setUp()
        m.age_sec = lambda stamp: 30
        body = json.dumps({"errorMessage": {"status": 200, "code": "INFO-000", "message": "정상", "total": 4},
                           "realtimeArrivalList": self.ROWS}, ensure_ascii=False).encode()
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: body

    def test_filters_actually_filter(self) -> None:
        got = lambda **kw: [(r["line"], r["direction"], r.get("eta_sec")) for r in self.ok("subway_arrivals", station="강남", **kw)["rows"]]   # noqa: E731
        self.assertEqual(len(got()), 4)
        self.assertEqual(got(line="2호선", direction="내선"), [("2호선", "내선", 120), ("2호선", "내선", 300)])   # 빨리 오는 순
        self.assertEqual(got(line="신분당선"), [("신분당선", "하행", 90)])
        self.assertEqual(got(direction="신도림"), [("2호선", "외선", 60)])            # 행선지로도 좁힌다
        self.assertTrue(self.ok("subway_arrivals", station="강남", line="신분당선")["rows"][0]["last_train"])

    def test_stale_trains_are_marked_not_dropped(self) -> None:
        """종착역에서 대기하는 열차도 오래 갱신되지 않는다. 지우지 않고 표시해 뒤로 보낸다."""
        m.age_sec = lambda stamp: 400 if stamp.endswith(":10") else 30           # 신분당선 행만 오래됐다
        b = self.ok("subway_arrivals", station="강남")
        sb = [r for r in b["rows"] if r["line"] == "신분당선"]
        self.assertTrue(sb and sb[0]["stale"])
        self.assertTrue(any("1건은 갱신된 지 90초가 넘어" in n for n in b["notices"]))

    def test_arriving_train_comes_first_and_eta_now(self) -> None:
        rows = [{"subwayId": "1004", "updnLine": "하행", "trainLineNm": "오이도행 - 신용산방면", "arvlMsg2": "7분 30초 후", "arvlMsg3": "동대문",
                 "barvlDt": "450", "ordkey": "12004오이도0", "recptnDt": "2026-11-15 08:54:30", "lstcarAt": "0"},
                {"subwayId": "1004", "updnLine": "하행", "trainLineNm": "사당행 - 신용산방면", "arvlMsg2": "서울 진입", "arvlMsg3": "서울",
                 "barvlDt": "0", "ordkey": "11000사당0", "recptnDt": "2026-11-15 08:54:30", "lstcarAt": "0"},
                {"subwayId": "1004", "updnLine": "하행", "trainLineNm": "사당행 - 신용산방면", "arvlMsg2": "전역 도착", "arvlMsg3": "회현",
                 "barvlDt": "120", "ordkey": "11001사당0", "recptnDt": "2026-11-15 08:54:30", "lstcarAt": "1"}]
        body = json.dumps({"errorMessage": {"status": 200, "code": "INFO-000", "message": "정상", "total": 3},
                           "realtimeArrivalList": rows}, ensure_ascii=False).encode()
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: body
        b = self.ok("subway_arrivals", station="강남", limit=2)
        self.assertEqual([r["status"] for r in b["rows"]], ["서울 진입", "전역 도착"])   # 들어오는 열차가 맨 앞, 7분 뒤 열차는 잘린다
        self.assertEqual(b["rows"][1]["eta_now_sec"], 90)                             # 120초 - 갱신 뒤 30초
        self.assertTrue(any("3건 중 가까운 2건만" in n for n in b["notices"]))
        self.assertTrue(any("막차" in n for n in b["notices"]))

    def test_filter_that_matches_nothing_says_so(self) -> None:
        b = self.ok("subway_arrivals", station="강남", line="9호선")
        self.assertEqual(b["rows"], [])
        self.assertTrue(any("지금 이 역에 있는 것: 2호선(" in n and "신분당선(" in n for n in b["notices"]), b["notices"])
        self.assertTrue(any("4건 중 line, direction 조건에 맞는 열차가 없습니다" in n for n in b["notices"]))

    def test_empty_answer_mentions_the_station_name(self) -> None:
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("info200.json")
        b = self.ok("subway_arrivals", station="홍대")
        self.assertTrue(any("역 이름이 서울시 표기와 다를 수" in n for n in b["notices"]))

    def test_outage_is_not_reported_as_a_wrong_station(self) -> None:
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("error500.json")
        self.assertEqual(self.call("subway_arrivals", station="강남")[0]["error"]["code"], "ERROR-500")

    def test_general_key_is_rejected_by_the_subway_api(self) -> None:
        e338 = json.dumps({"status": 500, "code": "ERROR-338", "message": "해당 인증키로는 실시간 서비스를 사용할 수 없습니다.",
                           "total": 0}, ensure_ascii=False).encode()
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: e338
        b = self.bad("subway_arrivals", "SUBWAY_KEY_NEEDED", station="강남")
        self.assertIn('place_now {"place": "강남역", "sections": ["subway"]}', b["error"]["next"])   # 일반 키로 되는 길을 알려 준다
        self.api.handlers["bikeList"] = lambda s, e, p: fx("bikeList.json")
        b = self.bad("key_setup", "KEY_INVALID", subway=KEY)
        self.assertIn("일반 인증키라서", b["error"]["message"])

    def test_without_a_subway_key_the_general_key_is_not_tried(self) -> None:
        m.set_client(sc.Client(key=KEY, fetch=self.api))
        self.assertEqual(m.client().subway_key_mode, "sample")
        b = self.bad("subway_arrivals", "SAMPLE_STATION_ONLY", station="강남")
        self.assertTrue(b["error"]["message"].startswith("일반 인증키는 들어 있습니다"))   # '체험 모드'라고 하지 않는다
        self.assertTrue(b["error"]["next"].startswith("일반 인증키만 있다면 place_now"))   # 되는 길을 먼저 알려 준다
        self.assertEqual(self.api.calls, [])


class SubwayNamesTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.seen = []
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: self.seen.append(p[0]) or (
            fx("subway_arrival.json") if p[0] in ("천호(풍납토성)", "군자(능동)") else fx("info200.json"))

    def test_bracketed_station_names(self) -> None:
        """API는 '천호'에 빈 결과를 주고 '천호(풍납토성)'에만 답한다. 괄호 앞부분으로도 찾게 한다."""
        b = self.ok("subway_arrivals", station="천호역")
        self.assertEqual(self.seen, ["천호(풍납토성)"])
        self.assertTrue(b["rows"])
        self.assertIn("천호(풍납토성)", b["meta"]["station_note"])
        self.ok("subway_arrivals", station="군자")
        self.assertEqual(self.seen[-1], "군자(능동)")

    def test_unknown_name_gets_suggestions(self) -> None:
        b = self.ok("subway_arrivals", station="고터")
        self.assertTrue(any("비슷한 이름: 고속터미널" in n for n in b["notices"]), b["notices"])

    def test_line_names(self) -> None:
        self.assertEqual(m._line_target("2"), "2호선")
        self.assertEqual(m._line_target("분당선"), "수인분당선")
        self.assertEqual(m._line_target("신분당선"), "신분당선")


class SubwayCountTest(Base):
    def test_every_real_request_is_counted_once(self) -> None:
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("info200.json")
        before = sc.count_subway()
        self.ok("subway_arrivals", station="강남")
        self.ok("subway_arrivals", station="강남")                                 # 캐시 적중은 세지 않는다
        self.ok("subway_arrivals", station="서울")                                 # 서울 + 서울역 두 번
        self.assertEqual(sc.count_subway() - before, 3)

    def test_describe_does_not_spend_subway_calls(self) -> None:
        self.assertIsNone(m._sample_row({"service": "realtimePosition", "host": "subway", "first_index": 0, "params": []}))
        self.assertFalse(any("swopenapi" in u for u in self.api.calls))


class SubwayDatasetQueryTest(Base):
    def setUp(self) -> None:
        super().setUp()
        trains = [{"subwayNm": "2호선" if i % 2 else "3호선", "statnNm": f"역{i}", "trainNo": str(i)} for i in range(76)]
        self.trains = trains
        self.api.handlers["realtimePosition"] = lambda s, e, p: json.dumps(
            {"errorMessage": {"status": 200, "code": "INFO-000", "message": "정상", "total": len(trains)},
             "realtimePositionList": trains[max(s, 1) - 1:e]}, ensure_ascii=False).encode()

    def test_service_name_goes_to_the_subway_host_with_correct_window(self) -> None:
        b = self.ok("dataset_query", service="realtimePosition", path_args=["2호선"], offset=5, limit=6)
        self.assertTrue(any("swopenapi" in u and "/realtimePosition/6/11/" in u for u in self.api.calls), self.api.calls)
        self.assertEqual([r["trainNo"] for r in b["rows"]], [str(i) for i in range(5, 11)])
        self.assertEqual((b["meta"]["total"], b["meta"]["next_offset"]), (76, 11))

    def test_where_and_aggregation_are_applied_on_subway_data(self) -> None:
        b = self.ok("dataset_query", service="realtimePosition", path_args=["2호선"], where=["subwayNm=2호선"], metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 38)                                 # 조건을 조용히 버리지 않는다
        self.assertTrue(b["meta"]["scan_complete"])


class SampleSubwayTest(SampleBase):
    def test_empty_after_filter_is_not_reported_as_no_trains(self) -> None:
        self.api.handlers["realtimeStationArrival"] = lambda s, e, p: fx("subway_arrival.json")
        b = self.ok("subway_arrivals", station="서울역", line="9호선")
        self.assertEqual(b["rows"], [])
        self.assertTrue(any("열차가 없다는 뜻이 아닙니다" in n for n in b["notices"]), b["notices"])

    def test_ambiguous_place_says_which_one_works_in_sample_mode(self) -> None:
        self.assertIn("'광화문·덕수궁'만 볼 수", self.bad("place_now", "PLACE_AMBIGUOUS", place="광화문")["error"]["next"])


class RobustnessTest(Base):
    def test_malformed_messages_get_errors_not_crashes(self) -> None:
        self.assertEqual(m.safe_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]})["error"]["code"], -32602)
        self.assertEqual(m.safe_handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": ["x"]}})["error"]["code"], -32602)
        self.assertEqual(m.safe_handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": "x"})["error"]["code"], -32602)
        self.assertEqual(m.safe_handle("문자열")["error"]["code"], -32600)
        self.assertEqual(m.safe_handle({"jsonrpc": "2.0", "id": 0, "method": "ping"})["id"], 0)        # id 0도 요청이다
        self.assertEqual(m.safe_handle({"jsonrpc": "2.0", "id": "abc", "method": "ping"})["id"], "abc")

    def test_stdio_survives_garbage_and_batches(self) -> None:
        lines = [b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":["x"]}', b"\xff\xfe broken bytes",
                 b'[{"jsonrpc":"2.0","id":2,"method":"ping"},{"jsonrpc":"2.0","method":"notifications/initialized"},{"jsonrpc":"2.0","id":3,"method":"ping"}]',
                 b"[]", b'{"jsonrpc":"2.0","id":4,"method":"ping"}']
        env = {**os.environ, "LC_ALL": "C"}
        p = subprocess.run([sys.executable, str(ROOT / "assets" / "mcp_server.py")], input=b"\n".join(lines) + b"\n",
                           capture_output=True, timeout=30, env=env)
        out = [json.loads(x) for x in p.stdout.decode("utf-8").splitlines()]
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-300:])
        self.assertEqual(out[0]["error"]["code"], -32602)
        self.assertEqual(out[1]["error"]["code"], -32700)
        self.assertEqual([x["id"] for x in out[2]], [2, 3])                          # 배치는 배열로 답한다
        self.assertEqual(out[3]["error"]["code"], -32600)
        self.assertEqual(out[4]["id"], 4)                                            # 끝까지 살아 있다

    def test_deadline_is_passed_down_to_the_http_call(self) -> None:
        import time as _t
        t = _t.time()
        with self.assertRaises(sc.SeoulError) as cm:
            sc.http_fetch("http://10.255.255.1:9/", deadline=_t.time() - 1)          # 이미 지난 기한이면 기다리지 않는다
        self.assertEqual(cm.exception.code, "NETWORK")
        self.assertLess(_t.time() - t, 1)
        seen = {}
        m.set_client(sc.Client(key=KEY, fetch=lambda url, **kw: seen.update(kw) or fx("bikeList.json")))
        m.site_fetch = lambda url, **kw: seen.update(site=kw) or fx("search_bike.html")
        self.ok("dataset_query", service="bikeList", limit=5)
        self.ok("dataset_search", query="따릉이")
        self.assertIsNotNone(seen["deadline"])
        self.assertIsNotNone(seen["site"]["deadline"])                               # 사이트 호출도 같은 기한 안에서 돈다

    def test_search_paging_does_not_lose_results(self) -> None:
        def page(n: int) -> bytes:
            items = "".join(
                f'<dl class="type-b"><dt><em class="stat-ctg"><span>공공데이터</span></em><em class="stat-class-1">[교통]</em>'
                f'<a href="#" data-rel="OA-{n}{i:02d}/S/1/datasetView.do" class="goView"><strong>데이터 {n}-{i}</strong></a></dt>'
                f'<dd class="list-statistics-info3"><button class="btn-data-1">SHEET</button>'
                + ('<button class="btn-data-2">OpenAPI</button>' if i < 7 else "") + "</dd></dl>" for i in range(10))
            return f"전체 검색결과&nbsp;<strong>30</strong>건 {items}".encode()
        self.api.site_handler = lambda url, data: page(int(data["pageIndex"]))
        first = self.ok("dataset_search", query="x", limit=10)
        ids = [r["dataset"] for r in first["rows"]]
        self.assertEqual((len(ids), first["meta"]["next_page"]), (14, 3))            # 1쪽 7건 + 2쪽 7건, 자르지 않는다
        rest = self.ok("dataset_search", query="x", limit=10, page=first["meta"]["next_page"])
        ids += [r["dataset"] for r in rest["rows"]]
        self.assertEqual(len(set(ids)), 21)                                          # API가 있는 21건이 빠짐없이 나온다

    def test_site_change_is_not_hidden_behind_a_stale_cache(self) -> None:
        self.ok("dataset_describe", dataset="OA-15486")
        os.utime(sc.cache_dir() / "spec" / "OA-15486.json", (1, 1))
        self.api.site_handler = lambda url, data: "<html>새 디자인</html>".encode()
        b = self.ok("dataset_describe", dataset="OA-15486")
        self.assertTrue(any("화면 구조가 바뀌어" in n for n in b["notices"]), b.get("notices"))
        with self.assertRaises(sc.SeoulError) as cm:
            catalog.get_spec("OA-15486", fetch=self.api, use_disk=False)             # 점검은 캐시 없이 읽어 변화를 잡는다
        self.assertEqual(cm.exception.code, "SITE_CHANGED")


class KeyFileTest(Base):
    def test_existing_lines_and_comments_survive(self) -> None:
        path = Path(os.environ["SEOUL_DATA_HOME"]) / ".env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 내 메모\nexport SEOUL_API_KEY=OLD\nOTHER=1\n", encoding="utf-8")
        m.write_keys("NEWKEY", "SUBNEW")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, "# 내 메모\nSEOUL_API_KEY=NEWKEY\nOTHER=1\nSEOUL_SUBWAY_API_KEY=SUBNEW\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(path.with_name(".env.tmp").exists())
        path.unlink()

    def test_saved_key_shadowed_by_env_is_announced(self) -> None:
        self.api.handlers["bikeList"] = lambda s, e, p: fx("bikeList.json")
        os.environ["SEOUL_API_KEY"] = "ENVKEYENVKEY1234"
        try:
            b = self.ok("key_setup", general=KEY)
            self.assertIn("환경변수에 다른 값이", b["notices"][0])
            self.assertNotIn("바로 적용됩니다", b["notices"][0])
        finally:
            os.environ.pop("SEOUL_API_KEY", None)
            (Path(os.environ["SEOUL_DATA_HOME"]) / ".env").unlink()

    def test_other_env_names_are_recognised(self) -> None:
        os.environ["SEOUL_OPENAPI_KEY"] = "FROMALIAS"
        try:
            self.assertEqual(sc.find_key("SEOUL_API_KEY"), "FROMALIAS")
            os.environ["SEOUL_API_KEY"] = "OFFICIAL"
            self.assertEqual(sc.find_key("SEOUL_API_KEY"), "OFFICIAL")               # 정식 이름이 먼저다
        finally:
            os.environ.pop("SEOUL_OPENAPI_KEY", None)
            os.environ.pop("SEOUL_API_KEY", None)


class ShuffledPagesTest(Base):
    """서울시 서버는 일부 데이터셋에서 장마다 행 순서를 바꿔 돌려준다 (2026-11-14 종로구 음식점에서 확인).
    그대로 이으면 같은 행이 두 번 들어오고 그만큼 다른 행이 빠진다."""

    def shuffled(self, n: int, seed: int = 7):
        import random
        base = [{"MGTNO": f"M{i:05d}", "GBN": "01" if i % 3 else "03"} for i in range(n)]
        state = {"calls": 0}

        def h(start, end, params):
            state["calls"] += 1
            order = base[:]
            random.Random(seed + state["calls"] // 3).shuffle(order)      # 장을 넘기는 사이 순서가 바뀐다
            return envelope("shuf", order[start - 1:end], len(base))
        return base, h

    def test_duplicates_are_refetched_until_every_row_is_seen(self) -> None:
        base, h = self.shuffled(3500)
        self.api.handlers["shuf"] = h
        b = self.ok("dataset_query", service="shuf", group_by=["GBN"], metrics=["count"])
        got = {r["GBN"]: r["count"] for r in b["rows"]}
        want = {"01": sum(1 for r in base if r["GBN"] == "01"), "03": sum(1 for r in base if r["GBN"] == "03")}
        self.assertEqual(got, want)                                               # 겹침 없이 정확한 건수
        self.assertTrue(b["meta"]["scan_complete"])
        self.assertTrue(any("빠짐없이 확인한 값" in n and "결과에는 영향이 없습니다" in n for n in b["notices"]), b["notices"])

    def test_when_rows_stay_missing_it_says_so(self) -> None:
        import random
        base = [{"MGTNO": f"M{i:05d}"} for i in range(2450)]            # 서버는 2,500건이라고 하지만 서로 다른 행은 2,450건만 준다
        n = {"c": 0}

        def stuck(start, end, params):
            n["c"] += 1
            order = base[:]
            random.Random(n["c"] // 3).shuffle(order)
            order += order[:50]                           # 마지막 장 끝의 50자리에는 첫 장에 이미 준 행이 다시 온다 (장 사이 겹침)
            return envelope("stuck", order[start - 1:end], 2500)
        self.api.handlers["stuck"] = stuck
        b = self.ok("dataset_query", service="stuck", metrics=["count"])
        self.assertFalse(b["meta"]["scan_complete"])
        self.assertGreater(b["meta"]["rows_missing"], 0)
        self.assertEqual(b["rows"][0]["count"] + b["meta"]["rows_missing"], 2500)
        self.assertTrue(any(n.startswith("어림값입니다") and "50행(약 2.0%)" in n for n in b["notices"]), b["notices"])
        self.assertEqual(b["meta"]["rows_missing_pct"], 2.0)
        self.assertFalse(any("마지막 조각" in n for n in b["notices"]))                # 처음부터 훑었으면 '앞 조각과 합치라'고 하지 않는다

    def test_genuinely_identical_rows_are_kept(self) -> None:
        """같은 날 같은 값으로 두 번 거래된 것처럼 내용이 완전히 같은 행은 겹침이 아니다. 지우면 건수가 준다."""
        rows = [{"N": str(i)} for i in range(2000)] + [{"N": "same"}] * 40
        self.api.handlers["x"] = table("x", rows)
        b = self.ok("dataset_query", service="x", metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 2040)
        self.assertTrue(b["meta"]["scan_complete"])
        self.assertNotIn("notices", b)

    def test_single_page_data_is_untouched(self) -> None:
        self.api.handlers["x"] = table("x", [{"i": str(i)} for i in range(800)])
        b = self.ok("dataset_query", service="x", metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 800)
        self.assertNotIn("notices", b)


class RoundFiveTest(Base):
    def test_stable_order_with_identical_rows_across_a_page_boundary(self) -> None:
        """순서가 고정된 서버에서 내용이 같은 두 행이 1,000번째와 1,001번째에 걸치면, 겹침이 아니라 정상 행이다."""
        rows = [{"N": str(i)} for i in range(2500)]
        rows[999] = rows[1000] = {"N": "same"}
        self.api.handlers["x"] = table("x", rows)
        b = self.ok("dataset_query", service="x", metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 2500)
        self.assertTrue(b["meta"]["scan_complete"])
        self.assertNotIn("notices", b)
        self.assertLessEqual(len(self.api.calls), 6)                               # 한 번 더 받아 순서를 확인하는 데서 끝난다

    def test_values_changing_between_fetches_never_exceed_the_total(self) -> None:
        """받는 사이 값이 바뀌면 같은 행이 다른 행으로 보인다. 총계보다 많은 행을 '맞췄다'고 내면 안 된다."""
        import random
        n = {"c": 0}

        def live(start, end, params):
            n["c"] += 1
            order = [{"ID": f"S{i}", "V": str(n["c"] // 2)} for i in range(2500)]   # 두 요청마다 값이 바뀌고 순서도 흔들린다
            random.Random(n["c"] // 2).shuffle(order)
            return envelope("live", order[start - 1:end], 2500)
        self.api.handlers["live"] = live
        b = self.ok("dataset_query", service="live", metrics=["count"])
        self.assertLessEqual(b["rows"][0]["count"], 2500)
        if b["rows"][0]["count"] != 2500 or b.get("notices"):
            self.assertFalse(b["meta"]["scan_complete"] and b["rows"][0]["count"] != 2500)

    def test_bike_stations_are_identified_by_station_id(self) -> None:
        import random
        n = {"c": 0}

        def bikes(start, end, params):
            n["c"] += 1
            st = [{"stationName": f"{i}. 대여소", "stationId": f"ST-{i}", "parkingBikeTotCnt": str((i + n["c"]) % 9), "rackTotCnt": "10",
                   "stationLatitude": "37.5", "stationLongitude": "127.0"} for i in range(2500)]
            random.Random(n["c"] // 3).shuffle(st)
            return envelope("rentBikeStatus", st[start - 1:end], end - start + 1)
        self.api.handlers["bikeList"] = bikes
        b = self.ok("bike_stations", query="대여소", limit=1)
        self.assertLessEqual(b["meta"]["stations_searched"], 2500)                 # 재고가 바뀌어도 같은 대여소를 두 번 세지 않는다
        self.assertGreater(b["meta"]["stations_searched"], 2300)

    def test_badly_incomplete_result_is_called_unreliable(self) -> None:
        import random
        base = [{"ID": f"M{i}"} for i in range(2000)]
        n = {"c": 0}

        def lossy(start, end, params):                    # 뒤쪽 절반은 끝내 오지 않고, 앞쪽 절반이 순서만 바뀌어 두 번 온다
            n["c"] += 1
            half = base[:1000]
            random.Random(n["c"]).shuffle(half)
            return envelope("lossy", half, 2000)
        self.api.handlers["lossy"] = lossy
        b = self.ok("dataset_query", service="lossy", metrics=["count"])
        self.assertFalse(b["meta"]["scan_complete"])
        self.assertEqual(b["meta"]["rows_missing_pct"], 50.0)
        self.assertTrue(b["notices"][0].startswith("믿기 어려운 값입니다"), b["notices"])

    def test_more_date_spellings(self) -> None:
        self.api.handlers["x"] = table("x", [{"T": "가", "D": "20260715", "P": "2026.07.15"}, {"T": "나", "D": "20260815", "P": "2026.08.15"}])
        q = lambda *w: [r["T"] for r in self.ok("dataset_query", service="x", where=list(w), fields=["T"])["rows"]]   # noqa: E731
        for cond in ("D>=2026.08.01", "D>=2026/08/01", "D>=2026-8-1", "P>=20260801", "P>=2026-08-01"):
            self.assertEqual(q(cond), ["나"], cond)

    def test_date_condition_on_a_non_date_field_is_announced(self) -> None:
        self.api.handlers["x"] = table("x", [{"T": "가", "D": "칠월"}, {"T": "나", "D": "팔월"}])
        b = self.ok("dataset_query", service="x", where=["D>=2026-08-01"])
        self.assertTrue(any("날짜 꼴로 읽히지 않아" in n for n in b["notices"]))

    def test_sample_mode_answer_is_not_cached_under_the_asked_name(self) -> None:
        """체험 모드에서 강남역을 물어 받은 광화문 응답이 '강남역' 이름으로 캐시되면, 키를 넣은 뒤 60초간 틀린 오류가 난다."""
        m.set_client(sc.Client(fetch=self.api))
        self.api.handlers["citydata"] = lambda s, e, p: fx("citydata.json")
        self.bad("place_now", "SAMPLE_PLACE_ONLY", place="강남역")
        self.assertIsNone(m.client().cache.get("place|citydata|강남역"))

    def test_error_responses_respect_the_size_limit(self) -> None:
        wide = [{f"아주긴필드이름_{i:03d}_" + "가" * 280: "1" for i in range(200)}]
        self.api.handlers["x"] = table("x", wide)
        body, err = m.run_tool("dataset_query", {"service": "x", "where": ["없는필드>1"]})
        self.assertTrue(err)
        self.assertLessEqual(len(json.dumps(body, ensure_ascii=False)), m.MAX_CHARS)

    def test_section_values_are_checked(self) -> None:
        self.bad("place_now", "BAD_INPUT", place="강남역", sections=[1])
        self.bad("place_now", "BAD_INPUT", place="강남역", sections=["없는섹션"])

    def test_describe_carries_a_real_row(self) -> None:
        self.api.handlers["culturalEventInfo"] = lambda s, e, p: fx("culturalEventInfo.json")
        b = self.ok("dataset_describe", dataset="OA-15486")
        self.assertIn("CODENAME", b["sample_row"])                                 # 코드 필드에 어떤 값이 드는지 보여 준다

    def test_setup_with_nothing_typed_does_not_claim_to_save(self) -> None:
        import getpass, io, contextlib
        real = getpass.getpass
        getpass.getpass = lambda prompt="": ""
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = m._setup_cli()
        finally:
            getpass.getpass = real
        self.assertEqual(code, 1)
        self.assertIn("저장하지 않았습니다", out.getvalue())
        self.assertNotIn("저장했습니다", out.getvalue())

    def test_chunked_scan_warns_that_sums_are_approximate(self) -> None:
        self.api.handlers["x"] = table("x", [{"N": str(i)} for i in range(3000)])
        b = self.ok("dataset_query", service="x", metrics=["count"], scan=1000)
        self.assertTrue(any("조각을 더한 값은" in n and "어림값" in n for n in b["notices"]), b["notices"])


class RoundFourTest(Base):
    def test_date_shapes_are_reconciled(self) -> None:
        rows = [{"T": "가", "END_DATE": "2026-11-13 00:00:00.0", "CTRT_DAY": "20260715"},
                {"T": "나", "END_DATE": "2026-11-14 00:00:00.0", "CTRT_DAY": "20260801"},
                {"T": "다", "END_DATE": "2026-11-20 00:00:00.0", "CTRT_DAY": "20260815"}]
        self.api.handlers["x"] = table("x", rows)
        q = lambda *w: [r["T"] for r in self.ok("dataset_query", service="x", where=list(w), fields=["T"])["rows"]]   # noqa: E731
        self.assertEqual(q("END_DATE>=20261114"), ["나", "다"])          # 데이터는 줄표, 조건은 붙여 써도 같다
        self.assertEqual(q("END_DATE<=20261114"), ["가", "나"])
        self.assertEqual(q("CTRT_DAY>=2026-08-01"), ["나", "다"])        # 반대 방향도
        self.assertEqual(q("CTRT_DAY>=202608"), ["나", "다"])
        self.assertEqual(q("CTRT_DAY=2026-08-15"), ["다"])

    def test_amounts_are_not_mistaken_for_dates(self) -> None:
        self.api.handlers["x"] = table("x", [{"T": "가", "AMT": "200000"}, {"T": "나", "AMT": "1990000"}, {"T": "다", "AMT": "95000"}])
        got = [r["T"] for r in self.ok("dataset_query", service="x", where=["AMT>=199000"], fields=["T"])["rows"]]
        self.assertEqual(got, ["가", "나"])

    def test_sort_with_a_stray_text_value(self) -> None:
        self.api.handlers["x"] = table("x", [{"V": v} for v in ("9", "85", "100", "-", "23")])
        self.assertEqual([r["V"] for r in self.ok("dataset_query", service="x", sort="-V")["rows"]], ["100", "85", "23", "9", "-"])

    def test_min_max_on_dates(self) -> None:
        self.api.handlers["x"] = table("x", [{"D": "2026-01-02"}, {"D": "2026-03-09"}, {"D": ""}])
        row = self.ok("dataset_query", service="x", metrics=["min:D", "max:D"])["rows"][0]
        self.assertEqual((row["min_D"], row["max_D"]), ("2026-01-02", "2026-03-09"))

    def test_common_cannot_break_the_size_limit(self) -> None:
        fat = [{f"C{i}": "가" * 500 for i in range(100)} | {"N": str(n)} for n in range(3)]
        self.api.handlers["x"] = table("x", fat)
        self.assertLessEqual(len(json.dumps(self.ok("dataset_query", service="x"), ensure_ascii=False)), m.MAX_CHARS)

    def test_input_edges(self) -> None:
        self.bad("bike_stations", "BAD_INPUT", lat=float("nan"), lng=127.0)
        self.bad("bike_stations", "BAD_INPUT", lat=999, lng=127.0)
        self.bad("dataset_query", "BAD_INPUT", service="x", group_by=[["G"]])
        self.bad("dataset_query", "BAD_INPUT", service="x", where=[f"A={i}" for i in range(12)])
        self.assertIsNone(m.safe_handle({"jsonrpc": "2.0", "id": 15, "result": {}}))     # 응답 메시지에는 답하지 않는다

    def test_parse_error_mid_scan_keeps_rows(self) -> None:
        rows = [{"i": str(i)} for i in range(3000)]
        n = {"c": 0}

        def h(start, end, params):
            n["c"] += 1
            return b"<html>maintenance</html>" if n["c"] == 3 else envelope("big", rows[start - 1:end], len(rows))
        self.api.handlers["big"] = h
        b = self.ok("dataset_query", service="big", metrics=["count"])
        self.assertEqual(b["rows"][0]["count"], 2000)
        self.assertFalse(b["meta"]["scan_complete"])

    def test_subway_hint_for_stations_inside_a_place(self) -> None:
        m.set_client(sc.Client(key=KEY, fetch=self.api))
        nxt = self.bad("subway_arrivals", "SAMPLE_STATION_ONLY", station="성수")["error"]["next"]
        self.assertIn('place_now {"place": "성수카페거리", "sections": ["subway"]}', nxt)

    def test_subway_only_section_has_as_of_and_arvtime_note(self) -> None:
        self.api.handlers["citydata"] = lambda s, e, p: fx("citydata.json")
        b = self.ok("place_now", place="광화문·덕수궁", sections=["subway"])
        self.assertTrue(b["meta"]["as_of"])
        self.assertEqual(b["meta"]["as_of_is"], "조회 시각")
        self.assertTrue(any("SUB_ARVTIME이 0이면" in n for n in b["notices"]))

    def test_notices_of_dropped_sections_are_dropped(self) -> None:
        self.api.handlers["citydata"] = lambda s, e, p: big_city(p[0])
        b = self.ok("place_now", places=["강남역", "성수동", "코엑스", "서울역", "여의도"], sections=list(m.SECTION_NAMES))
        gone = set(m.SECTION_NAMES) - set(b["meta"]["sections"])
        self.assertTrue(gone)
        self.assertFalse(any(any(f"{g} 정보는" in n for g in gone) for n in b["notices"]))
        self.assertTrue({"population", "weather"} <= set(b["meta"]["sections"]))

    def test_population_only_compare_has_no_list_notice(self) -> None:
        self.api.handlers["citydata_ppltn"] = lambda s, e, p: json.dumps(
            {"SeoulRtd.citydata_ppltn": [dict(json.loads(fx("citydata.json"))["CITYDATA"]["LIVE_PPLTN_STTS"][0], AREA_NM=p[0])]},
            ensure_ascii=False).encode()
        b = self.ok("place_now", places=["강남역", "서울역"], sections=["population"])
        self.assertFalse(any("주차장, 정류소" in n for n in b["notices"]))


class PluginLayoutTest(unittest.TestCase):
    """Claude Code와 Codex가 같은 저장소를 플러그인으로 읽는다. 두 매니페스트가 어긋나면 한쪽만 깨진다."""

    def test_manifests_agree(self):
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        for key in ("name", "version", "description"):
            self.assertEqual(claude[key], codex[key], key)

    def test_codex_paths_exist(self):
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / codex["skills"] / "seoul-data" / "SKILL.md").is_file())
        mcp = json.loads((ROOT / codex["mcpServers"]).read_text(encoding="utf-8"))
        server = mcp["mcpServers"]["seoul-data"]
        self.assertEqual(server["cwd"], ".")           # Codex는 ${CLAUDE_PLUGIN_ROOT}를 풀지 않는다
        self.assertTrue((ROOT / server["args"][0]).is_file())

    def test_no_root_skill(self):
        self.assertFalse((ROOT / "SKILL.md").exists())  # 본문이 두 벌이 되지 않게 한다


if __name__ == "__main__":
    unittest.main(verbosity=2)
