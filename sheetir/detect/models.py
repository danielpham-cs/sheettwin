"""TableSpec (design §5.2) and the conversion from the tables SheetIR detected and stored in its IR database.

Coordinates are 1-based, as in Excel; `range` is an A1 range covering the whole table: group-header rows, header,
body and total row. A table's id is f"{sheet_slug}__t{n}", n counting the sheet's tables top to bottom, left to right.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from openpyxl.utils import get_column_letter, range_boundaries
from pydantic import BaseModel

from ..clean.headers import slug

Source = Literal["rule:excel_table", "rule:auto_filter", "rule:named_range", "rule:heuristic", "user", "llm"]

_SOURCES = {"listobject": "rule:excel_table", "autofilter": "rule:auto_filter", "named_range": "rule:named_range",
            "detected": "rule:heuristic", "user": "user", "llm": "llm"}


class TableSpec(BaseModel):
    table_id: str
    sheet: str
    range: str
    header_rows: list[int]
    total_rows: list[int] = []
    note_rows: list[int] = []
    title: str | None = None
    confidence: float
    source: Source
    name: str | None = None            # SheetIR's name for the table (unique in the workbook)
    ir_table_id: int | None = None     # its row in the IR `tables` table, when it came from there
    low_confidence: bool = False       # kept although neither the rules nor the model were sure of it

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """(c1, r1, c2, r2)"""
        return range_boundaries(self.range)

    @property
    def body_start(self) -> int:
        return (max(self.header_rows) + 1) if self.header_rows else self.bounds[1]


def a1(c1: int, r1: int, c2: int, r2: int) -> str:
    return f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"


def _title(con: sqlite3.Connection, sheet: str, top: int, c1: int, c2: int) -> str | None:
    """A caption just above a table: the nearest of the 3 rows above holding only one or two text cells."""
    for r in range(top - 1, max(0, top - 4), -1):
        cells = con.execute("SELECT vtype, value FROM cells WHERE sheet = ? AND row = ? ORDER BY col",
                            (sheet, r)).fetchall()
        if not cells:
            continue
        if len(cells) <= 2 and all(v == "text" for v, _ in cells):
            return " ".join(str(json.loads(x)) for _, x in cells).strip() or None
        return None
    return None


def specs_from_ir(con: sqlite3.Connection) -> list[TableSpec]:
    """Every table in an IR database, as TableSpec."""
    rows = con.execute("SELECT table_id, sheet, name, source, header_row, row_start, row_end, col_start, col_end, "
                       "total_row, header_top, confidence FROM tables ORDER BY sheet, "
                       "COALESCE(header_top, header_row, row_start), col_start").fetchall()
    order = {s: i for i, s in enumerate(json.loads(
        (con.execute("SELECT value FROM meta WHERE key = 'sheets'").fetchone() or ["[]"])[0]))}
    rows.sort(key=lambda r: (order.get(r[1], 1e9), r[10] or r[4] or r[5], r[7]))
    seen: dict[str, int] = {}
    out = []
    for tid, sheet, name, source, header, start, end, c1, c2, total, top, conf in rows:
        seen[sheet] = seen.get(sheet, 0) + 1
        first = top or header or start
        out.append(TableSpec(
            table_id=f"{slug(sheet)}__t{seen[sheet]}", sheet=sheet, range=a1(c1, first, c2, total or end),
            header_rows=list(range(top or header, header + 1)) if header else [],
            total_rows=[total] if total else [], title=_title(con, sheet, first, c1, c2),
            confidence=1.0 if conf is None else float(conf), source=_SOURCES.get(source, "rule:heuristic"),
            name=name, ir_table_id=tid))
    return out


def validate_spec(spec: TableSpec, used: tuple[int, int, int, int] | None, others: list[TableSpec]) -> list[str]:
    """What is wrong with a spec (design §6.3.5), empty when nothing is. `used` is the sheet's used range (c1, r1, c2,
    r2); `others` are the sheet's other tables."""
    errors = []
    try:
        c1, r1, c2, r2 = spec.bounds
    except (ValueError, TypeError):
        return [f"range {spec.range!r} is not an A1 range"]
    if None in (c1, r1, c2, r2):
        return [f"range {spec.range!r} must name both corners"]
    if used and not (used[0] <= c1 and used[1] <= r1 and c2 <= used[2] and r2 <= used[3]):
        errors.append(f"range {spec.range} is outside the sheet's used range {a1(*used)}")
    hs = sorted(spec.header_rows)
    if hs and (hs[0] != r1 or hs != list(range(hs[0], hs[-1] + 1))):
        errors.append(f"header rows {hs} must be consecutive and start at the range's first row {r1}")
    if any(not r1 <= r <= r2 for r in [*spec.total_rows, *spec.note_rows]):
        errors.append("total and note rows must lie inside the range")
    data = [r for r in range(spec.body_start, r2 + 1) if r not in spec.total_rows and r not in spec.note_rows]
    if not data:
        errors.append("the table needs at least one data row below its header")
    for o in others:
        if o.table_id == spec.table_id:
            continue
        oc1, or1, oc2, or2 = o.bounds
        if not (c2 < oc1 or oc2 < c1 or r2 < or1 or or2 < r1):
            errors.append(f"range {spec.range} overlaps table {o.range}")
    return errors
