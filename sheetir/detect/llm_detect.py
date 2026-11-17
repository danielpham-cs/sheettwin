"""Language-model fallback for table detection (design §6.3.4, §6.3.5, prompt §15.1).

Used for tables the rules found with low confidence, and for blocks of cells no table covers. The model gets the
region encoded as text (encode_region: one line per row, A1 column letters, merged ranges listed, long regions cut to
their first and last rows with a summary of each column), the rules' guess as a hint, and returns TableSpecs. Each is
validated (validate_spec); invalid output goes back with the errors, up to detect.llm_retries times. If the model
still fails, the caller keeps the rules' spec and marks it low_confidence.
"""

from __future__ import annotations

import json
from collections import Counter

from openpyxl.utils import get_column_letter
from pydantic import BaseModel

from ..clean.normalize import SheetGrid
from .models import TableSpec, a1, validate_spec

HEAD_ROWS, TAIL_ROWS = 40, 8


def _show(v) -> str:
    s = str(v).replace("\n", " ").replace("|", "/")
    return s if len(s) <= 40 else s[:37] + "…"


def encode_region(grid: SheetGrid, c1: int, r1: int, c2: int, r2: int, max_chars: int = 24000) -> str:
    """The region as text for the model: 'row N: A=… | B=… ', merges, and for a long region the first HEAD_ROWS and
    last TAIL_ROWS rows with the types of the rows left out."""
    def line(r):
        cells = []
        for c in range(c1, c2 + 1):
            x = grid.own(r, c)
            if x is not None and x.value not in (None, ""):
                mark = "ƒ" if x.formula else ""
                cells.append(f"{get_column_letter(c)}={mark}{_show(x.value)}")
            elif x is not None and x.formula:
                cells.append(f"{get_column_letter(c)}=ƒ(no value)")
        return f"row {r}: " + " | ".join(cells) if cells else f"row {r}: (empty)"

    rows = list(range(r1, r2 + 1))
    if len(rows) > HEAD_ROWS + TAIL_ROWS:
        cut = rows[HEAD_ROWS:-TAIL_ROWS]
        kinds = Counter()
        for r in cut:
            for c in range(c1, c2 + 1):
                x = grid.own(r, c)
                if x is not None:
                    kinds[f"{get_column_letter(c)}:{x.vtype}"] += 1
        summary = ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items()))
        lines = [line(r) for r in rows[:HEAD_ROWS]] + [
            f"… rows {cut[0]}-{cut[-1]} not shown ({len(cut)} rows; cells by column and type: {summary})"] + \
            [line(r) for r in rows[-TAIL_ROWS:]]
    else:
        lines = [line(r) for r in rows]
    merges = [a1(m[0], m[1], m[2], m[3]) for m in grid.merges if c1 <= m[0] and m[2] <= c2 and r1 <= m[1] and m[3] <= r2]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (cut)"
    return text + (f"\nMerged ranges: {', '.join(merges)}" if merges else "")


class _Found(BaseModel):
    range: str
    header_rows: list[int]
    total_rows: list[int]
    note_rows: list[int]
    title: str | None


class _Reply(BaseModel):
    tables: list[_Found]


SYSTEM = """You analyze one region of a spreadsheet and identify the data tables inside it.
Return JSON only, matching the schema. Use absolute Excel row numbers and A1 ranges.

Definitions:
- header_rows: rows containing column labels. Multi-level headers have several rows.
- total_rows: subtotal or grand-total rows that aggregate rows above them.
- note_rows: free-text notes inside the table range.
- title: a caption above the table, if any (not part of the range).

Rules:
- A range must include its header rows, and the header rows come first in it.
- Tables must not overlap.
- If there is no table, return an empty list."""


def llm_detect(grid: SheetGrid, region: tuple[int, int, int, int], hint: TableSpec | None, others: list[TableSpec],
               llm, retries: int = 2, max_chars: int = 24000) -> list[TableSpec] | None:
    """Tables in `region` (c1, r1, c2, r2) as the model reads them, validated; None when the model fails or keeps
    returning invalid specs. `others` are tables elsewhere on the sheet, which the result must not overlap."""
    c1, r1, c2, r2 = region
    hints = {"range": hint.range, "header_rows": hint.header_rows, "total_rows": hint.total_rows,
             "confidence": hint.confidence} if hint else "none"
    user = (f'Sheet: "{grid.sheet}"\nRegion: {a1(c1, r1, c2, r2)}\n'
            f"Hints from rules (may be wrong): {json.dumps(hints)}\n\nEncoded cells:\n"
            f"{encode_region(grid, c1, r1, c2, r2, max_chars)}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    used = grid.used_range()
    for _ in range(retries + 1):
        try:
            reply = llm.chat_json(messages, _Reply, name="detect_tables", retries=1)
        except Exception:  # noqa: BLE001
            return None
        specs, errors = [], []
        for i, f in enumerate(reply.tables, start=1):
            spec = TableSpec(table_id=f"llm_{i}", sheet=grid.sheet, range=f.range.replace("$", "").upper(),
                             header_rows=sorted(f.header_rows), total_rows=sorted(f.total_rows),
                             note_rows=sorted(f.note_rows), title=f.title, confidence=0.75, source="llm")
            errs = validate_spec(spec, used, [*others, *specs])
            if not errs:
                try:
                    sc1, sr1, sc2, sr2 = spec.bounds
                    if not (c1 <= sc1 and r1 <= sr1 and sc2 <= c2 and sr2 <= r2):
                        errs.append(f"range {spec.range} is outside the region {a1(c1, r1, c2, r2)}")
                except (ValueError, TypeError):
                    errs.append(f"range {spec.range!r} is not an A1 range")
            if errs:
                errors += [f"table {i} ({f.range}): {e}" for e in errs]
            else:
                specs.append(spec)
        if not errors:
            return specs
        messages = messages + [
            {"role": "assistant", "content": reply.model_dump_json()},
            {"role": "user", "content": "These tables are invalid:\n- " + "\n- ".join(errors) +
                                        "\nReturn the corrected list of tables for the region."}]
    return None


def uncovered_blocks(grid: SheetGrid, specs: list[TableSpec], min_rows: int = 3, min_cols: int = 2):
    """Blocks of filled cells (4-connected, across merges) that no spec covers and that are at least min_rows ×
    min_cols: candidates the rules rejected, for the model to look at."""
    covered = set()
    for s in specs:
        sc1, sr1, sc2, sr2 = s.bounds
        covered |= {(r, c) for r in range(sr1, sr2 + 1) for c in range(sc1, sc2 + 1)}
    filled = {(r, c) for (r, c), x in grid.cells.items() if x.value not in (None, "") or x.formula} - covered
    filled |= {p for p in grid.cover if grid.at(*p) is not None} - covered
    seen, blocks = set(), []
    for start in sorted(filled):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            r, c = stack.pop()
            comp.append((r, c))
            for nb in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if nb in filled and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        rows = [r for r, _ in comp]
        cols = [c for _, c in comp]
        box = (min(cols), min(rows), max(cols), max(rows))
        if box[3] - box[1] + 1 >= min_rows and box[2] - box[0] + 1 >= min_cols:
            blocks.append(box)
    return blocks
