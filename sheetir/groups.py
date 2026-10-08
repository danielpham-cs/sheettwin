"""L2: group formula cells that perform the same computation (identical R1C1 form).

Contiguous cells with the same R1C1 key down a column (or across a row) become one
group, i.e. one vectorizable computation. Groups are what later stages name, explain
and translate, instead of thousands of individual cells.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .loader import CellKey


@dataclass
class FormulaGroup:
    group_id: int
    sheet: str
    axis: str  # col (filled down) | row (filled right) | cell (singleton)
    key: str
    cells: list[CellKey]

    @property
    def anchor(self) -> CellKey:
        return self.cells[0]


def _runs(items: list[tuple[int, str, CellKey]]):
    """Split (position, key, cell) items sorted by position into contiguous same-key runs."""
    run: list[CellKey] = []
    prev_pos, prev_key = None, None
    for pos, key, cell in items:
        if run and (pos != prev_pos + 1 or key != prev_key):
            yield prev_key, run
            run = []
        run.append(cell)
        prev_pos, prev_key = pos, key
    if run:
        yield prev_key, run


def group_formulas(keys: dict[CellKey, str]) -> list[FormulaGroup]:
    groups: list[FormulaGroup] = []
    assigned: set[CellKey] = set()

    def add(sheet, axis, key, cells):
        groups.append(FormulaGroup(len(groups) + 1, sheet, axis, key, cells))
        assigned.update(cells)

    by_col: dict[tuple[str, int], list] = defaultdict(list)
    for (sheet, row, col), key in keys.items():
        by_col[(sheet, col)].append((row, key, (sheet, row, col)))
    for (sheet, _), items in sorted(by_col.items()):
        for key, run in _runs(sorted(items)):
            if len(run) >= 2:
                add(sheet, "col", key, run)

    by_row: dict[tuple[str, int], list] = defaultdict(list)
    for cell, key in keys.items():
        if cell not in assigned:
            sheet, row, col = cell
            by_row[(sheet, row)].append((col, key, cell))
    for (sheet, _), items in sorted(by_row.items()):
        for key, run in _runs(sorted(items)):
            add(sheet, "row" if len(run) >= 2 else "cell", key, run)

    return groups
