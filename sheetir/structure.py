"""L2/L3: detect tables, classify cell roles, find parameters and formula anomalies.

Everything here is deterministic. Names are slugs of nearby labels/headers; an LLM
may later replace them (only names change, never structure).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .loader import CellKey, RawCell, RawWorkbook
from .refs import Ref, parse_ref

_TOTAL_RE = re.compile(r"\b(total|sum|subtotal|grand total)\b|合計|總計|小計|总计|合计", re.I)
_UNIT_RE = re.compile(r"\s*[\(（\[]([^\)）\]]*)[\)）\]]\s*")


def split_label(label: str) -> tuple[str, str | None]:
    """'Copper price (TWD/kg)' -> ('copper_price', 'TWD/kg')."""
    unit = None
    m = _UNIT_RE.search(label)
    if m:
        unit = m.group(1).strip() or None
        label = _UNIT_RE.sub(" ", label)
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)  # LaborRate -> Labor Rate
    name = re.sub(r"\W+", "_", label.strip().lower()).strip("_")
    if name and name[0].isdigit():
        name = "_" + name
    return name or "unnamed", unit


def unique(name: str, taken: set[str]) -> str:
    out, i = name, 2
    while out in taken:
        out, i = f"{name}_{i}", i + 1
    taken.add(out)
    return out


@dataclass
class Field:
    col: int
    header: str
    name: str
    unit: str | None
    kind: str  # data | derived
    key: str | None  # majority R1C1 key for derived fields
    number_format: str | None


@dataclass
class Table:
    table_id: int
    sheet: str
    name: str
    source: str  # listobject | detected
    header_row: int | None
    row_start: int
    row_end: int
    c1: int
    c2: int
    total_row: int | None = None
    key_col: int | None = None
    kind: str = "data"  # data | parameter_list
    fields: list[Field] = field(default_factory=list)

    def owns(self, row: int, col: int) -> bool:
        top = self.header_row or self.row_start
        bottom = self.total_row or self.row_end
        return top <= row <= bottom and self.c1 <= col <= self.c2

    def data_rows(self) -> range:
        return range(self.row_start, self.row_end + 1)


def _is_text(c: RawCell | None) -> bool:
    return c is not None and c.vtype == "text"


def _is_total_row(cells, sheet, row, t_start, c1, c2, refs_of) -> bool:
    for col in range(c1, c2 + 1):
        c = cells.get((sheet, row, col))
        if c is None:
            continue
        if _is_text(c) and _TOTAL_RE.search(str(c.value)):
            return True
        if c.vtype == "formula":
            for ref, _ in refs_of.get((sheet, row, col), []):
                if (ref.sheet == sheet and ref.c1 == ref.c2 == col and ref.r1 is not None
                        and ref.r2 < row and ref.r1 >= t_start and ref.r2 > ref.r1):
                    return True
    return False


def detect_tables(raw: RawWorkbook, refs_of: dict[CellKey, list]) -> list[Table]:
    cells = raw.cells
    tables: list[Table] = []
    claimed: set[CellKey] = set()

    def claim(t: Table):
        tables.append(t)
        top = t.header_row or t.row_start
        for r in range(top, (t.total_row or t.row_end) + 1):
            for c in range(t.c1, t.c2 + 1):
                claimed.add((t.sheet, r, c))

    for td in raw.tables:
        ref = parse_ref(td.ref)
        if ref is None or ref.r1 is None or ref.c1 is None:
            continue
        header = ref.r1 if td.header_rows else None
        start = ref.r1 + td.header_rows
        end = ref.r2 - td.totals_rows
        claim(Table(len(tables) + 1, td.sheet, td.name, "listobject", header, start, end,
                    ref.c1, ref.c2, total_row=ref.r2 if td.totals_rows else None))

    # A cell some formula points at individually is an input value, not a column header.
    pointed_at = {(r.sheet, r.r1, r.c1) for refs in refs_of.values() for r, _ in refs if r.is_cell}

    by_sheet: dict[str, dict[int, list[int]]] = {}
    for (sheet, row, col) in cells:
        by_sheet.setdefault(sheet, {}).setdefault(row, []).append(col)

    for sheet in raw.sheets:
        rows = by_sheet.get(sheet, {})
        for row in sorted(rows):
            cols = sorted(rows[row])
            # maximal runs of adjacent, unclaimed text cells = header candidates
            runs, run = [], []
            for col in cols:
                ck = (sheet, row, col)
                ok = _is_text(cells[ck]) and ck not in claimed and ck not in pointed_at
                if ok and run and col == run[-1] + 1:
                    run.append(col)
                else:
                    if len(run) >= 2:
                        runs.append(run)
                    run = [col] if ok else []
            if len(run) >= 2:
                runs.append(run)

            for run in runs:
                if any((sheet, row, c) in claimed for c in run):
                    continue
                below = [cells.get((sheet, row + 1, c)) for c in run]
                filled = [b for b in below if b is not None]
                if len(filled) < math.ceil(len(run) / 2):
                    continue
                if not any(b.vtype in ("number", "formula", "date", "bool") for b in filled):
                    continue
                end = row + 1
                while any((sheet, end + 1, c) in cells for c in run):
                    end += 1
                if end - row < 2:  # a single row under two labels is a label/value pair, not a table
                    continue
                total = None
                if end > row + 1 and _is_total_row(cells, sheet, end, row + 1, run[0], run[-1], refs_of):
                    total, end = end, end - 1
                claim(Table(len(tables) + 1, sheet, "", "detected", row, row + 1, end,
                            run[0], run[-1], total_row=total))

    taken: set[str] = set()
    per_sheet = Counter(t.sheet for t in tables)
    for t in tables:
        base = t.name if t.source == "listobject" else split_label(t.sheet)[0]
        if t.source != "listobject" and per_sheet[t.sheet] > 1 and t.header_row:
            first = cells.get((t.sheet, t.header_row, t.c1))
            base = f"{base}_{split_label(str(first.value))[0]}" if first else base
        t.name = unique(base, taken)
        _describe_table(t, cells)
    return tables


def _describe_table(t: Table, cells: dict[CellKey, RawCell]) -> None:
    taken: set[str] = set()
    for col in range(t.c1, t.c2 + 1):
        data = [cells.get((t.sheet, r, col)) for r in t.data_rows()]
        present = [c for c in data if c is not None]
        if t.key_col is None and present and len(present) == len(data) and all(_is_text(c) for c in present):
            values = [str(c.value).strip() for c in present]
            if len(set(values)) == len(values):
                t.key_col = col

        h = cells.get((t.sheet, t.header_row, col)) if t.header_row else None
        header = str(h.value) if h is not None and h.value is not None else f"col_{col}"
        name, unit = split_label(header)
        formulas = [c for c in present if c.vtype == "formula"]
        kind = "derived" if len(formulas) * 2 > len(data) else "data"
        key = None
        fmts = Counter(c.number_format for c in present)
        t.fields.append(Field(col, header, unique(name, taken), unit, kind, key,
                              fmts.most_common(1)[0][0] if fmts else None))


def assign_field_keys(t: Table, keys: dict[CellKey, str]) -> None:
    for f in t.fields:
        if f.kind == "derived":
            counts = Counter(keys[(t.sheet, r, f.col)] for r in t.data_rows() if (t.sheet, r, f.col) in keys)
            f.key = counts.most_common(1)[0][0] if counts else None


def field_anomalies(t: Table, cells: dict[CellKey, RawCell], keys: dict[CellKey, str]):
    """Cells in a derived column that break the column's pattern. Yields (cell, kind, detail)."""
    for f in t.fields:
        if f.kind != "derived" or f.key is None:
            continue
        for r in t.data_rows():
            ck = (t.sheet, r, f.col)
            c = cells.get(ck)
            if c is None:
                yield ck, "missing_formula", f"column '{f.header}' expects {f.key}"
            elif c.vtype != "formula":
                yield ck, "hardcoded_in_formula_column", f"constant {c.value!r} where column '{f.header}' computes {f.key}"
            elif keys[ck] != f.key:
                yield ck, "inconsistent_formula", f"{keys[ck]} differs from column pattern {f.key}"


def classify_roles(cells: dict[CellKey, RawCell], referenced: set[CellKey]) -> dict[CellKey, str]:
    roles = {}
    for ck, c in cells.items():
        if c.vtype == "formula":
            roles[ck] = "derived" if ck in referenced else "output"
        elif ck in referenced:
            roles[ck] = "input"
        else:
            roles[ck] = "label" if c.vtype == "text" else "orphan"
    return roles


def find_label(cells: dict[CellKey, RawCell], sheet: str, row: int, col: int) -> str | None:
    for c in range(col - 1, max(0, col - 4), -1):
        cell = cells.get((sheet, row, c))
        if cell is not None:
            return str(cell.value) if _is_text(cell) else None
    above = cells.get((sheet, row - 1, col))
    return str(above.value) if _is_text(above) else None


def cell_in(refs: list[Ref], sheet: str, row: int, col: int) -> bool:
    return any(r.sheet == sheet and r.contains(row, col) for r in refs)
