"""A compact text sketch of a parsed workbook, for a language model to read.

Built from the SheetIR database alone. It lists, per sheet, the tables (range, header, key column, fields with units and
formula patterns, a few sample rows), the parameters, the results outside tables, other labeled content, and then the
lookups between tables and the issues found. Large workbooks are cut down to stay within a few thousand tokens; the
cuts are stated in the text. Cell ids are written exactly as in the database so an answer can cite them.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

MAX_SHEETS = 12
MAX_FIELDS = 30
SAMPLE_ROWS = 3
MAX_PARAMS = 40
MAX_OUTPUTS = 30
MAX_LOOSE = 40
MAX_ISSUES = 15
MAX_VALUE = 40


def _v(raw) -> str:
    """A stored value (JSON text) as short display text."""
    if raw is None:
        return ""
    try:
        v = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        v = raw
    if isinstance(v, float):
        v = round(v, 4)
    s = str(v)
    return s if len(s) <= MAX_VALUE else s[:MAX_VALUE - 1] + "…"


def _shown(c) -> str:
    """A cell for the sketch: its value, or for a formula without a stored result, the formula itself."""
    if c["formula"]:
        return _v(c["cached"]) if c["cached"] is not None else "ƒ" + (c["formula"] if len(c["formula"]) <= MAX_VALUE else c["formula"][:MAX_VALUE - 1] + "…")
    return _v(c["value"])


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sketch(con: sqlite3.Connection) -> str:
    con.row_factory = sqlite3.Row
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    sheets = json.loads(meta.get("sheets") or "[]")
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    out = [f"Workbook: {Path(meta.get('source', '')).name}", f"Sheets ({len(sheets)}): {', '.join(sheets)}"]
    if len(sheets) > MAX_SHEETS:
        out.append(f"(only the first {MAX_SHEETS} sheets are described)")

    for sheet in sheets[:MAX_SHEETS]:
        dims = q("SELECT COUNT(*) AS n, MAX(row) AS r, MAX(col) AS c, SUM(vtype='formula') AS f FROM cells WHERE sheet = ?", sheet)[0]
        out.append("")
        out.append(f"## Sheet {sheet!r}: {dims['n'] or 0} cells, {dims['f'] or 0} formulas, up to row {dims['r'] or 0}, column {_col(dims['c'] or 0)}")
        for t in q("SELECT * FROM tables WHERE sheet = ? ORDER BY table_id", sheet):
            top = (t["header_top"] if "header_top" in t.keys() else None) or t["header_row"] or t["row_start"]
            bottom = t["total_row"] or t["row_end"]
            how = {"listobject": "declared Excel Table", "user": "defined by the user"}.get(t["source"], "detected")
            out.append(f"Table `{t['name']}` {_col(t['col_start'])}{top}:{_col(t['col_end'])}{bottom} ({how}); "
                       f"body rows {t['row_start']}-{t['row_end']} ({t['row_end'] - t['row_start'] + 1} rows)"
                       + (f"; key column {_col(t['key_col'])}" if t["key_col"] else "")
                       + (f"; total row {t['total_row']}" if t["total_row"] else ""))
            fields = q("SELECT col, header, name, unit, kind, r1c1 FROM fields WHERE table_id = ? ORDER BY col", t["table_id"])
            for f in fields[:MAX_FIELDS]:
                out.append(f"  - {_col(f['col'])} `{f['name']}`" + (f" [{f['unit']}]" if f["unit"] else "")
                           + f" header {f['header']!r}" + (f": computed {f['r1c1']}" if f["kind"] == "derived" and f["r1c1"] else ""))
            if len(fields) > MAX_FIELDS:
                out.append(f"  - … {len(fields) - MAX_FIELDS} more columns")
            rows = range(t["row_start"], min(t["row_end"], t["row_start"] + SAMPLE_ROWS - 1) + 1)
            out.append(f"  sample rows (the first {len(rows)} of {t['row_end'] - t['row_start'] + 1}; the rest are in the database):")
            for r in rows:
                vals = q("SELECT col, value, cached, formula FROM cells WHERE sheet = ? AND row = ? AND col BETWEEN ? AND ? ORDER BY col",
                         sheet, r, t["col_start"], min(t["col_end"], t["col_start"] + MAX_FIELDS - 1))
                out.append(f"  row {r}: " + " | ".join(f"{_col(v['col'])}={_shown(v)}" for v in vals))
        params = q("SELECT p.name, p.label, p.unit, p.cell_id, c.value FROM params p JOIN cells c USING (cell_id) WHERE c.sheet = ?", sheet)
        for p in params[:MAX_PARAMS]:
            out.append(f"Parameter `params.{p['name']}` = {_v(p['value'])}" + (f" {p['unit']}" if p["unit"] else "")
                       + f" at {p['cell_id']} (label {p['label']!r})")
        outputs = q("SELECT cell_id, semantic, formula, cached FROM cells WHERE sheet = ? AND role = 'output' AND table_id IS NULL ORDER BY row, col", sheet)
        for o in outputs[:MAX_OUTPUTS]:
            out.append(f"Result {o['cell_id']} `{o['semantic'] or ''}` = {o['formula']}" + (f" (last value {_v(o['cached'])})" if o["cached"] else ""))
        loose = q("SELECT cell_id, vtype, value, formula FROM cells WHERE sheet = ? AND table_id IS NULL AND role IN ('label', 'orphan') "
                  "ORDER BY row, col LIMIT ?", sheet, MAX_LOOSE + 1)
        if loose:
            out.append("Other content: " + "; ".join(f"{c['cell_id']}={_v(c['value'])}" for c in loose[:MAX_LOOSE])
                       + (" …" if len(loose) > MAX_LOOSE else ""))

    links = q("SELECT * FROM table_links")
    if links:
        out.append("")
        out.append("## Lookups between tables")
        for l in links:
            out.append(f"- {l['from_table'] or '?'}.{l['from_field'] or '?'} -> {l['to_table']}.{l['key_field']} returns "
                       f"{l['return_field'] or '?'} ({l['function']}, {l['match']} match, {l['n_cells']} cells, e.g. {l['example']})")
    issues = q("SELECT cell_id, kind, severity, detail FROM issues ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END LIMIT ?", MAX_ISSUES)
    if issues:
        out.append("")
        out.append("## Issues found by SheetIR's rules")
        out.extend(f"- {i['severity']} {i['kind']} at {i['cell_id']}: {i['detail']}" for i in issues)
    return "\n".join(out)
