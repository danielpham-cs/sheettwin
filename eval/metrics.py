"""Metrics (design §11.3) and comparison of runs.

    uv run python -m eval.metrics compare results/b0.json results/b1.json results/b2.json

Answer scoring:
    number  any number in the answer (read either way: 1,234.5 or 1.234,5) within relative `tolerance` of the gold
    text    any of `accept` (or the answer) appears in the reply, compared unaccented and lowercase, as whole words
    list    every item appears
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

from openpyxl.utils import range_boundaries
from unidecode import unidecode

from sheetir.understand import _NUMBER, _readings


def _norm(s: str) -> str:
    """Unaccented, lowercase, without markdown emphasis and code marks ('Tháng **12**' -> 'thang 12')."""
    s = re.sub(r"[*_`]+", "", str(s))
    return re.sub(r"\s+", " ", unidecode(s).lower()).strip()


def numbers_in(text: str) -> list[float]:
    out = []
    for raw, pct in _NUMBER.findall(text or ""):
        for x, _ in _readings(raw):
            out.append(x / 100 if pct else x)
            out.append(x)
    return out


def correct(q: dict, reply: str) -> bool:
    t = q["answer_type"]
    if t == "number":
        gold = float(q["answer"])
        tol = float(q.get("tolerance", 1e-3))
        return any(abs(x - gold) <= tol * max(1.0, abs(gold)) for x in numbers_in(reply))
    text = _norm(reply)

    def has(item):
        it = _norm(item)
        return bool(it) and re.search(r"(?<![a-z0-9])" + re.escape(it) + r"(?![a-z0-9])", text) is not None
    if t == "text":
        return any(has(a) for a in [*q.get("accept", []), q["answer"]])
    if t == "list":
        return all(has(a) for a in q["answer"])
    return False


def iou(a: str, b: str) -> float:
    a1, b1 = range_boundaries(a), range_boundaries(b)
    ix = max(0, min(a1[2], b1[2]) - max(a1[0], b1[0]) + 1)
    iy = max(0, min(a1[3], b1[3]) - max(a1[1], b1[1]) + 1)
    inter = ix * iy
    area = lambda r: (r[2] - r[0] + 1) * (r[3] - r[1] + 1)  # noqa: E731
    return inter / (area(a1) + area(b1) - inter)


def detection(gold: list[dict], predicted: list[dict], threshold: float = 0.9) -> dict:
    """Greedy one-to-one matching by IoU per sheet. predicted: {sheet, range, header_rows, total_rows}."""
    pairs = sorted(((iou(g["range"], p["range"]), gi, pi) for gi, g in enumerate(gold) for pi, p in enumerate(predicted)
                    if g["sheet"] == p["sheet"]), reverse=True)
    used_g, used_p, matches = set(), set(), []
    for score, gi, pi in pairs:
        if score < threshold or gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        matches.append((gold[gi], predicted[pi]))
    tp = len(matches)
    gold_totals = sum(len(g["total_rows"]) for g in gold)
    found_totals = sum(len(set(g["total_rows"]) & set(p["total_rows"])) for g, p in matches)
    return {"tp": tp, "gold": len(gold), "predicted": len(predicted),
            "header_ok": sum(g["header_rows"] == p["header_rows"] for g, p in matches),
            "gold_totals": gold_totals, "found_totals": found_totals}


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def summarize(records: list[dict], detect: list[dict] | None = None, families: list[dict] | None = None) -> dict:
    """Per-category and overall answer accuracy, plus the other metrics of §11.3 where the records carry them."""
    out: dict = {"questions": len(records)}
    cats: dict[str, list] = {}
    for r in records:
        cats.setdefault(r["category"], []).append(r["correct"])
    out["accuracy"] = _pct(sum(r["correct"] for r in records), len(records))
    out["by_category"] = {c: {"n": len(v), "accuracy": _pct(sum(v), len(v))} for c, v in sorted(cats.items())}
    lat = sorted(r["seconds"] for r in records if r.get("seconds") is not None)
    if lat:
        out["latency_p50"] = round(statistics.median(lat), 2)
        out["latency_p95"] = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2)
    steps = [r["steps"] for r in records if r.get("steps") is not None]
    if steps:
        out["steps_mean"] = round(statistics.mean(steps), 2)
    for k in ("prompt_tokens", "completion_tokens", "calls"):
        vals = [r["usage"][k] for r in records if r.get("usage")]
        if vals:
            out[f"{k}_mean"] = round(statistics.mean(vals), 1)
    runs = [x for r in records for x in r.get("sql_runs", [])]
    if runs:
        out["sql_exec_success"] = _pct(sum(x["ok"] for x in runs), len(runs))
    rec = [r["retrieval_hit"] for r in records if r.get("retrieval_hit") is not None]
    if rec:
        out["retrieval_recall_at_k"] = _pct(sum(rec), len(rec))
    prov = [r["provenance_recall"] for r in records if r.get("provenance_recall") is not None]
    if prov:
        out["provenance_recall"] = round(100 * statistics.mean(prov), 1)
    out["grounded"] = _pct(sum(bool(r.get("grounded")) for r in records), len(records))
    if detect:
        tp = sum(d["tp"] for d in detect)
        g = sum(d["gold"] for d in detect)
        p = sum(d["predicted"] for d in detect)
        prec, recall = (tp / p if p else 0), (tp / g if g else 0)
        out["detect"] = {"precision": round(100 * prec, 1), "recall": round(100 * recall, 1),
                         "f1": round(100 * (2 * prec * recall / (prec + recall)) if prec + recall else 0, 1),
                         "header_accuracy": _pct(sum(d["header_ok"] for d in detect), tp),
                         "total_row_recall": _pct(sum(d["found_totals"] for d in detect),
                                                  sum(d["gold_totals"] for d in detect))}
    if families:
        out["family_accuracy"] = _pct(sum(f["ok"] for f in families), len(families))
    return out


def compare(paths: list[str]) -> str:
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    cats = sorted({c for r in runs for c in r["summary"]["by_category"]})
    head = "| metric | " + " | ".join(r["system"] for r in runs) + " |"
    lines = [head, "|---|" + "---|" * len(runs)]

    def row(label, get):
        vals = []
        for r in runs:
            try:
                v = get(r["summary"])
            except (KeyError, TypeError):
                v = None
            vals.append("—" if v is None else str(v))
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    row("accuracy (%)", lambda s: s["accuracy"])
    for c in cats:
        row(f"  {c} (%)", lambda s, c=c: f"{s['by_category'][c]['accuracy']} (n={s['by_category'][c]['n']})")
    for k, label in [("latency_p50", "latency p50 (s)"), ("latency_p95", "latency p95 (s)"), ("steps_mean", "steps"),
                     ("calls_mean", "model calls"), ("prompt_tokens_mean", "prompt tokens"),
                     ("completion_tokens_mean", "completion tokens"), ("sql_exec_success", "SQL exec success (%)"),
                     ("retrieval_recall_at_k", "retrieval recall@k (%)"), ("provenance_recall", "provenance recall (%)"),
                     ("grounded", "grounded answers (%)"), ("family_accuracy", "family accuracy (%)")]:
        row(label, lambda s, k=k: s.get(k))
    for k in ("precision", "recall", "f1", "header_accuracy", "total_row_recall"):
        row(f"detect {k} (%)", lambda s, k=k: (s.get("detect") or {}).get(k))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("results", nargs="+")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(compare(a.results))


if __name__ == "__main__":
    main()
