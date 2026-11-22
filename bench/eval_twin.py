"""Measure the Python twin on a folder of real workbooks: translation coverage, V1 parity, V2 agreement.

    uv run python bench/eval_twin.py <folder> -o twin_eval.json [--jobs 4] [--scenarios 3] [--max-formulas 20000]

Each workbook is parsed into a temporary SheetIR database, translated and checked (sheetir.twin.check). The output
lists, per workbook, the coverage and the V1 / V2 counts, and overall: which functions block translation most often,
and the first mismatches, which are the cases to fix next.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheetir.build import build_ir  # noqa: E402
from sheetir.twin import check  # noqa: E402


def one(path: str, scenarios: int, max_formulas: int) -> dict:
    t0 = time.perf_counter()
    rec: dict = {"file": Path(path).name}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ir.db"
            res = build_ir(path, db, verify=False)
            rec["formulas"] = res.counts["formula_cells"]
            if res.counts["formula_cells"] == 0:
                rec["status"] = "no_formulas"
                return rec
            if res.counts["formula_cells"] > max_formulas:
                rec["status"] = "skipped_large"
                return rec
            r = check(db, xlsx=path, v2=scenarios > 0, scenarios=scenarios)
            v1, v2 = r["v1"], r["v2"] or {}
            rec.update(
                status=r["status"], coverage=r["coverage"],
                v1=v1["status"], v1_iso=v1.get("isolated"), v1_e2e=v1.get("end_to_end"),
                v1_mismatches=v1.get("mismatches", [])[:5],
                v2=v2.get("status"), v2_checked=v2.get("checked"), v2_passed=v2.get("passed"),
                v2_reference_errors=v2.get("reference_errors"), v2_mismatches=v2.get("mismatches", [])[:3],
                v2_summary=v2.get("summary"),
            )
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    return rec


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("-o", "--out", default="twin_eval.json")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--scenarios", type=int, default=3)
    p.add_argument("--max-formulas", type=int, default=20000)
    a = p.parse_args()
    files = sorted(str(f) for f in Path(a.folder).iterdir() if f.suffix.lower() in (".xlsx", ".xlsm"))
    rows = []
    with ProcessPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(one, f, a.scenarios, a.max_formulas) for f in files]
        for i, fut in enumerate(as_completed(futs), 1):
            rows.append(fut.result())
            if i % 25 == 0:
                print(f"{i}/{len(files)}", file=sys.stderr)
    checked = [r for r in rows if "coverage" in r]
    blockers: Counter = Counter()
    for r in checked:
        for u in r["coverage"]["untranslated"]:
            blockers[u["reason"]] += u["cells"]
    tot = lambda k, sub: sum((r.get(k) or {}).get(sub, 0) for r in checked)  # noqa: E731
    summary = {
        "workbooks": len(rows), "checked": len(checked),
        "status": Counter(r["status"] for r in rows),
        "formula_cells": sum(r["coverage"]["cells"] for r in checked),
        "cells_translated": sum(r["coverage"]["cells_translated"] for r in checked),
        "v1_isolated": {"checked": tot("v1_iso", "checked"), "passed": tot("v1_iso", "passed")},
        "v1_end_to_end": {"checked": tot("v1_e2e", "checked"), "passed": tot("v1_e2e", "passed")},
        "v2": {"checked": sum(r.get("v2_checked") or 0 for r in checked),
               "passed": sum(r.get("v2_passed") or 0 for r in checked)},
        "top_blockers": blockers.most_common(25),
    }
    Path(a.out).write_text(json.dumps({"summary": summary, "workbooks": rows}, indent=1, ensure_ascii=False,
                                      default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
