"""Local web UI over SheetIR databases (FastAPI). Nothing is fetched from the internet.

Layout of the data directory (default ./data, override with SHEETIR_DATA):
    data/<id>/<original file name>.xlsx   copy of the upload (the user's file is never touched)
    data/<id>/ir.db                       SheetIR database
where <id> is the first 12 hex chars of the workbook's SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..build import build_ir
from ..sample import make_quotation
from ..store import TRACE_SQL

STATIC = Path(__file__).parent / "static"
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_SQL_ROWS = 500

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


@app.get("/", response_class=HTMLResponse)
def index():
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
            "outputs": "SELECT COUNT(*) FROM cells WHERE role='output'",
        }.items()
    }
    return {
        "id": wid,
        "name": Path(meta["source"]).name,
        "sha256": meta["sha256"],
        "built_at": meta.get("built_at"),
        "sheets": _loads(meta.get("sheets")) or [r["sheet"] for r in _rows(con, "SELECT DISTINCT sheet FROM cells")],
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
    return {"cells": cells, "issues": issues}


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
    """Tables with fields, parameters and outputs: the semantic view of the workbook."""
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
    return {"tables": tables, "params": params, "outputs": outputs, "groups": groups}


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
