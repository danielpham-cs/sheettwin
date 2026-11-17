"""Run a system over a dataset and score it (design §11.5).

    uv run python -m eval.run_eval --dataset eval/datasets/synthetic_v1 --system b2 --out results/b2_synth_v1.json
    uv run python -m eval.run_eval --dataset eval/datasets/synthetic_v1 --system b0
    uv run python -m eval.metrics compare results/b0_*.json results/b2_*.json

Systems: b0 (workbook dumped into the prompt), b1 (agent over raw sheets), b2 (agent over the catalog: the full
pipeline). The model comes from the usual settings (config/settings.yaml, .env). Each workbook's IR and catalog are
built once into <dataset>/.work/<workbook>/ and reused (delete the folder to rebuild); --rules-catalog builds the
catalog without the model (no model-written cards, no model for unsure tables).

Per question the run records the reply, whether it is correct, latency, steps, model calls and tokens, the queries
tried, whether the first search found the gold sheets (retrieval recall@k) and how many gold sheets the provenance
names. Table detection is scored against tables.jsonl, families against the revenue sheets.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sheetir.build import build_ir
from sheetir.catalog.store import CatalogHandle, build_catalog
from sheetir.llm import get_llm

from .metrics import correct, detection, summarize

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def prepare(dataset: Path, workbook: str, system: str, rules_catalog: bool):
    """(work folder, IR path, catalog or raw catalog) for one workbook, built once."""
    work = dataset / ".work" / Path(workbook).stem
    with _lock(str(work)):
        work.mkdir(parents=True, exist_ok=True)
        ir = work / "ir.db"
        if not ir.exists():
            build_ir(dataset / "workbooks" / workbook, ir, verify=False)
        if system == "b2":
            h = CatalogHandle.open(work)
            if h is None:
                h = build_catalog(ir, work, llm=None if rules_catalog else get_llm())
            return work, ir, h
        if system == "b1":
            from .baselines.b1_raw_sql import RawCatalog
            raw = RawCatalog(work) if (work / "raw.json").exists() else RawCatalog.build(ir, work)
            return work, ir, raw
        return work, ir, None


def run_question(q: dict, dataset: Path, system: str, rules_catalog: bool) -> dict:
    work, ir, cat = prepare(dataset, q["workbook"], system, rules_catalog)
    llm = get_llm()
    if llm is None:
        raise SystemExit("no language model configured (see config/settings.yaml or .env)")
    t0 = time.perf_counter()
    rec = {"id": q["id"], "workbook": q["workbook"], "category": q["category"], "question": q["question"],
           "gold": q["answer"]}
    try:
        if system == "b0":
            from .baselines import b0_dump
            out = b0_dump.run(q["question"], ir, llm)
            rec.update(reply=out["answer"], steps=1, grounded=False)
        else:
            from sheetir.agent.loop import answer
            r = answer(q["question"], cat, llm)
            gold_sheets = {s["sheet"] for s in q.get("gold_sources", [])}
            first = r.searches[0] if r.searches else None
            hit = None
            if first is not None and gold_sheets:
                covered = {s["sheet"] for cid in first if (c := cat.card(cid)) for s in c.sources}
                hit = gold_sheets <= covered
            prov = {p["sheet"] for p in r.provenance}
            rec.update(reply=r.answer, steps=len(r.steps), grounded=r.grounded, stopped=r.stopped,
                       sql_runs=r.sql_runs, retrieval_hit=hit,
                       provenance_recall=(len(gold_sheets & prov) / len(gold_sheets)) if gold_sheets else None,
                       trace=[{"tool": s.tool, "args": s.args, "result": s.result} for s in r.steps])
    except Exception as e:  # noqa: BLE001 - a failed question is scored wrong, the run goes on
        rec.update(reply="", error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-1500:], steps=0)
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    rec["usage"] = dict(llm.usage)
    rec["correct"] = correct(q, rec.get("reply") or "")
    return rec


def detect_scores(dataset: Path, workbooks: list[str]) -> tuple[list[dict], list[dict]]:
    gold = [json.loads(x) for x in (dataset / "tables.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    books = {json.loads(x)["workbook"]: json.loads(x) for x in
             (dataset / "workbooks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    det, fam = [], []
    for wb in workbooks:
        h = CatalogHandle.open(dataset / ".work" / Path(wb).stem)
        if h is None:
            continue
        preds = []
        for sp in h.specs():
            ct = h.tables.get(sp.table_id)
            preds.append({"sheet": sp.sheet, "range": sp.range, "header_rows": sp.header_rows,
                          "total_rows": sorted(set(sp.total_rows) | set(ct.subtotal_rows if ct else []))})
        det.append(detection([g for g in gold if g["workbook"] == wb], preds))
        if "split_period" in books.get(wb, {}).get("perturbations", []):
            want = {f"DT_T{m}" for m in range(1, 13)}
            got = [{h.tables[t].spec.sheet for t in f.member_table_ids if t in h.tables} for f in h.families]
            fam.append({"workbook": wb, "ok": want in got})
    return det, fam


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--system", choices=["b0", "b1", "b2"], default="b2")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int, help="first N questions only")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rules-catalog", action="store_true", help="build catalogs without the model")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    dataset = Path(a.dataset)
    qs = [json.loads(x) for x in (dataset / "questions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]
    if a.limit:
        qs = qs[: a.limit]
    workbooks = sorted({q["workbook"] for q in qs})
    t0 = time.perf_counter()
    # catalogs first, one workbook per worker, so questions do not wait on each other's builds
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda wb: prepare(dataset, wb, a.system, a.rules_catalog), workbooks))
    print(f"prepared {len(workbooks)} workbooks in {time.perf_counter() - t0:.0f}s")
    records = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for rec in ex.map(lambda q: run_question(q, dataset, a.system, a.rules_catalog), qs):
            records.append(rec)
            mark = "PASS" if rec["correct"] else "FAIL"
            print(f"{mark} {rec['id']:<14} {rec['category']:<18} {rec['seconds']:6.1f}s  "
                  f"gold={rec['gold']!s:<24.24} reply={(rec.get('reply') or rec.get('error') or '')[:90]!r}")
    det, fam = detect_scores(dataset, workbooks) if a.system == "b2" else (None, None)
    summary = summarize(records, det, fam)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"system": a.system, "dataset": str(dataset), "summary": summary,
                                           "records": records}, ensure_ascii=False, indent=1, default=str),
                               encoding="utf-8")


if __name__ == "__main__":
    main()
