"""Header scoring for table detection: row features, the hand-weighted scorer, and a learned one.

Table detection asks two questions about a row, restricted to a range of columns:
  - header: is this run of cells the header row of a table below it?
  - block:  is this row, directly above a header row, part of the same header (group labels,
            units, series metadata), rather than a title or something else?

Both are answered from the same feature vector (`row_features`). `HandScorer` combines a few
features with hand-set weights. `ModelScorer` evaluates gradient-boosted trees trained on the
StatSheets benchmark (bench/train_table_model.py); the trees are stored as JSON next to this file
and evaluated here in plain Python, so no ML library is needed at runtime. The hand scorer is the
default; the learned one is opt-in (`sheetir build --tables model`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .loader import CellKey, RawCell

_TOTAL_RE = re.compile(r"\b(total|sum|subtotal|grand total)\b|合計|總計|小計|总计|合计", re.I)
HEADER_MARKS = frozenset({"bold", "fill", "border_bottom"})
MODEL_DIR = Path(__file__).with_name("models")


@dataclass
class Grid:
    """One workbook's cells as table detection sees them."""

    cells: dict[CellKey, RawCell]
    cover: dict[CellKey, CellKey]           # cell inside a merged range -> its top-left cell
    group_starts: set[CellKey] = field(default_factory=set)   # first cells of filled-down formula groups
    pointed_at: set[CellKey] = field(default_factory=set)     # cells some formula references individually
    row_cols: dict[str, dict[int, set[int]]] = field(default_factory=dict)  # sheet -> row -> occupied columns

    def at(self, sheet: str, row: int, col: int) -> RawCell | None:
        """The cell at a position; inside a merged range, the cell holding the merged value."""
        ck = (sheet, row, col)
        return self.cells.get(ck) or self.cells.get(self.cover.get(ck))


def is_value(c: RawCell | None) -> bool:
    return c is not None and c.vtype in ("number", "formula", "date", "bool")


def year_cols(grid: Grid, sheet: str, row: int, cols) -> set[int]:
    """Columns in a run of consecutive years (2023 | 2024 | 2025): header labels, not data."""
    def year(col):
        c = grid.at(sheet, row, col)
        v = None if c is None or c.vtype != "number" else c.value
        return v if isinstance(v, int) and not isinstance(v, bool) and 1900 <= v <= 2100 else None
    out = set()
    for col in cols:
        y = year(col)
        if y is not None and (year(col - 1) == y - 1 or year(col + 1) == y + 1):
            out.add(col)
    return out


def is_label(c: RawCell, col: int, years: set[int]) -> bool:
    return c.vtype in ("text", "date") or col in years


FEATURES = [
    "n_cols_log", "fill", "n_distinct_log", "text", "number", "date", "formula", "other_types", "year",
    "label_share", "bold", "fill_style", "border_bottom", "marked", "contrast", "contrast_any",
    "unique", "merged", "merged_across", "merged_down", "sparse", "dense", "stub_empty",
    "mean_len", "digit_text", "short_text", "total_word", "pointed",
    "dist_values", "below_fill", "below_value", "first_fill", "first_value", "formula_starts",
    "wider_left", "wider_right", "row_extra", "blank_above", "blank_below", "above_fill", "row_pos",
    "lookahead",
]


def row_features(grid: Grid, sheet: str, row: int, cols: list[int], first: int | None,
                 lookahead: bool = True) -> dict[str, float]:
    """Features of `row` over `cols`. `first` is the nearest row below holding values in those
    columns (None if there is none within reach): the header is compared with it."""
    at = grid.at
    n = len(cols)
    heads = [at(sheet, row, c) for c in cols]
    present = [h for h in heads if h is not None]
    distinct = list({id(h): h for h in present}.values())  # a merged cell counts once
    k = max(len(present), 1)
    years = year_cols(grid, sheet, row, cols)
    below_first = [at(sheet, first, c) for c in cols] if first else [None] * n
    nxt = [at(sheet, row + 1, c) for c in cols]
    above = [at(sheet, row - 1, c) for c in cols] if row > 1 else [None] * n

    def share(pred, items=heads, denom=k):
        return sum(1 for x in items if x is not None and pred(x)) / denom

    marked = [h is not None and bool(h.style & HEADER_MARKS)
              and (b is None or (b.style & HEADER_MARKS) != (h.style & HEADER_MARKS))
              for h, b in zip(heads, below_first)]
    labels = sum((h is not None and is_label(h, c, years)) or m for h, c, m in zip(heads, cols, marked)) / n
    contrast = sum(h is not None and is_label(h, c, years) and is_value(b) and not (h.vtype == b.vtype == "date")
                   for h, b, c in zip(heads, below_first, cols)) / n
    texts = [str(h.value) for h in distinct if h.vtype == "text"]
    covered = [grid.cover.get((sheet, row, c)) for c in cols]
    stub_free = heads[1:] if heads and heads[0] is None else heads
    occupied = grid.row_cols.get(sheet, {}).get(row, set())
    return {
        "n_cols_log": math.log1p(n),
        "fill": len(present) / n,
        "n_distinct_log": math.log1p(len(distinct)),
        "text": share(lambda h: h.vtype == "text"),
        "number": share(lambda h: h.vtype == "number"),
        "date": share(lambda h: h.vtype == "date"),
        "formula": share(lambda h: h.vtype == "formula"),
        "other_types": share(lambda h: h.vtype not in ("text", "number", "date")),
        "year": len(years) / n,
        "label_share": labels,
        "bold": share(lambda h: "bold" in h.style),
        "fill_style": share(lambda h: "fill" in h.style),
        "border_bottom": share(lambda h: "border_bottom" in h.style),
        "marked": sum(marked) / n,
        "contrast": contrast,
        "contrast_any": float(contrast > 0),
        "unique": float(len({str(h.value) for h in distinct}) == len(distinct)),
        "merged": sum(c is not None for c in covered) / n,
        "merged_across": float(any(a is not None and a == b for a, b in zip(covered, covered[1:]))),
        "merged_down": float(bool(cols) and grid.cover.get((sheet, row + 1, cols[0])) == (sheet, row, cols[0])),
        "sparse": float(bool(heads) and heads[0] is None and len(present) > 0
                        and all(heads[i + 1] is None for i, h in enumerate(heads[:-1]) if h is not None)),
        "dense": sum(h is not None for h in stub_free) / max(len(stub_free), 1),
        "stub_empty": float(bool(heads) and heads[0] is None),
        "mean_len": min(sum(len(t) for t in texts) / max(len(texts), 1), 100) / 100,
        "digit_text": sum(any(ch.isdigit() for ch in t) for t in texts) / max(len(texts), 1),
        "short_text": sum(len(t.strip()) <= 4 for t in texts) / max(len(texts), 1),
        "total_word": float(any(_TOTAL_RE.search(t) for t in texts)),
        "pointed": sum((h.sheet, h.row, h.col) in grid.pointed_at for h in distinct) / max(len(distinct), 1),
        "dist_values": float(first - row) if first else 0.0,
        "below_fill": sum(b is not None for b in nxt) / n,
        "below_value": sum(is_value(b) for b in nxt) / n,
        "first_fill": sum(b is not None for b in below_first) / n,
        "first_value": sum(is_value(b) for b in below_first) / n,
        "formula_starts": sum((sheet, row + 1, c) in grid.group_starts for c in cols) / n,
        "wider_left": float(bool(cols) and at(sheet, row, cols[0] - 1) is not None),
        "wider_right": float(bool(cols) and at(sheet, row, cols[-1] + 1) is not None),
        "row_extra": min(len(occupied - set(cols)) / n, 5.0),
        "blank_above": float(all(a is None for a in above)),
        "blank_below": float(all(b is None for b in nxt)),
        "above_fill": sum(a is not None for a in above) / n,
        "row_pos": min(row, 100) / 100,
        "lookahead": float(lookahead),
    }


MAX_LABEL_ROWS = 2   # label-only rows (units, section headings) allowed between a header and its first values
BLOCK_REACH = 12     # rows below a header-block row searched for the first values


def header_features(grid: Grid, sheet: str, row: int, run: list[int], lookahead: bool = True) -> dict | None:
    """Features of a candidate header run, or None when too little sits below it to be a table.

    The header is compared with the first row below that holds values; with `lookahead`, up to
    MAX_LABEL_ROWS rows of labels only (a units row such as '$m | $m') may sit in between.
    """
    heads = [grid.at(sheet, row, c) for c in run]
    if any(h is None for h in heads):
        return None
    below, first = [], row + 1
    for first in range(row + 1, row + 2 + (MAX_LABEL_ROWS if lookahead else 0)):
        below = [grid.at(sheet, first, c) for c in run]
        if any(is_value(b) for b in below):
            break
    if sum(b is not None for b in below) < math.ceil(len(run) / 2):
        return None
    return row_features(grid, sheet, row, run, first, lookahead)


def block_features(grid: Grid, sheet: str, row: int, cols: list[int]) -> dict:
    """Features of a row directly above a header, over the table's columns."""
    first = next((r for r in range(row + 1, row + 1 + BLOCK_REACH)
                  if any(is_value(grid.at(sheet, r, c)) for c in cols)), None)
    return row_features(grid, sheet, row, cols, first, lookahead=False)


class HandScorer:
    """Hand-set weights. Plain text labels over numbers pass on their own; formatting lets all-text
    tables and numeric headers pass, and formula columns raise the score."""

    name = "hand"
    threshold = 0.7
    W_LABELS, W_CONTRAST, W_CONTRAST_SHARE, W_STYLE, W_FORMULAS, W_UNIQUE = 0.4, 0.3, 0.15, 0.3, 0.3, 0.1

    def header(self, f: dict[str, float]) -> float:
        return (self.W_LABELS * f["label_share"] + self.W_CONTRAST * f["contrast_any"]
                + self.W_CONTRAST_SHARE * f["contrast"] + self.W_STYLE * f["marked"]
                + self.W_FORMULAS * f["formula_starts"] + self.W_UNIQUE * f["unique"])

    def block(self, f: dict[str, float]) -> bool:
        """Group labels: two or more labels within the columns, merged across, merged down into the
        row below, sparse (spanning the blanks to their right), or dense (series metadata)."""
        if f["n_distinct_log"] < math.log1p(2) or f["other_types"] > 0 or f["pointed"] > 0:
            return False
        if f["wider_left"] or f["wider_right"]:
            return False  # the row is wider than the table, so it is something else
        return bool(f["merged_across"] or f["merged_down"] or f["sparse"] or f["dense"] >= 0.8)


class TreeModel:
    """A LightGBM binary classifier, as exported by bench/train_table_model.py."""

    def __init__(self, spec: dict):
        self.features: list[str] = spec["features"]
        self.trees: list = spec["trees"]
        self.threshold: float = spec.get("threshold", 0.5)

    @classmethod
    def load(cls, path: str | Path) -> "TreeModel":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def predict(self, f: dict[str, float]) -> float:
        x = [f[name] for name in self.features]
        raw = 0.0
        for node in self.trees:
            while isinstance(node, list):  # [feature, threshold, left, right]; x <= threshold goes left
                node = node[2] if x[node[0]] <= node[1] else node[3]
            raw += node
        return 1.0 / (1.0 + math.exp(-raw))


class ModelScorer:
    """Learned header and header-block classifiers; a row passes when its probability reaches the
    model's threshold."""

    name = "model"

    def __init__(self, header: TreeModel, block: TreeModel):
        self.header_model, self.block_model = header, block
        self.threshold = header.threshold

    def header(self, f: dict[str, float]) -> float:
        return self.header_model.predict(f)

    def block(self, f: dict[str, float]) -> bool:
        return self.block_model.predict(f) >= self.block_model.threshold

    @classmethod
    def load(cls, directory: str | Path = MODEL_DIR) -> "ModelScorer | None":
        d = Path(directory)
        if not (d / "header_row.json").exists() or not (d / "header_block.json").exists():
            return None
        return cls(TreeModel.load(d / "header_row.json"), TreeModel.load(d / "header_block.json"))


def get_scorer(name: str = "hand") -> HandScorer | ModelScorer:
    """"hand" (the default): hand-set weights, which also cover business workbooks (formula columns,
    styled all-text tables, form fields above a table). "model": the classifiers trained on StatSheets,
    more accurate on statistical tables (see README) but trained without formulas."""
    if name == "hand":
        return HandScorer()
    if name == "model":
        scorer = ModelScorer.load()
        if scorer is None:
            raise FileNotFoundError(f"no table model in {MODEL_DIR}; train one with bench/train_table_model.py")
        return scorer
    raise ValueError(f"unknown table scorer {name!r}; use 'hand' or 'model'")


def default_scorer() -> HandScorer:
    return HandScorer()
