#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""범용 도구가 실제로 몇 건에 닿는지 잰다. 실제 사이트와 API를 부른다 (단위 테스트가 아니다).

    python3 tests/coverage_probe.py            # 무작위 60건
    python3 tests/coverage_probe.py 100 7      # 100건, 난수 씨앗 7

하는 일
1. 전체 목록(8천여 건)에서 무작위 쪽을 골라 Open API가 있는 데이터셋을 모은다.
2. 한 건씩 dataset_describe로 명세를 읽고, 명세의 예시 그대로 dataset_query를 부른다 (5행).
3. 결과를 분류해 references/coverage.md에 남긴다.

"5,891개 API에 닿는다"는 주장의 근거가 이 숫자다. 명세 화면이 바뀌면 여기서 먼저 드러난다.
사이트에 부담을 주지 않도록 호출 사이에 0.3초를 쉰다.
"""
from __future__ import annotations

import datetime
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "assets"))

import catalog                      # noqa: E402
import mcp_server as m              # noqa: E402


def main(argv: list[str]) -> int:
    n = int(argv[0]) if argv else 60
    seed = int(argv[1]) if len(argv) > 1 else 20261114
    rng = random.Random(seed)
    first = catalog.search("", 1)
    pages = max(first["total"] // catalog.PAGE_SIZE, 1)
    pool: dict[str, dict] = {}
    tried = 0
    while len(pool) < n and tried < n:                    # 쪽마다 10건이라 넉넉히 돈다
        tried += 1
        for it in catalog.search("", rng.randint(1, pages))["items"]:
            if it["has_api"]:
                pool[it["dataset"]] = it
        time.sleep(0.3)
    picks = rng.sample(sorted(pool), min(n, len(pool)))
    results = []
    for i, ds in enumerate(picks, 1):
        row = {"dataset": ds, "title": pool[ds]["title"], "category": pool[ds]["category"]}
        spec, err = m.run_tool("dataset_describe", {"dataset": ds})
        time.sleep(0.3)
        if err:
            row.update(stage="describe", outcome=spec["error"]["code"], detail=" ".join(spec["error"]["message"].split())[:60])
        else:
            row["service"] = spec["meta"]["service"]
            row["required"] = [f["name"] for f in spec["filters"] if f["required"]]
            body, err = m.run_tool("dataset_query", spec["example"]["arguments"])
            if err:
                row.update(stage="query", outcome=body["error"]["code"], detail=" ".join(body["error"]["message"].split())[:60])
            else:
                rows = body["rows"]
                labeled = len(body.get("fields", {}))
                row.update(stage="query", outcome="rows" if rows else "empty", rows=len(rows), labeled_fields=labeled)
            time.sleep(0.3)
        results.append(row)
        print(f"{i:3d}/{len(picks)} {ds:<9} {row['outcome']:<10} {row.get('service', ''):<32} {row['title'][:30]}", flush=True)

    total = len(results)
    count = lambda pred: sum(1 for r in results if pred(r))                       # noqa: E731
    described = count(lambda r: r["stage"] == "query")
    rows_ok = count(lambda r: r["outcome"] == "rows")
    empty = count(lambda r: r["outcome"] == "empty")
    failed = [r for r in results if r["outcome"] not in ("rows", "empty")]
    by_code: dict[str, int] = {}
    for r in failed:
        by_code[r["outcome"]] = by_code.get(r["outcome"], 0) + 1
    today = datetime.date.today().isoformat()
    lines = [
        "# 범용 도구 적중률 측정",
        "",
        f"측정일 {today}. `python3 tests/coverage_probe.py {n} {seed}`로 다시 잴 수 있다. "
        + ("샘플 키로 쟀다." if m.client().key_mode == "sample" else "실제 인증키로 쟀다."),
        "",
        "전체 목록에서 Open API가 있는 데이터셋을 무작위로 뽑아, 명세를 읽고(`dataset_describe`) 명세의 예시 그대로 "
        "5행을 불렀다(`dataset_query`).",
        "",
        "| 항목 | 건수 | 비율 |",
        "|---|---|---|",
        f"| 뽑은 데이터셋 | {total} | 100% |",
        f"| 명세를 읽음 (서비스명, 인자, 필드) | {described} | {described / total:.0%} |",
        f"| 호출해서 행을 받음 | {rows_ok} | {rows_ok / total:.0%} |",
        f"| 호출은 정상이나 데이터 없음 (INFO-200) | {empty} | {empty / total:.0%} |",
        f"| 실패 | {len(failed)} | {len(failed) / total:.0%} |",
        "",
    ]
    if failed:
        lines += ["## 실패한 것", "", "| 데이터셋 | 서비스 | 단계 | 코드 | 내용 |", "|---|---|---|---|---|"]
        lines += [f"| {r['dataset']} {r['title'][:24]} | {r.get('service', '')} | {r['stage']} | {r['outcome']} | {r.get('detail', '')} |"
                  for r in failed]
        lines += ["", "코드별: " + ", ".join(f"{k} {v}건" for k, v in sorted(by_code.items())), ""]
    lines += ["## 읽는 법", "",
              "- 이 숫자는 표본이다. 전체 5,891건에 대한 보장이 아니라 추정이다.",
              f"- 표본 중 필수 인자가 있는 데이터셋은 {count(lambda r: bool(r.get('required')))}건이다. 필수 인자를 조립하는 경로는 "
              "`--selftest`의 '필수 인자 조립' 항목(OA-2218)이 따로 확인한다.",
              "- '데이터 없음'은 도구가 맞게 불렀는데 그 조건의 행이 없다는 뜻이다. 실패로 세지 않는다.",
              "- ERROR-500은 서울시 서버가 그 서비스를 내리거나 고장 낸 경우가 많다. 사이트의 미리보기에서도 같은 오류가 난다.",
              "- 명세 읽기 비율이 떨어지면 열린데이터광장의 명세 화면 구조가 바뀐 것이다. `assets/catalog.py`의 `parse_spec`을 본다.",
              ""]
    (ROOT / "references").mkdir(exist_ok=True)
    (ROOT / "references" / "coverage.md").write_text("\n".join(lines), encoding="utf-8")
    (ROOT / "references" / "coverage.json").write_text(json.dumps(
        {"date": today, "n": total, "seed": seed, "described": described, "rows": rows_ok, "empty": empty,
         "failed": len(failed), "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n명세 읽음 {described}/{total}, 행 받음 {rows_ok}, 데이터 없음 {empty}, 실패 {len(failed)} {by_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
