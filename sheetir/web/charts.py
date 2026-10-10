"""Data for the charts view: structure/audit statistics and the rows of every detected table.

Table values are constants as typed, or for formula cells the value Excel last cached.
Files never saved by Excel (including the bundled sample) have no cached values, so their
derived columns come back empty and are reported as `missing`.
"""

from __future__ import annotations

import json
import sqlite3

MAX_ROWS = 500


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def charts(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    rows = lambda sql: [dict(r) for r in con.execute(sql)]  # noqa: E731
    out = {
        "roles": rows("SELECT role, COUNT(*) AS n FROM cells GROUP BY role ORDER BY n DESC"),
        "severity": rows("SELECT severity, COUNT(*) AS n FROM issues GROUP BY severity"),
        "kinds": rows("SELECT kind, severity, COUNT(*) AS n FROM issues GROUP BY kind, severity ORDER BY n DESC, kind"),
        "sheets": rows("SELECT sheet, COUNT(*) AS cells, SUM(vtype = 'formula') AS formulas FROM cells "
                       "GROUP BY sheet ORDER BY MIN(rowid)"),
        "tables": [],
    }
    for t in rows("SELECT * FROM tables ORDER BY table_id"):
        fields = rows(f"SELECT col, name, header, unit, kind, number_format FROM fields "
                      f"WHERE table_id = {int(t['table_id'])} ORDER BY col")
        cells = {(r["row"], r["col"]): r for r in con.execute(
            "SELECT row, col, vtype, value, cached FROM cells WHERE table_id = ? AND row BETWEEN ? AND ?",
            (t["table_id"], t["row_start"], t["row_end"]))}
        data = []
        for row in range(t["row_start"], min(t["row_end"], t["row_start"] + MAX_ROWS - 1) + 1):
            if row == t["total_row"]:
                continue
            values = {}
            for f in fields:
                c = cells.get((row, f["col"]))
                if c is not None:
                    raw = c["cached"] if c["vtype"] == "formula" else c["value"]
                    values[f["name"]] = None if raw is None else json.loads(raw)
            key_cell = cells.get((row, t["key_col"])) if t["key_col"] else None
            key = json.loads(key_cell["value"]) if key_cell and key_cell["value"] else f"row {row}"
            data.append({"key": str(key), "values": values})
        for f in fields:
            vals = [f_v for d in data if (f_v := d["values"].get(f["name"])) is not None]
            f["numeric"] = any(_num(v) is not None for v in vals) or (f["kind"] == "derived" and not vals)
            f["missing"] = sum(1 for d in data if d["values"].get(f["name"]) is None)
            f["distinct"] = len({str(v) for v in vals})
            f["percent"] = "%" in (f["number_format"] or "")
        out["tables"].append({"table_id": t["table_id"], "name": t["name"], "sheet": t["sheet"],
                              "kind": t["kind"], "fields": fields, "rows": data})
    return out
