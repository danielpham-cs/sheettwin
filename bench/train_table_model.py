"""Train the learned header scorers used by table detection, and measure them with cross-validation.

Two binary classifiers share one feature vector (sheetir/tablemodel.py):
  header_row    is a candidate run of header-like cells a header row?
  header_block  is a row directly above a header row part of that header?
Labels come from StatSheets' annotations. HEADER there covers row labels in the body too, so a row
counts as a header row when it lies inside an annotated table, above the table's first row holding
a cell labeled DATA, and its non-empty cells are mostly labeled HEADER.

    uv run --with numpy --with lightgbm python bench/train_table_model.py <path-to>/dataset \\
        [--folds 5] [--jobs 6] [--export sheetir/models]

Cross-validation splits sheets by file. For each fold, both models are trained on the other folds and
table detection runs on the held-out sheets; the hand-weighted scorer runs on the same sheets for
comparison. With --export, final models trained on all sheets are written as JSON for
sheetir.tablemodel.TreeModel.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from eval_statsheets import match, report  # noqa: E402

EMPTY, HEADER, DATA = 0, 1, 2
PARAMS = dict(objective="binary", num_leaves=15, learning_rate=0.05, min_data_in_leaf=20,
              feature_fraction=0.9, lambda_l2=1.0, deterministic=True, seed=2112, verbose=-1, num_threads=4)
ROUNDS = 300


# ---- labels: StatSheets annotations live on a compressed grid --------------------------------------

def _compressed_index(occupied: set[int], last: int) -> dict[int, int]:
    """Original 1-based index -> compressed 0-based index. Runs of 3+ empty rows (columns) keep 2."""
    out, run, ci = {}, 0, 0
    for i in range(1, last + 1):
        if i in occupied:
            run = 0
        else:
            run += 1
            if run > 2:
                continue
        out[i] = ci
        ci += 1
    return out


def cell_labels(grid, sheet: str, npz) -> tuple[dict[tuple[int, int], int], bool]:
    """Annotated type of each (row, col) of the sheet, and whether the grid mapping checks out
    (the compressed table ranges map back onto the original ones)."""
    from openpyxl.utils import range_boundaries

    def empty(c):
        if c is None or c.vtype == "error":
            return True
        v = c.cached if c.vtype == "formula" else c.value
        return v is None or (isinstance(v, float) and v != v) or (isinstance(v, str) and v.strip().lower() in ("", "none"))

    positions = [(r, c) for (s, r, c) in [*grid.cells, *grid.cover] if s == sheet]
    filled = {(r, c) for r, c in positions if not empty(grid.at(sheet, r, c))}
    if not filled:
        return {}, False
    rows = _compressed_index({r for r, _ in filled}, max(r for r, _ in filled))
    cols = _compressed_index({c for _, c in filled}, max(c for _, c in filled))

    def ok(comp, orig):
        a, b = range_boundaries(comp), range_boundaries(orig)  # (c1, r1, c2, r2)
        inv_r = {v: k for k, v in rows.items()}
        inv_c = {v: k for k, v in cols.items()}
        try:
            return (inv_c[a[0] - 1], inv_r[a[1] - 1], inv_c[a[2] - 1], inv_r[a[3] - 1]) == b
        except KeyError:
            return False

    comp = [x.decode() if isinstance(x, bytes) else str(x) for x in npz["ranges"]]
    orig = [x.decode() if isinstance(x, bytes) else str(x) for x in npz["ranges_original"]]
    mapping_ok = all(ok(a, b) for a, b in zip(comp, orig))
    lab, (ar, ac) = npz["labels"], [int(v) for v in npz["anchor_labels"]]
    out = {}
    for r, ri in rows.items():
        for c, cj in cols.items():
            i, j = ri - ar, cj - ac
            if 0 <= i < lab.shape[0] and 0 <= j < lab.shape[1]:
                out[(r, c)] = int(lab[i, j])
    return out, mapping_ok


# ---- examples: computed by the same functions table detection uses ---------------------------------

def extract(xlsx: str, sheet: str, labels_path: str) -> dict:
    import numpy as np

    from sheetir.build import WorkbookResolver
    from sheetir.formula import parse_formula
    from sheetir.groups import group_formulas
    from sheetir.loader import load
    from sheetir.structure import header_candidates, make_grid
    from sheetir.tablemodel import FEATURES, block_features, header_features

    raw = load(xlsx)
    resolver = WorkbookResolver(raw)
    parsed = {ck: parse_formula(c.formula, c.sheet, c.row, c.col, resolver)
              for ck, c in raw.cells.items() if c.formula is not None and c.sheet == sheet}
    groups = group_formulas({ck: pf.key for ck, pf in parsed.items()})
    grid = make_grid(raw, {ck: pf.refs for ck, pf in parsed.items()},
                     {g.anchor for g in groups if g.axis == "col"})
    from openpyxl.utils import range_boundaries
    npz = np.load(labels_path, allow_pickle=False)
    labels, mapping_ok = cell_labels(grid, sheet, npz)
    tables = [range_boundaries(x.decode() if isinstance(x, bytes) else str(x)) for x in npz["ranges_original"]]

    def header_zone(row, cols):
        """Inside an annotated table that overlaps `cols`, above the table's first DATA row."""
        for c1, r1, c2, r2 in tables:
            if r1 <= row <= r2 and any(c1 <= c <= c2 for c in cols):
                first_data = next((r for r in range(r1, r2 + 1)
                                   if any(labels.get((r, c)) == DATA for c in range(c1, c2 + 1))), r2 + 1)
                if row < first_data:
                    return True
        return False

    def is_header(row, cols):
        types = [labels.get((row, c), EMPTY) for c in cols if grid.at(sheet, row, c) is not None]
        types = [t for t in types if t != EMPTY]
        return int(bool(types) and sum(t == HEADER for t in types) >= 0.5 * len(types) and header_zone(row, cols))

    filled = [(r, c) for (s, r, c) in grid.cells if s == sheet and (r, c) in labels]
    aligned = sum(labels[rc] != EMPTY for rc in filled) / max(len(filled), 1)

    header_rows, block_rows, seen = [], [], set()
    for row, run in header_candidates(grid, sheet):
        y = is_header(row, run)
        for lookahead in (True, False):
            f = header_features(grid, sheet, row, run, lookahead)
            if f is not None:
                header_rows.append(([f[k] for k in FEATURES], y))
        if not y or header_features(grid, sheet, row, run) is None:
            continue
        for r in range(row - 1, max(row - 11, 0), -1):  # the rows a header block could extend over
            if all(grid.at(sheet, r, c) is None for c in run):
                break
            if (r, run[0], run[-1]) not in seen:
                seen.add((r, run[0], run[-1]))
                f = block_features(grid, sheet, r, run)
                block_rows.append(([f[k] for k in FEATURES], is_header(r, run)))
    return {"mapping_ok": mapping_ok, "aligned": aligned, "labeled_cells": len(labels),
            "header": header_rows, "block": block_rows}


def run_worker(args: list[str], timeout: int) -> dict | list | None:
    try:
        proc = subprocess.run([sys.executable, __file__, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout)


# ---- training and export ---------------------------------------------------------------------------

def train(examples: list) -> "lightgbm.Booster":
    import lightgbm as lgb
    import numpy as np
    X = np.array([x for x, _ in examples], dtype=float)
    y = np.array([t for _, t in examples], dtype=float)
    return lgb.train(PARAMS, lgb.Dataset(X, y), num_boost_round=ROUNDS)


def export(booster, path: Path, meta: dict) -> None:
    """Write the trees as nested lists: [feature, threshold, left, right] or a leaf value."""
    from sheetir.tablemodel import FEATURES

    def node(n):
        if "leaf_value" in n:
            return round(n["leaf_value"], 8)
        assert n["decision_type"] == "<=", n["decision_type"]
        return [n["split_feature"], n["threshold"], node(n["left_child"]), node(n["right_child"])]

    dump = booster.dump_model()
    assert dump["feature_names"] == [f"Column_{i}" for i in range(len(FEATURES))]
    spec = {"features": FEATURES, "threshold": 0.5, "meta": meta,
            "trees": [node(t["tree_structure"]) for t in dump["tree_info"]]}
    path.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")


def check_parity(booster, path: Path, examples: list) -> float:
    """Largest difference between LightGBM's and the exported model's probabilities."""
    import numpy as np

    from sheetir.tablemodel import FEATURES, TreeModel
    m = TreeModel.load(path)
    X = np.array([x for x, _ in examples[:2000]], dtype=float)
    ours = [m.predict(dict(zip(FEATURES, row))) for row in X.tolist()]
    return float(np.max(np.abs(booster.predict(X) - np.array(ours)))) if len(X) else 0.0


def fold_of(file: str, k: int) -> int:
    return int(hashlib.sha1(file.encode()).hexdigest(), 16) % k


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--export", type=Path, help="directory to write the final models to")
    ap.add_argument("--out", type=Path, help="write per-sheet CV predictions (JSON lines)")
    ap.add_argument("--skip-cv", action="store_true", help="only train and export the final models")
    args = ap.parse_args()
    if args.skip_cv:
        args.folds = 0

    with open(args.dataset / "manifest.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["file_path"].lower().endswith(".xlsx")]

    def ex(r):
        return r, run_worker(["--extract", str(args.dataset / r["file_path"]), r["sheet_name"],
                              str(args.dataset / r["labels_path"])], args.timeout)
    with ThreadPoolExecutor(args.jobs) as pool:
        extracted = list(pool.map(ex, rows))
    usable = [(r, e) for r, e in extracted if e is not None and e["mapping_ok"] and e["labeled_cells"]]
    print(f"sheets {len(rows)}: usable {len(usable)}, failed {sum(e is None for _, e in extracted)}, "
          f"grid mapping mismatch {sum(e is not None and not e['mapping_ok'] for _, e in extracted)}")
    n_h = sum(len(e["header"]) for _, e in usable)
    n_b = sum(len(e["block"]) for _, e in usable)
    print(f"examples: header_row {n_h} ({sum(y for _, e in usable for _, y in e['header'])} positive), "
          f"header_block {n_b} ({sum(y for _, e in usable for _, y in e['block'])} positive)")

    recs = {"hand": [], "model": []}
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(args.folds):
            train_set = [e for r, e in usable if fold_of(r["file_path"], args.folds) != k]
            test_rows = [r for r, e in usable if fold_of(r["file_path"], args.folds) == k]
            d = Path(tmp) / f"fold{k}"
            d.mkdir()
            for name in ("header", "block"):
                examples = [x for e in train_set for x in e[name]]
                export(train(examples), d / f"header_{'row' if name == 'header' else 'block'}.json", {"fold": k})

            def det(r, spec):
                pred = run_worker(["--detect", str(args.dataset / r["file_path"]), r["sheet_name"], spec], args.timeout)
                import numpy as np
                gold = [g.decode() if isinstance(g, bytes) else str(g) for g in
                        np.load(args.dataset / r["labels_path"], allow_pickle=False)["ranges_original"]]
                rec = {"file": r["file_path"], "sheet": r["sheet_name"], "source": r["source"], "gold": gold,
                       "pred": pred or [], "seconds": 0.0, "fold": k}
                if pred is None:
                    rec["error"] = "failed"
                rec["ious"] = match(rec["pred"], gold)
                return rec
            with ThreadPoolExecutor(args.jobs) as pool:
                recs["model"] += list(pool.map(lambda r: det(r, str(d)), test_rows))
                recs["hand"] += list(pool.map(lambda r: det(r, "hand"), test_rows))
            print(f"fold {k}: {len(test_rows)} test sheets")

    for name in ("hand", "model") if args.folds else ():
        print(f"\n=== {name} scorer, {args.folds}-fold cross-validation (held-out sheets) ===")
        report(recs[name])
    if args.out:
        args.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for name in recs for r in
                                      [{**x, "scorer": name} for x in recs[name]]) + "\n", encoding="utf-8")

    if args.export:
        args.export.mkdir(parents=True, exist_ok=True)
        meta = {"trained": date.today().isoformat(), "data": "StatSheets (.xlsx sheets)",
                "sheets": len(usable), "params": PARAMS, "rounds": ROUNDS}
        for name, file in (("header", "header_row.json"), ("block", "header_block.json")):
            examples = [x for _, e in usable for x in e[name]]
            booster = train(examples)
            export(booster, args.export / file, {**meta, "examples": len(examples)})
            print(f"exported {args.export / file}: parity max |dp| = {check_parity(booster, args.export / file, examples):.2e}")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--extract":
        print(json.dumps(extract(*sys.argv[2:])))
    elif len(sys.argv) == 5 and sys.argv[1] == "--detect":
        from eval_statsheets import detect
        print(json.dumps(detect(*sys.argv[2:])))
    else:
        main()
