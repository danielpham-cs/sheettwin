"""Local web UI over SheetIR databases (FastAPI). Nothing is fetched from the internet.

Pages:
    /         landing page and, at /app/, the chat UI: the static build of frontend/ in static/app
              (falls back to the legacy page if not built)
    /legacy   structure browser, static/index.html

Layout of the data directory (default ./data, override with SHEETIR_DATA):
    data/<id>/<original file name>.xlsx   copy of the upload (the user's file is never touched)
    data/<id>/ir.db                       SheetIR database
where <id> is the first 12 hex chars of the workbook's SHA-256.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..build import build_ir
from ..sample import make_quotation
from ..store import IR_SCHEMA, TRACE_SQL
from . import graph
from .charts import charts
from .chat import answer, find_cells

STATIC = Path(__file__).parent / "static"
CHAT_UI = STATIC / "app"  # written by `npm run build` in frontend/
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_SQL_ROWS = 500
MAX_LAYOUT_CELLS = 30_000  # per sheet, for the overview minimaps

app = FastAPI(title="SheetTwin · SheetIR")


def data_dir() -> Path:
    d = Path(os.environ.get("SHEETIR_DATA", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path(wid: str) -> Path:
    if not _ID_RE.match(wid):
        raise HTTPException(400, "bad workbook id")
    p = data_dir() / wid / "ir.db"
    if not p.exists():
        raise HTTPException(404, "workbook not found")
    return p


def _connect(wid: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{_db_path(wid).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql, *args) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _loads(v):
    return None if v is None else json.loads(v)


def _ingest(data: bytes, filename: str) -> dict:
    name = Path(filename or "workbook.xlsx").name
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "only .xlsx / .xlsm files are supported")
    wid = hashlib.sha256(data).hexdigest()[:12]
    folder = data_dir() / wid
    folder.mkdir(exist_ok=True)
    src = folder / name
    src.write_bytes(data)
    try:
        res = build_ir(src, folder / "ir.db")
    except Exception as exc:
        raise HTTPException(422, f"could not parse workbook: {exc}") from exc
    return {"id": wid, "counts": res.counts}


@app.get("/legacy", response_class=HTMLResponse)
def legacy():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/workbooks")
def list_workbooks():
    out = []
    for db in sorted(data_dir().glob("*/ir.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        n_issues = con.execute("SELECT COUNT(*) FROM issues WHERE severity='high'").fetchone()[0]
        con.close()
        out.append({"id": db.parent.name, "name": Path(meta["source"]).name,
                    "built_at": meta.get("built_at"), "high_issues": n_issues})
    return out


@app.post("/api/workbooks")
def upload(file: UploadFile):
    return _ingest(file.file.read(), file.filename)


@app.post("/api/workbooks/sample")
def sample():
    with tempfile.TemporaryDirectory() as tmp:
        p = make_quotation(Path(tmp) / "quotation_sample.xlsx")
        return _ingest(p.read_bytes(), p.name)


@app.get("/api/workbooks/{wid}/summary")
def summary(wid: str):
    con = _connect(wid)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    counts = {
        name: con.execute(sql).fetchone()[0] for name, sql in {
            "cells": "SELECT COUNT(*) FROM cells",
            "formulas": "SELECT COUNT(*) FROM cells WHERE vtype='formula'",
            "groups": "SELECT COUNT(*) FROM formula_groups",
            "tables": "SELECT COUNT(*) FROM tables",
            "params": "SELECT COUNT(*) FROM params",
            "relations": "SELECT COUNT(*) FROM relations",
            "outputs": "SELECT COUNT(*) FROM cells WHERE role='output'",
            "inputs": "SELECT COUNT(*) FROM cells WHERE role='input'",
            "edges": "SELECT COUNT(*) FROM edges",
            "names": "SELECT COUNT(*) FROM names",
            "issues": "SELECT COUNT(*) FROM issues",
            "issues_high": "SELECT COUNT(*) FROM issues WHERE severity='high'",
        }.items()
    }
    sheets = _loads(meta.get("sheets")) or [r["sheet"] for r in _rows(con, "SELECT DISTINCT sheet FROM cells")]
    per_sheet = {r["sheet"]: r for r in _rows(
        con, "SELECT sheet, COUNT(*) AS cells, SUM(vtype='formula') AS formulas, MAX(row) AS max_row, "
             "MAX(col) AS max_col FROM cells GROUP BY sheet")}
    tables_per = dict(con.execute("SELECT sheet, COUNT(*) FROM tables GROUP BY sheet").fetchall())
    issues_per = {r["sheet"]: r for r in _rows(
        con, "SELECT c.sheet, COUNT(*) AS issues, SUM(i.severity='high') AS high FROM issues i "
             "JOIN cells c USING(cell_id) GROUP BY c.sheet")}
    sheet_stats = [{"sheet": n, "cells": per_sheet.get(n, {}).get("cells", 0),
                    "formulas": per_sheet.get(n, {}).get("formulas", 0) or 0,
                    "max_row": per_sheet.get(n, {}).get("max_row", 0), "max_col": per_sheet.get(n, {}).get("max_col", 0),
                    "tables": tables_per.get(n, 0), "issues": issues_per.get(n, {}).get("issues", 0),
                    "issues_high": issues_per.get(n, {}).get("high", 0) or 0} for n in sheets]
    return {
        "id": wid,
        "name": Path(meta["source"]).name,
        "sha256": meta["sha256"],
        "built_at": meta.get("built_at"),
        "sheetir_version": meta.get("sheetir_version"),
        "table_scorer": meta.get("table_scorer", "hand"),
        "stale": meta.get("ir_schema") != str(IR_SCHEMA),
        "sheets": sheets,
        "sheet_stats": sheet_stats,
        "counts": counts,
        "checks": _rows(con, "SELECT name, passed, severity, detail FROM checks"),
        "issues_by_kind": _rows(con, "SELECT kind, severity, COUNT(*) AS n FROM issues GROUP BY kind, severity "
                                     "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, n DESC"),
    }


@app.get("/api/workbooks/{wid}/sheet")
def sheet(wid: str, name: str):
    con = _connect(wid)
    cells = _rows(con, "SELECT cell_id, row, col, a1, vtype, role, value, cached, formula, group_id, semantic, "
                       "number_format, table_id FROM cells WHERE sheet = ?", name)
    for c in cells:
        c["value"], c["cached"] = _loads(c["value"]), _loads(c["cached"])
    issues = _rows(con, "SELECT i.cell_id, i.kind, i.severity FROM issues i JOIN cells c USING(cell_id) "
                        "WHERE c.sheet = ?", name)
    tables = _rows(con, "SELECT * FROM tables WHERE sheet = ? ORDER BY table_id", name)
    for t in tables:
        t["fields"] = _rows(con, "SELECT col, header, name, unit, kind FROM fields WHERE table_id = ? ORDER BY col",
                            t["table_id"])
    params = _rows(con, "SELECT p.cell_id, p.name, p.label, p.unit FROM params p JOIN cells c USING(cell_id) "
                        "WHERE c.sheet = ?", name)
    return {"cells": cells, "issues": issues, "tables": tables, "params": params}


@app.get("/api/workbooks/{wid}/layout")
def layout(wid: str):
    """Compact per-sheet maps for the overview: [row, col, role, table_id, issue severity] per cell."""
    con = _connect(wid)
    sev = {r["cell_id"]: r["sev"] for r in _rows(
        con, "SELECT cell_id, MIN(CASE severity WHEN 'high' THEN 'high' WHEN 'medium' THEN 'medium' ELSE 'low' END) "
             "AS sev FROM issues WHERE cell_id IS NOT NULL GROUP BY cell_id")}
    out = {}
    for (name,) in con.execute("SELECT DISTINCT sheet FROM cells").fetchall():
        rows = con.execute("SELECT cell_id, row, col, role, table_id FROM cells WHERE sheet = ? LIMIT ?",
                           (name, MAX_LAYOUT_CELLS + 1)).fetchall()
        out[name] = {
            "cells": [[r[1], r[2], r[3], r[4], sev.get(r[0])] for r in rows[:MAX_LAYOUT_CELLS]],
            "truncated": len(rows) > MAX_LAYOUT_CELLS,
            "tables": _rows(con, "SELECT table_id, name, row_start, row_end, col_start, col_end, header_row, total_row "
                                 "FROM tables WHERE sheet = ?", name),
        }
    return out


@app.get("/api/workbooks/{wid}/db")
def download_db(wid: str):
    con = _connect(wid)
    name = Path(dict(con.execute("SELECT key, value FROM meta").fetchall())["source"]).stem
    con.close()
    return FileResponse(_db_path(wid), media_type="application/vnd.sqlite3", filename=f"{name}.sheetir.db")


def _source_file(wid: str) -> Path:
    _db_path(wid)
    files = [p for p in (data_dir() / wid).iterdir() if p.suffix.lower() in (".xlsx", ".xlsm")]
    if not files:
        raise HTTPException(404, "the original workbook is no longer stored")
    return files[0]


@app.post("/api/workbooks/{wid}/rebuild")
def rebuild(wid: str):
    """Parse the stored copy again with the current SheetIR (after an upgrade)."""
    src = _source_file(wid)
    try:
        res = build_ir(src, data_dir() / wid / "ir.db")
    except Exception as exc:
        raise HTTPException(422, f"could not parse workbook: {exc}") from exc
    return {"id": wid, "counts": res.counts}


@app.delete("/api/workbooks/{wid}")
def delete(wid: str):
    """Remove the stored copy and its database (the user's original file is never touched)."""
    folder = _db_path(wid).parent
    for p in folder.iterdir():
        p.unlink()
    folder.rmdir()
    return {"deleted": wid}


@app.get("/api/workbooks/{wid}/cell")
def cell(wid: str, id: str, depth: int = 6):
    con = _connect(wid)
    rows = _rows(con, "SELECT * FROM cells WHERE cell_id = ?", id)
    c = rows[0] if rows else {"cell_id": id, "role": "empty"}
    if rows:
        c["value"], c["cached"] = _loads(c["value"]), _loads(c["cached"])
    group = _rows(con, "SELECT group_id, axis, anchor, last, n_cells, r1c1 FROM formula_groups WHERE group_id = ?",
                  c.get("group_id"))
    trace = _rows(con, TRACE_SQL, id, max(1, min(depth, 30)))
    for t in trace:
        t["value"] = _loads(t["value"])
    return {
        "cell": c,
        "group": group[0] if group else None,
        "issues": _rows(con, "SELECT kind, severity, detail FROM issues WHERE cell_id = ?", id),
        "precedents": trace[1:],
        "dependents": _rows(con, "SELECT DISTINCT d.dst_cell AS cell_id, c.semantic FROM deps d "
                                 "LEFT JOIN cells c ON c.cell_id = d.dst_cell WHERE d.src_cell = ? "
                                 "ORDER BY c.sheet, c.col, c.row", id),
    }


@app.get("/api/workbooks/{wid}/issues")
def issues(wid: str):
    con = _connect(wid)
    return _rows(con, "SELECT i.cell_id, i.kind, i.severity, i.detail, c.semantic, c.sheet FROM issues i "
                      "LEFT JOIN cells c USING(cell_id) ORDER BY CASE i.severity WHEN 'high' THEN 0 "
                      "WHEN 'medium' THEN 1 ELSE 2 END, i.kind, c.sheet, c.col, c.row")


@app.get("/api/workbooks/{wid}/model")
def model(wid: str):
    """Tables with fields, parameters, outputs and relations: the semantic view of the workbook."""
    con = _connect(wid)
    tables = _rows(con, "SELECT * FROM tables ORDER BY table_id")
    for t in tables:
        t["fields"] = _rows(con, "SELECT col, header, name, unit, kind, r1c1 FROM fields WHERE table_id = ? "
                                 "ORDER BY col", t["table_id"])
        t["n_rows"] = t["row_end"] - t["row_start"] + 1
    params = _rows(con, "SELECT p.cell_id, p.label, p.name, p.unit, p.domain, c.value FROM params p "
                        "JOIN cells c USING(cell_id) ORDER BY p.param_id")
    for p in params:
        p["value"], p["domain"] = _loads(p["value"]), _loads(p["domain"])
    outputs = _rows(con, "SELECT cell_id, semantic, formula FROM cells WHERE role='output' AND table_id IS NULL "
                         "ORDER BY sheet, row, col")
    groups = _rows(con, "SELECT group_id, sheet, axis, anchor, last, n_cells, r1c1 FROM formula_groups "
                        "ORDER BY n_cells DESC, group_id")
    # One row per lookup/aggregate pattern; MIN() makes SQLite take cell_id from the first relation.
    relations = _rows(con, "SELECT MIN(r.relation_id) AS first, r.cell_id, r.function, r.kind, r.match, r.value, "
                           "ft.name AS from_table, r.from_field, tt.name AS to_table, r.key_field, r.return_field, "
                           "r.key_range, r.return_range, COUNT(*) AS n_cells FROM relations r "
                           "LEFT JOIN tables ft ON ft.table_id = r.from_table "
                           "LEFT JOIN tables tt ON tt.table_id = r.to_table "
                           "GROUP BY r.group_id, r.function, r.kind, r.match, r.key_range, r.return_range "
                           "ORDER BY n_cells DESC, first")
    return {"tables": tables, "params": params, "outputs": outputs, "groups": groups, "relations": relations}


class SqlRequest(BaseModel):
    query: str


@app.post("/api/workbooks/{wid}/sql")
def run_sql(wid: str, req: SqlRequest):
    q = req.query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise HTTPException(400, "only SELECT / WITH queries are allowed")
    con = _connect(wid)  # opened read-only, so writes fail even if they slip through
    try:
        cur = con.execute(q)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_SQL_ROWS + 1)
    except sqlite3.Error as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"columns": cols, "rows": [list(r) for r in rows[:MAX_SQL_ROWS]],
            "truncated": len(rows) > MAX_SQL_ROWS}


class ChatRequest(BaseModel):
    message: str
    lang: str = "zh"  # reply language: zh | en


@app.post("/api/workbooks/{wid}/chat")
def chat(wid: str, req: ChatRequest):
    return answer(_connect(wid), req.message, req.lang)


@app.get("/api/workbooks/{wid}/graph")
def dependency_graph(wid: str, cell: str | None = None, up: int = 3, down: int = 2):
    """Block-level overview, or the neighbourhood of one cell when `cell` is given."""
    con = _connect(wid)
    if not cell:
        return graph.overview(con)
    found = find_cells(con, cell)
    if not found:
        raise HTTPException(400, f"expected a sheet-qualified cell like Quote!J13, got {cell!r}")
    return graph.focus(con, found[0], max(0, min(up, 10)), max(0, min(down, 10)))


@app.get("/api/workbooks/{wid}/charts")
def chart_data(wid: str):
    """Structure/audit statistics and table rows for the charts view."""
    return charts(_connect(wid))


# Files derived from the workbook. CSV/JSON exports are generated on request from ir.db.
EXPORTS = {
    "ir.db": ("SheetIR 資料庫", "SQLite", "application/vnd.sqlite3"),
    "model.json": ("語意模型", "JSON", "application/json"),
    "cells.csv": ("儲存格清單", "CSV", "text/csv"),
    "issues.csv": ("問題清單", "CSV", "text/csv"),
    "formula_groups.csv": ("公式群組", "CSV", "text/csv"),
}
CSV_SQL = {
    "cells.csv": "SELECT cell_id, sheet, a1, vtype, role, semantic, value, cached, formula, r1c1, group_id "
                 "FROM cells ORDER BY sheet, row, col",
    "issues.csv": "SELECT cell_id, group_id, kind, severity, detail FROM issues ORDER BY issue_id",
    "formula_groups.csv": "SELECT group_id, sheet, axis, anchor, last, n_cells, r1c1 FROM formula_groups "
                          "ORDER BY group_id",
}


def _export_bytes(wid: str, name: str) -> bytes:
    if name == "model.json":
        return json.dumps(model(wid), ensure_ascii=False, indent=2, default=str).encode("utf-8")
    con = _connect(wid)
    cur = con.execute(CSV_SQL[name])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([d[0] for d in cur.description])
    w.writerows(cur.fetchall())
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly


@app.get("/api/workbooks/{wid}/exports")
def list_exports(wid: str):
    db = _db_path(wid)
    out = [{"name": src.name, "label": "原始活頁簿", "format": "XLSX", "size": src.stat().st_size, "source": True}
           for src in db.parent.iterdir() if src.suffix.lower() in (".xlsx", ".xlsm")]
    for name, (label, fmt, _) in EXPORTS.items():
        size = db.stat().st_size if name == "ir.db" else len(_export_bytes(wid, name))
        out.append({"name": name, "label": label, "format": fmt, "size": size, "source": False})
    return out


@app.get("/api/workbooks/{wid}/exports/{name}")
def download_export(wid: str, name: str):
    db = _db_path(wid)
    stem = Path(dict(_connect(wid).execute("SELECT key, value FROM meta").fetchall())["source"]).stem
    if name == "ir.db":
        return FileResponse(db, media_type=EXPORTS[name][2], filename=f"{stem}.sheetir.db")
    if name in EXPORTS:
        return Response(_export_bytes(wid, name), media_type=EXPORTS[name][2],
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'{stem}.{name}')}"})
    src = db.parent / Path(name).name
    if src.suffix.lower() in (".xlsx", ".xlsm") and src.is_file():
        return FileResponse(src, filename=src.name)
    raise HTTPException(404, "no such export")


# Registered last so the API routes above take precedence over the static mount.
if (CHAT_UI / "index.html").exists():
    app.mount("/", StaticFiles(directory=CHAT_UI, html=True), name="chat-ui")
else:
    app.add_api_route("/", legacy, response_class=HTMLResponse, include_in_schema=False)
