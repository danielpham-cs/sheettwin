"""TableSpec + the IR's cells -> one clean table (design §6.4).

    grid = SheetGrid.load(con, sheet)
    rows, table = to_clean_table(spec, grid, duckdb_name)

`rows` are lists in column order, starting with `_row` (the Excel row each came from, for provenance), then the
`section` column when the body has section labels, then one value per spec column.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from openpyxl.utils import range_boundaries

from ..detect.models import TableSpec
from ..tablemodel import _TOTAL_RE
from .headers import identifiers
from .models import CleanTable, ColumnSpec
from .types import candidate, column_type
from .units import column_unit

NOTE_MIN_LEN = 40


@dataclass
class Cell:
    vtype: str
    value: object          # constant, or Excel's cached result for a formula (or SheetTwin's, see `computed`)
    number_format: str | None
    formula: str | None
    computed: bool = False  # the value was computed by sheetir.recalc: Excel stored none


class SheetGrid:
    """One sheet's cells from the IR database, with merged ranges resolved."""

    def __init__(self, sheet: str, cells: dict[tuple[int, int], Cell], merges: list[str]):
        self.sheet, self.cells = sheet, cells
        self.cover: dict[tuple[int, int], tuple[int, int]] = {}
        self.merges: list[tuple[int, int, int, int]] = []
        for ref in merges:
            c1, r1, c2, r2 = range_boundaries(ref)
            if (r2 - r1 + 1) * (c2 - c1 + 1) > 10_000:
                continue
            self.merges.append((c1, r1, c2, r2))
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    if (r, c) != (r1, c1):
                        self.cover[(r, c)] = (r1, c1)

    @classmethod
    def load(cls, con: sqlite3.Connection, sheet: str, computed: dict | None = None) -> "SheetGrid":
        """`computed`: {(sheet, row, col): value} for formulas Excel stored no result for (sheetir.recalc)."""
        computed = computed or {}
        cells = {}
        for row, col, vtype, value, cached, fmt, formula in con.execute(
                "SELECT row, col, vtype, value, cached, number_format, formula FROM cells WHERE sheet = ?", (sheet,)):
            raw = cached if formula is not None else value
            if formula is not None and raw is None and (sheet, row, col) in computed:
                cells[(row, col)] = Cell(vtype, computed[(sheet, row, col)], fmt, formula, computed=True)
                continue
            cells[(row, col)] = Cell(vtype, None if raw is None else json.loads(raw), fmt, formula)
        merges = [r for (r,) in con.execute("SELECT ref FROM merges WHERE sheet = ?", (sheet,))] \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'merges'").fetchone() else []
        return cls(sheet, cells, merges)

    def own(self, r: int, c: int) -> Cell | None:
        return self.cells.get((r, c))

    def at(self, r: int, c: int) -> Cell | None:
        """The cell at (r, c); inside a merged range, the cell holding its value."""
        return self.cells.get((r, c)) or self.cells.get(self.cover.get((r, c), (0, 0)))

    def used_range(self) -> tuple[int, int, int, int] | None:
        if not self.cells:
            return None
        rows = [r for r, _ in self.cells]
        cols = [c for _, c in self.cells]
        return min(cols), min(rows), max(cols), max(rows)

    def vertical_merge(self, r: int, c: int) -> bool:
        """Whether (r, c) lies in a range merged down (not across) below its top cell."""
        top = self.cover.get((r, c))
        return top is not None and top[1] == c and any(
            m[0] == m[2] == c and m[1] == top[0] and m[1] < r <= m[3] for m in self.merges)


def header_paths(spec: TableSpec, grid: SheetGrid) -> dict[int, list[str]]:
    """Each column's header levels, top to bottom: merged cells give their value to every column they span; an unmerged
    group label spans rightwards until the next label in its row (as in sheetir.structure._header_text)."""
    c1, _, c2, _ = spec.bounds
    out: dict[int, list[str]] = {}
    for col in range(c1, c2 + 1):
        parts: list[str] = []
        for i, r in enumerate(sorted(spec.header_rows)):
            c = grid.at(r, col)
            if c is None and i < len(spec.header_rows) - 1:
                c = next((grid.at(r, k) for k in range(col - 1, c1 - 1, -1) if grid.at(r, k) is not None), None)
                if c is not None and grid.vertical_merge(r, col):
                    c = None
            if c is not None and c.value is not None:
                text = re.sub(r"\s+", " ", str(c.value)).strip()
                if text and (not parts or parts[-1] != text):
                    parts.append(text)
        out[col] = parts
    return out


def _row_cells(grid: SheetGrid, r: int, c1: int, c2: int) -> list[tuple[int, Cell]]:
    return [(c, x) for c in range(c1, c2 + 1) if (x := grid.own(r, c)) is not None and x.value not in (None, "")]


def _is_subtotal(grid: SheetGrid, r: int, c1: int, c2: int, body_top: int) -> bool:
    """A total row inside the body: a total word in a text cell, or a SUM/SUBTOTAL over rows above it."""
    for c, x in _row_cells(grid, r, c1, c2):
        if x.vtype == "text" and _TOTAL_RE.search(str(x.value)):
            return True
        if x.formula and re.match(r"^=\s*(SUM|SUBTOTAL)\s*\(", x.formula, re.I):
            m = re.search(r"\(\s*(?:\d+\s*,\s*)?\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)\s*\)", x.formula)
            if m and m[1] == m[3] and int(m[2]) >= body_top and int(m[4]) < r:
                return True
    return False


def refine_spec(spec: TableSpec, grid: SheetGrid) -> TableSpec:
    """Fix two things detection often leaves: a second header row read as data (a row of labels over columns of
    numbers, e.g. 'Doanh thu | Số đơn' under a merged 'Kết quả'), and a total row followed by notes, which ends the
    range so the total is not seen as one. The rows are re-read, never moved outside the range."""
    c1, r1, c2, r2 = spec.bounds
    header = sorted(spec.header_rows)
    if header:
        for _ in range(2):
            r = header[-1] + 1
            if r >= r2:
                break
            filled = _row_cells(grid, r, c1, c2)
            if len(filled) < 2 or any(x.vtype != "text" for _, x in filled):
                break
            below = [x for rr in range(r + 1, min(r2, r + 6) + 1) for c, _ in filled
                     if (x := grid.own(rr, c)) is not None]
            if not below or sum(x.vtype in ("number", "formula") for x in below) * 2 < len(below):
                break
            header.append(r)
    totals = set(spec.total_rows)
    notes = set(spec.note_rows)
    r = r2
    while r > (header[-1] if header else r1):  # walk up over notes and blank rows to the last real row
        filled = _row_cells(grid, r, c1, c2)
        if not filled:
            r -= 1
            continue
        if len(filled) == 1 and filled[0][1].vtype == "text" and len(str(filled[0][1].value)) > NOTE_MIN_LEN:
            notes.add(r)
            r -= 1
            continue
        if r not in totals and _is_subtotal(grid, r, c1, c2, (header[-1] + 1) if header else r1):
            totals.add(r)
        break
    if header == sorted(spec.header_rows) and totals == set(spec.total_rows) and notes == set(spec.note_rows):
        return spec
    return spec.model_copy(update={"header_rows": header, "total_rows": sorted(totals), "note_rows": sorted(notes)})


def to_clean_table(spec: TableSpec, grid: SheetGrid, duckdb_name: str,
                   ffill_vertical_merges: bool = True) -> tuple[list[str], list[list], CleanTable]:
    """(column names incl. _row, rows, CleanTable). Never raises on odd content: problems go to `warnings`."""
    c1, r1, c2, r2 = spec.bounds
    body_top = spec.body_start
    paths = header_paths(spec, grid)
    cols = list(range(c1, c2 + 1))
    warnings: list[str] = []

    # ---- which body rows hold data ---------------------------------------------------------------------------------
    skip = set(spec.total_rows) | set(spec.note_rows)
    data_rows, subtotals, sections, notes = [], [], {}, []
    current = None
    for r in range(body_top, r2 + 1):
        if r in skip:
            continue
        filled = _row_cells(grid, r, c1, c2)
        if not filled:
            continue  # blank row
        if _is_subtotal(grid, r, c1, c2, body_top):
            subtotals.append(r)
            continue
        if len(filled) == 1 and filled[0][1].vtype == "text" and len(cols) >= 3:
            text = str(filled[0][1].value).strip()
            if len(text) > NOTE_MIN_LEN:
                notes.append(r)
                continue
            if filled[0][0] == c1:
                current = text  # a section label: it names the rows below it
                sections[r] = text
                continue
        data_rows.append(r)
        if current is not None:
            sections.setdefault(-r, current)
    spec = spec.model_copy(update={"note_rows": sorted(set(spec.note_rows) | set(notes))})

    # ---- columns ---------------------------------------------------------------------------------------------------
    # columns that are empty in the header and the body (spacers) are dropped
    keep = [c for c in cols if paths[c] or any(grid.own(r, c) is not None for r in data_rows)]
    headers = [" / ".join(paths[c]) if paths[c] else None for c in keep]
    names = identifiers(headers)
    values_by_col, specs = {}, []
    for c, name, header in zip(keep, names, headers):
        cands, formats = [], []
        for r in data_rows:
            x = grid.own(r, c)
            if x is None and ffill_vertical_merges and grid.vertical_merge(r, c):
                x = grid.at(r, c)
            if x is None:
                cands.append((None, None))
                continue
            cands.append(candidate(x.vtype, x.value, x.number_format))
            formats.append(x.number_format)
        dtype, values, dropped = column_type(cands)
        if dropped:
            warnings.append(f"column {name}: {dropped} value(s) did not fit type {dtype} and were left empty")
        formula_cells = [x for r in data_rows if (x := grid.own(r, c)) is not None and x.formula]
        if formula_cells and all(x.value is None for x in formula_cells):
            warnings.append(f"column {name}: formulas without a value stored by Excel (the file was not last saved "
                            "by Excel), so the column is empty")
        elif any(x.computed for x in formula_cells):
            n = sum(x.computed for x in formula_cells)
            warnings.append(f"column {name}: {n} formula value(s) computed by SheetTwin, since Excel stored none "
                            "(the file was not last saved by Excel)")
        unit = column_unit(header, formats)
        if dtype not in ("int", "float") and unit == "percent":
            unit = None
        present = [v for v in values if v is not None]
        distinct = list(dict.fromkeys(str(v) for v in present))
        specs.append(ColumnSpec(
            name=name, display_name=header or name, header_path=paths[c] or [name], dtype=dtype, unit=unit,
            null_ratio=round(1 - len(present) / len(values), 4) if values else 1.0, distinct_count=len(distinct),
            sample_values=distinct[:5], source_col=c))
        values_by_col[c] = values

    # ---- section column --------------------------------------------------------------------------------------------
    col_names = ["_row"]
    section_values = None
    if sections and any(k < 0 for k in sections):
        section_values = [sections.get(-r) for r in data_rows]
        present = [v for v in section_values if v]
        specs.insert(0, ColumnSpec(name="section", display_name="Section", header_path=["Section"], dtype="text",
                                   unit=None, null_ratio=round(1 - len(present) / len(section_values), 4),
                                   distinct_count=len(set(present)), sample_values=list(dict.fromkeys(present))[:5]))
        col_names.append("section")
    col_names += names

    rows = []
    for i, r in enumerate(data_rows):
        row = [r]
        if section_values is not None:
            row.append(section_values[i])
        row += [values_by_col[c][i] for c in keep]
        rows.append(row)

    # ---- totals check (design §6.4 step 9) -------------------------------------------------------------------------
    totals = {}
    for tr in spec.total_rows:
        for c, cs in zip(keep, [s for s in specs if s.source_col is not None]):
            if cs.dtype not in ("int", "float"):
                continue
            x = grid.own(tr, c)
            k, v = candidate(x.vtype, x.value, x.number_format) if x is not None else (None, None)
            if k not in ("int", "float") or not isinstance(v, (int, float)):
                continue
            s = sum(v_ for v_ in values_by_col[c] if v_ is not None)
            ok = abs(s - v) <= 1e-6 * max(1.0, abs(v))
            totals[cs.name] = ok
            if not ok:
                warnings.append(f"column {cs.name}: rows sum to {s:g} but the total row says {v:g} "
                                "(a subtotal row may be counted twice, or rows are missing)")
    table = CleanTable(table_id=spec.table_id, duckdb_name=duckdb_name, spec=spec, columns=specs, n_rows=len(rows),
                       totals_check=totals, subtotal_rows=subtotals, section_rows=sorted(k for k in sections if k > 0),
                       warnings=warnings)
    return col_names, rows, table
