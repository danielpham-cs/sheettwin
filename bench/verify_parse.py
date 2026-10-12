"""Check a SheetIR database against its workbook, read independently of SheetIR's parser.

For each workbook the script builds the IR, then re-reads the .xlsx with plain openpyxl (no SheetIR code) and checks:

  content, rebuild, references, groups   as in sheetir/verify.py (which SheetIR also runs after every build)
  issues        planted problems (when given) are reported
  tables        detected table ranges against hand-labeled ranges (when given)

It writes a JSON report (with a snapshot of each sheet for display) and prints a summary.

    uv run python bench/verify_parse.py spec.json -o report.json

spec.json: [{"file": "...xlsx", "label": "...", "expected_issues": [["Quote!I13", "inconsistent_formula"], ...],
             "gold_tables": {"Sheet": ["A1:D20", ...]}, "sheets": ["only these sheets", ...]}, ...]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula

from sheetir.verify import read_xlsx, verify  # the independent checks (they import nothing else from SheetIR)

SNAPSHOT_ROWS, SNAPSHOT_COLS = 45, 16


# ---- checks ----------------------------------------------------------------------------------------

def tamper(db: Path) -> list[str]:
    """Damage the IR on purpose (control run): each change must make some check fail."""
    con = sqlite3.connect(db)
    done = []
    v = con.execute("SELECT cell_id FROM cells WHERE vtype = 'number' ORDER BY cell_id LIMIT 1").fetchone()
    if v:
        con.execute("UPDATE cells SET value = '424242' WHERE cell_id = ?", v)
        done.append(f"changed the value of {v[0]} to 424242")
    d = con.execute("SELECT cell_id FROM cells WHERE vtype = 'text' ORDER BY cell_id DESC LIMIT 1").fetchone()
    if d:
        con.execute("DELETE FROM cells WHERE cell_id = ?", d)
        done.append(f"deleted cell {d[0]}")
    f = con.execute("SELECT cell_id, formula FROM cells WHERE vtype = 'formula' AND group_id IN "
                    "(SELECT group_id FROM formula_groups WHERE n_cells > 2) ORDER BY cell_id DESC LIMIT 1").fetchone()
    if f:
        con.execute("UPDATE cells SET formula = ? WHERE cell_id = ?", (f[1] + "+1", f[0]))
        done.append(f"appended +1 to the formula of {f[0]}")
    e = con.execute("SELECT rowid, src_id, dst_cell FROM edges WHERE src_kind = 'cell' ORDER BY rowid LIMIT 1").fetchone()
    if e:
        con.execute("DELETE FROM edges WHERE rowid = ?", (e[0],))
        done.append(f"removed the dependency {e[1]} → {e[2]}")
    con.commit()
    con.close()
    return done


def check(entry: dict, workdir: Path) -> dict:
    from sheetir import build_ir  # the system under test

    path = Path(entry["file"])
    db = workdir / (path.stem + (".tampered" if entry.get("tamper") else "") + ".db")
    res = build_ir(path, db, verify=False)  # verified below, after any tampering
    tampered = tamper(db) if entry.get("tamper") else None
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    _, wb = read_xlsx(path)
    v = verify(path, db)
    report = {"label": entry.get("label", path.name), "file": path.name, "counts": res.counts, "checks": v["checks"],
              "coverage": v["coverage"], "status": v["status"], "tampered": tampered}
    c = report["checks"]["content"]
    c["constants"] = c["cells"] - c["formulas"]

    # planted issues
    if entry.get("expected_issues"):
        found = {(r["cell_id"], r["kind"]) for r in con.execute("SELECT cell_id, kind FROM issues")}
        exp = [tuple(x) for x in entry["expected_issues"]]
        report["checks"]["issues"] = {"expected": [list(x) for x in exp], "found": [list(x) for x in exp if x in found],
                                      "pass": all(x in found for x in exp)}

    # tables against hand labels
    tables = [dict(r) for r in con.execute("SELECT * FROM tables ORDER BY table_id")]
    for t in tables:
        top = t.get("header_top") or t["header_row"] or t["row_start"]
        t["range"] = f"{get_column_letter(t['col_start'])}{top}:{get_column_letter(t['col_end'])}{t['total_row'] or t['row_end']}"
    if entry.get("gold_tables"):
        rows = []
        for sheet, golds in entry["gold_tables"].items():
            preds = [t["range"] for t in tables if t["sheet"] == sheet]
            for gold in golds:
                best = max(((iou(gold, p), p) for p in preds), default=(0.0, None))
                rows.append({"sheet": sheet, "gold": gold, "pred": best[1], "iou": round(best[0], 3)})
        report["checks"]["tables"] = {"rows": rows, "pass": all(r["iou"] >= 0.8 for r in rows)}

    sheets = json.loads(dict(con.execute("SELECT key, value FROM meta").fetchall())["sheets"])
    report["sheets"] = [snapshot(con, wb, s, tables, entry.get("gold_tables", {}).get(s, []))
                        for s in sheets if not entry.get("sheets") or s in entry["sheets"]]
    report["queries"] = demo_queries(con)
    con.close()
    return report


def iou(a: str, b: str) -> float:
    c1, r1, c2, r2 = range_boundaries(a)
    d1, s1, d2, s2 = range_boundaries(b)
    h, w = min(r2, s2) - max(r1, s1) + 1, min(c2, d2) - max(c1, d1) + 1
    if h <= 0 or w <= 0:
        return 0.0
    inter = h * w
    return inter / ((r2 - r1 + 1) * (c2 - c1 + 1) + (s2 - s1 + 1) * (d2 - d1 + 1) - inter)


def snapshot(con, wb, sheet: str, tables: list[dict], gold: list[str]) -> dict:
    """The top-left of a sheet as openpyxl shows it, with SheetIR's reading of each cell."""
    ws = wb[sheet]
    ir = {(r["row"], r["col"]): r for r in con.execute(
        "SELECT row, col, role, table_id, semantic, cell_id FROM cells WHERE sheet = ?", (sheet,))}
    sev = {r[0]: r[1] for r in con.execute(
        "SELECT cell_id, MIN(CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END) FROM issues GROUP BY cell_id")}
    n_rows = min(ws.max_row or 1, SNAPSHOT_ROWS)
    n_cols = min(ws.max_column or 1, SNAPSHOT_COLS)
    grid = []
    for r in range(1, n_rows + 1):
        row = []
        for c in range(1, n_cols + 1):
            v = ws.cell(r, c).value
            if v is None:
                row.append(None)
                continue
            text = v.text if isinstance(v, ArrayFormula) else str(v)
            info = ir.get((r, c))
            row.append({"t": text[:40], "f": isinstance(v, str) and v.startswith("=") or isinstance(v, ArrayFormula),
                        "role": info["role"] if info else None, "tid": info["table_id"] if info else None,
                        "sem": info["semantic"] if info else None,
                        "sev": ["high", "medium", "low"][sev[info["cell_id"]]] if info and info["cell_id"] in sev else None})
        grid.append(row)
    return {"sheet": sheet, "rows": ws.max_row, "cols": ws.max_column, "shown_rows": n_rows, "shown_cols": n_cols,
            "grid": grid, "gold": gold,
            "tables": [{k: t.get(k) for k in ("table_id", "name", "range", "header_row", "header_top", "row_start",
                                                "row_end", "total_row", "col_start", "col_end", "key_col", "confidence")}
                       for t in tables if t["sheet"] == sheet]}


def demo_queries(con) -> list[dict]:
    """A few questions answered by SQL over the IR, to show the result is queryable."""
    qs = [
        ("Cells by readable name", "SELECT semantic, cell_id, formula FROM cells WHERE semantic IS NOT NULL AND table_id IS NOT NULL ORDER BY sheet, row, col LIMIT 6"),
        ("Tables and their fields", "SELECT t.name AS table_name, f.name AS field, f.kind FROM fields f JOIN tables t USING (table_id) ORDER BY t.table_id, f.col LIMIT 8"),
        ("What the first result depends on", "WITH RECURSIVE tr(cell_id, depth) AS (SELECT (SELECT cell_id FROM cells WHERE role = 'output' ORDER BY sheet, row, col LIMIT 1), 0 "
         "UNION SELECT d.src_cell, tr.depth + 1 FROM deps d JOIN tr ON d.dst_cell = tr.cell_id WHERE tr.depth < 3) "
         "SELECT tr.cell_id, MIN(depth) AS depth, c.semantic FROM tr LEFT JOIN cells c USING (cell_id) GROUP BY tr.cell_id ORDER BY depth, tr.cell_id LIMIT 8"),
    ]
    out = []
    for title, sql in qs:
        cur = con.execute(sql)
        out.append({"title": title, "sql": sql, "columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    reports = []
    with tempfile.TemporaryDirectory() as tmp:
        for entry in spec:
            r = check(entry, Path(tmp))
            reports.append(r)
            marks = " ".join(f"{k}={'PASS' if v['pass'] else 'FAIL'}" for k, v in r["checks"].items())
            print(f"{r['label']:<40} {marks}")
    args.out.write_text(json.dumps(reports, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
