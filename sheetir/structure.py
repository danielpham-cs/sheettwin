"""L2/L3: detect tables, classify cell roles, find parameters and formula anomalies.

Everything here is deterministic. Names are slugs of nearby labels/headers; an LLM
may later replace them (only names change, never structure).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from openpyxl.utils import range_boundaries

from .loader import CellKey, RawCell, RawWorkbook
from .refs import Ref, parse_ref
from .tablemodel import (
    HEADER_MARKS, Grid, block_features, default_scorer, header_features, is_label, is_value, year_cols,
)

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
    source: str  # listobject (declared in Excel) | detected (from the layout) | user (defined by the user)
    header_row: int | None  # bottom header row; its cells name the fields
    row_start: int
    row_end: int
    c1: int
    c2: int
    total_row: int | None = None
    key_col: int | None = None
    kind: str = "data"  # data | parameter_list
    fields: list[Field] = field(default_factory=list)
    header_top: int | None = None  # first row of a multi-row header (group labels above header_row)
    gap_rows: list[int] = field(default_factory=list)  # blank rows tolerated inside the body
    confidence: float = 1.0  # header score for detected tables; 1.0 for declared Excel Tables

    @property
    def top(self) -> int:
        return self.header_top or self.header_row or self.row_start

    @property
    def bottom(self) -> int:
        return self.total_row or self.row_end

    def owns(self, row: int, col: int) -> bool:
        return self.top <= row <= self.bottom and self.c1 <= col <= self.c2

    def data_rows(self) -> list[int]:
        return [r for r in range(self.row_start, self.row_end + 1) if r not in self.gap_rows]


def _is_text(c: RawCell | None) -> bool:
    return c is not None and c.vtype == "text"


def _is_total_row(at, sheet, row, t_start, c1, c2, refs_of) -> bool:
    for col in range(c1, c2 + 1):
        c = at(sheet, row, col)
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


# Whether a row is a header is scored (tablemodel.py): by a classifier trained on the StatSheets
# benchmark when its model files are packaged, else by hand-set weights. The rules here decide
# which rows are candidates and how far a table extends.
_MAX_PARENT_ROWS = 10    # header rows allowed above the bottom header row (group labels, series metadata)
_MAX_GAP_ROWS = 3        # rows empty across the table's columns that the body may continue past
_MAX_MERGE_CELLS = 10_000


def _merge_cover(raw: RawWorkbook) -> dict[CellKey, CellKey]:
    """Every cell inside a merged range -> the range's top-left cell, which holds the value."""
    cover: dict[CellKey, CellKey] = {}
    for sheet, rng in raw.merged:
        c1, r1, c2, r2 = range_boundaries(rng)
        if (r2 - r1 + 1) * (c2 - c1 + 1) > _MAX_MERGE_CELLS:
            continue
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                cover[(sheet, r, c)] = (sheet, r1, c1)
    return cover


def make_grid(raw: RawWorkbook, refs_of: dict[CellKey, list], group_starts=frozenset()) -> Grid:
    cover = _merge_cover(raw)
    row_cols: dict[str, dict[int, set[int]]] = {}
    for (sheet, row, col) in [*raw.cells, *cover]:
        row_cols.setdefault(sheet, {}).setdefault(row, set()).add(col)
    # A cell some formula points at individually is an input value, not a column header.
    pointed_at = {(r.sheet, r.r1, r.c1) for refs in refs_of.values() for r, _ in refs if r.is_cell}
    return Grid(raw.cells, cover, set(group_starts), pointed_at, row_cols)


def _header_capable(c: RawCell | None, col: int, years: set[int]) -> bool:
    if c is None:
        return False
    return is_label(c, col, years) or (c.vtype in ("number", "formula") and bool(c.style & HEADER_MARKS))


def header_candidates(grid: Grid, sheet: str, claimed: set[CellKey] = frozenset()):
    """(row, columns) of every run of two or more adjacent, unclaimed header-like cells."""
    rows = grid.row_cols.get(sheet, {})
    for row in sorted(rows):
        cols = sorted(rows[row])
        years = year_cols(grid, sheet, row, cols)
        run: list[int] = []
        for col in cols:
            c = grid.at(sheet, row, col)
            ok = (_header_capable(c, col, years) and (sheet, row, col) not in claimed
                  and (c.sheet, c.row, c.col) not in grid.pointed_at)
            if ok and run and col == run[-1] + 1:
                run.append(col)
            else:
                if len(run) >= 2:
                    yield row, run
                run = [col] if ok else []
        if len(run) >= 2:
            yield row, run


def _header_score(grid: Grid, scorer, sheet, row, run) -> float | None:
    """Score of `row` as the header of the columns in `run`, or None if it cannot be one.

    When label-only rows sit between the run and its first values, and the last of them is a
    header in its own right, that lower row is the header (this one at most a group label above it).
    """
    f = header_features(grid, sheet, row, run)
    if f is None:
        return None
    if f["dist_values"] > 1:
        own = header_features(grid, sheet, row + int(f["dist_values"]) - 1, run, lookahead=False)
        if own is not None and scorer.header(own) >= scorer.threshold:
            return None
    return scorer.header(f)


def _row_classes(row: list[RawCell | None]) -> list[str | None]:
    return [None if c is None else ("value" if is_value(c) else "label") for c in row]


def _continues_body(body: list[list[str | None]], nxt: list[RawCell | None]) -> bool:
    """After a blank row, does `nxt` look like more rows of the same body (and not a new header)?"""
    classes = _row_classes(nxt)
    filled = [i for i, k in enumerate(classes) if k is not None]
    if len(filled) < math.ceil(len(classes) / 2) or "value" not in classes:
        return False
    body = [r for r in body if "value" in r]  # label-only rows (units, headings) set no pattern
    if not body:
        return True
    agree = 0
    for i in filled:
        seen = Counter(r[i] for r in body if r[i] is not None)
        agree += bool(seen) and seen.most_common(1)[0][0] == classes[i]
    return agree >= 0.8 * len(filled)


def user_table(sheet: str, spec: dict, at) -> Table:
    """A table the user defined: {"range": "A3:F20", "header_rows": 1, "total": false, "name": optional}.

    The first `header_rows` rows are the header (0: none), the last row is the total row when `total` is set, and rows
    empty across the range inside the body are gaps."""
    c1, r1, c2, r2 = range_boundaries(spec["range"].replace("$", ""))
    n = max(0, int(spec.get("header_rows", 1)))
    header_row = r1 + n - 1 if n else None
    total = r2 if spec.get("total") else None
    start, end = r1 + n, (r2 - 1 if total else r2)
    gaps = [r for r in range(start, end + 1) if all(at(sheet, r, c) is None for c in range(c1, c2 + 1))]
    return Table(0, sheet, spec.get("name") or "", "user", header_row, start, end, c1, c2, total_row=total,
                 header_top=r1 if n > 1 else None, gap_rows=gaps, confidence=1.0)


def detect_tables(raw: RawWorkbook, refs_of: dict[CellKey, list],
                  group_starts: set[CellKey] = frozenset(), scorer=None,
                  user_tables: dict[str, list[dict]] | None = None) -> list[Table]:
    """Declared Excel Tables first, then tables the user defined, then tables inferred from the layout.

    `group_starts` are the first cells of filled-down formula groups: a computed column
    starting right under a row is evidence that the row is a header. `scorer` decides which
    rows are headers (default: tablemodel.default_scorer()). `user_tables` maps a sheet to the tables
    the user defined there (see user_table); such a sheet gets exactly those, and no detection.
    """
    user_tables = user_tables or {}
    scorer = scorer or default_scorer()
    grid = make_grid(raw, refs_of, group_starts)
    at = grid.at
    tables: list[Table] = []
    claimed: set[CellKey] = set()

    def claim(t: Table):
        tables.append(t)
        for r in range(t.top, t.bottom + 1):
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

    for sheet, specs in user_tables.items():
        for spec in specs:
            t = user_table(sheet, spec, at)
            t.table_id = len(tables) + 1
            claim(t)

    for sheet in raw.sheets:
        if sheet in user_tables:
            continue
        for row, run in header_candidates(grid, sheet, claimed):
            if any((sheet, row, c) in claimed for c in run):
                continue
            score = _header_score(grid, scorer, sheet, row, run)
            if score is None or score < scorer.threshold:
                continue
            end, gaps = _extend_body(at, sheet, row, run, claimed)
            if end - row - len(gaps) < 2:  # a single row under labels is a label/value pair, not a table
                continue
            total = None
            if end > row + 1 and _is_total_row(at, sheet, end, row + 1, run[0], run[-1], refs_of):
                total, end = end, end - 1
                if end in gaps:
                    gaps.remove(end)
                    end -= 1
            data_rows = [r for r in range(row + 1, end + 1) if r not in gaps]
            c1 = _extend_stub(at, sheet, row, run[0], data_rows, claimed)
            top = _header_top(grid, scorer, sheet, row, list(range(c1, run[-1] + 1)), claimed)
            claim(Table(len(tables) + 1, sheet, "", "detected", row, row + 1, end, c1, run[-1],
                        total_row=total, header_top=top if top < row else None, gap_rows=gaps,
                        confidence=round(min(score, 1.0), 2)))

    tables = _merge_side_by_side(tables)
    taken: set[str] = set()
    per_sheet = Counter(t.sheet for t in tables)
    for t in tables:
        base = (t.name if t.source == "listobject" else split_label(t.name)[0] if t.source == "user" and t.name
                else split_label(t.sheet)[0])
        if t.source != "listobject" and per_sheet[t.sheet] > 1 and t.header_row:
            first = at(t.sheet, t.header_row, t.c1)
            base = f"{base}_{split_label(str(first.value))[0]}" if first else base
        t.name = unique(base, taken)
        _describe_table(t, at)
    return tables


def _extend_body(at, sheet, row, run, claimed) -> tuple[int, list[int]]:
    """Last body row under the header at `row`. Up to _MAX_GAP_ROWS rows that are empty across
    the table's columns (blank rows, or section headings written only in a column to the left)
    are skipped when the row after them continues the body (same label/value pattern per column)."""
    def cells_at(r):
        return [at(sheet, r, c) for c in run]

    def taken(r):
        return any((sheet, r, c) in claimed for c in run)

    end, gaps = row + 1, []
    body = [_row_classes(cells_at(end))]
    while True:
        nxt = cells_at(end + 1)
        if any(c is not None for c in nxt) and not taken(end + 1):
            end += 1
            body.append(_row_classes(nxt))
            continue
        for k in range(end + 2, end + 2 + _MAX_GAP_ROWS):
            after = cells_at(k)
            if any(c is not None for c in after):
                break
        if not taken(k) and _continues_body(body, after):
            gaps.extend(range(end + 1, k))
            end = k
            body.append(_row_classes(after))
            continue
        return end, gaps


def _extend_stub(at, sheet, row, c1, data_rows, claimed) -> int:
    """First column of the table, widened left over row-label (stub) columns: an empty header cell
    over a column of text labels, as in statistical tables whose header row starts at the values.
    An empty spacer column is crossed only when a stub column lies beyond it."""
    def stub(col):
        if col < 1 or at(sheet, row, col) is not None or any((sheet, r, col) in claimed for r in data_rows):
            return False
        body = [c for c in (at(sheet, r, col) for r in data_rows) if c is not None]
        return len(body) >= 0.3 * len(data_rows) and sum(_is_text(c) for c in body) >= 0.8 * len(body)

    def empty(col):
        return col >= 1 and all(at(sheet, r, col) is None for r in [row, *data_rows])

    while True:
        if stub(c1 - 1):
            c1 -= 1
        elif empty(c1 - 1) and stub(c1 - 2):
            c1 -= 2
        else:
            return c1


def _merge_side_by_side(tables: list[Table]) -> list[Table]:
    """Join detected tables that are one table split by empty spacer columns: neighbours at most one
    column apart, with headers ending within one row of each other and nearly the same body rows."""
    out: list[Table] = []
    for t in sorted(tables, key=lambda t: (t.sheet, t.c1)):
        prev = next((p for p in reversed(out) if p.sheet == t.sheet and p.source == t.source == "detected"
                     and 0 <= t.c1 - p.c2 - 1 <= 1 and abs(t.header_row - p.header_row) <= 1), None)
        if prev is not None:
            overlap = min(prev.bottom, t.bottom) - max(prev.row_start, t.row_start) + 1
            if overlap >= 0.8 * min(prev.bottom - prev.row_start + 1, t.bottom - t.row_start + 1):
                top = min(prev.top, t.top)
                prev.header_row = max(prev.header_row, t.header_row)
                prev.header_top = top if top < prev.header_row else None
                prev.row_start = prev.header_row + 1
                prev.row_end = max(prev.row_end, t.row_end)
                prev.total_row = max(prev.total_row or 0, t.total_row or 0) or None
                prev.gap_rows = sorted(set(prev.gap_rows) & set(t.gap_rows))
                prev.c2 = t.c2
                prev.confidence = max(prev.confidence, t.confidence)
                continue
        out.append(t)
    out.sort(key=lambda t: t.table_id)
    for i, t in enumerate(out, start=1):
        t.table_id = i
    return out


def _header_top(grid: Grid, scorer, sheet, row, cols, claimed) -> int:
    """Topmost row of a multi-row header: rows directly above `row` join the header while the scorer
    takes them for header rows (group labels, units, series metadata) rather than a title or other
    content. A blank row ends the header."""
    top = row
    for _ in range(_MAX_PARENT_ROWS):
        r = top - 1
        if r < 1 or all(grid.at(sheet, r, c) is None for c in cols) or any((sheet, r, c) in claimed for c in cols):
            break
        if not scorer.block(block_features(grid, sheet, r, cols)):
            break
        top = r
    return top


def _header_text(t: Table, at, col: int) -> str | None:
    """Header of one column, with group labels above it joined: '2024 / Q1'."""
    if not t.header_row:
        return None
    parts: list[str] = []
    for r in range(t.top, t.header_row + 1):
        c = at(t.sheet, r, col)
        if c is None and r < t.header_row:
            # an unmerged group label spans rightwards until the next label in its row,
            # but a cell merged down (e.g. the key column's header) does not span sideways
            c = next((at(t.sheet, r, k) for k in range(col - 1, t.c1 - 1, -1)
                      if at(t.sheet, r, k) is not None), None)
            if c is not None and c.row != r:
                c = None
        if c is not None and c.value is not None:
            text = str(c.value)
            if not parts or parts[-1] != text:
                parts.append(text)
    return " / ".join(parts) or None


def _describe_table(t: Table, at) -> None:
    taken: set[str] = set()
    for col in range(t.c1, t.c2 + 1):
        data = [at(t.sheet, r, col) for r in t.data_rows()]
        present = [c for c in data if c is not None]
        if t.key_col is None and present and len(present) == len(data) and all(_is_text(c) for c in present):
            values = [str(c.value).strip() for c in present]
            if len(set(values)) == len(values):
                t.key_col = col

        header = _header_text(t, at, col) or f"col_{col}"
        name, unit = split_label(header.replace(" / ", " "))
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
