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
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, fixes
from ..build import build_ir
from ..convert import SUFFIXES
from ..llm import get_llm
from ..loader import WorkbookError
from ..report import audit_report, to_html, to_markdown
from ..sample import make_quotation
from ..store import IR_SCHEMA, TRACE_SQL
from ..understand import ask
from ..verify import NOT_UNDERSTOOD
from . import graph, provenance
from .charts import charts
from .chat import answer, find_cells

STATIC = Path(__file__).parent / "static"
CHAT_UI = STATIC / "app"  # written by `npm run build` in frontend/
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_SQL_ROWS = 500
MAX_LAYOUT_CELLS = 30_000  # per sheet, for the overview minimaps

app = FastAPI(title="SheetTwin · SheetIR")
log = logging.getLogger("sheetir.web")


def data_dir() -> Path:
    d = Path(os.environ.get("SHEETIR_DATA", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _folder(wid: str) -> Path:
    """The stored upload, parsed or not."""
    if not _ID_RE.match(wid):
        raise HTTPException(400, "bad workbook id")
    p = data_dir() / wid
    if not p.is_dir():
        raise HTTPException(404, "workbook not found")
    return p


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


def _db_counts(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
        return {"sheets": len(json.loads(dict(con.execute("SELECT key, value FROM meta").fetchall()).get("sheets") or "[]")),
                "cells": q("SELECT COUNT(*) FROM cells"), "formula_cells": q("SELECT COUNT(*) FROM cells WHERE vtype='formula'"),
                "formula_groups": q("SELECT COUNT(*) FROM formula_groups"), "tables": q("SELECT COUNT(*) FROM tables"),
                "params": q("SELECT COUNT(*) FROM params"), "edges": q("SELECT COUNT(*) FROM edges"),
                "relations": q("SELECT COUNT(*) FROM relations"), "issues": q("SELECT COUNT(*) FROM issues")}
    finally:
        con.close()


def _ingest(data: bytes, filename: str, file_modified: str | None = None) -> dict:
    """Store and parse an uploaded workbook. The same bytes always land in the same result (data/<sha256[:12]>);
    uploading them again reuses it, re-parsing only when it was built with an older IR schema."""
    name = Path(filename or "workbook.xlsx").name
    if not name.lower().endswith(SUFFIXES):
        raise HTTPException(400, {"kind": "unsupported_type", "ext": Path(name).suffix.lower(),
                                  "message": "only .xlsx / .xlsm / .xls / .ods / .csv / .tsv files are supported"})
    sha = hashlib.sha256(data).hexdigest()
    wid = sha[:12]
    folder = data_dir() / wid
    folder.mkdir(exist_ok=True)
    rec, seen = provenance.record_upload(folder, name, sha, len(data), file_modified)
    src = folder / rec["name"]
    if not src.exists():
        src.write_bytes(data)
    db = folder / "ir.db"
    if not seen:
        provenance.inherit_ignored(folder)
    reparse = not seen or provenance.db_meta(db).get("ir_schema") != str(IR_SCHEMA)
    base = {"id": wid, "name": rec["name"], "duplicate": seen, "first_uploaded": rec["first_uploaded"], "reparsed": reparse}
    if reparse:
        failed = _parse(folder, src)
        if failed:
            return {**base, "failed": True, "error": failed, "counts": None, "status": "failed"}
    return {**base, "failed": False, "counts": _db_counts(db), "status": provenance.db_meta(db).get("parse_status", "unverified")}


def _parse(folder: Path, src: Path) -> dict | None:
    """Build (and verify) the IR for a stored workbook. On failure, record why in error.json and return it."""
    try:
        build_ir(src, folder / "ir.db", user_tables=provenance.user_tables(folder), ignored=provenance.ignored(folder))
    except WorkbookError as exc:
        return provenance.record_failure(folder, exc.kind, exc.detail, __version__)
    except Exception as exc:  # noqa: BLE001 - a SheetIR bug: keep the upload, say so, retry after an upgrade
        return provenance.record_failure(folder, "parse_failed", f"{type(exc).__name__}: {exc}", __version__)
    provenance.clear_failure(folder)
    return None


@app.get("/legacy", response_class=HTMLResponse)
def legacy():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/workbooks")
def list_workbooks():
    """Every result with its source (file name, SHA-256, size, upload times) and its version among results
    of the same file name, most recently uploaded first."""
    return provenance.catalog(data_dir())


@app.post("/api/workbooks")
def upload(file: UploadFile, last_modified: int | None = Form(None)):
    """`last_modified`: the file's modification time on the uploader's machine, in ms since the epoch."""
    modified = (dt.datetime.fromtimestamp(last_modified / 1000).isoformat(timespec="seconds")
                if last_modified else None)
    return _ingest(file.file.read(), file.filename, modified)


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
        "source": provenance.read(_db_path(wid).parent),
        "built_at": meta.get("built_at"),
        "sheetir_version": meta.get("sheetir_version"),
        "table_scorer": meta.get("table_scorer", "hand"),
        "stale": meta.get("ir_schema") != str(IR_SCHEMA),
        "status": meta.get("parse_status", "unverified"),
        "verification": _loads(meta.get("verification")),
        "load_warnings": _loads(meta.get("load_warnings")) or [],
        "user_tables": _loads(meta.get("user_tables")) or {},
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
    return FileResponse(_db_path(wid), media_type="application/vnd.sqlite3", filename=f"{name}.{wid}.sheetir.db")


@app.get("/api/workbooks/{wid}/diff")
def diff(wid: str, against: str):
    """Cells added, removed or changed from result `against` (e.g. the previous version) to this one."""
    return provenance.diff(_db_path(wid), _db_path(against))


@app.post("/api/workbooks/{wid}/rebuild")
def rebuild(wid: str):
    """Parse the stored copy again with the current SheetIR (after an upgrade)."""
    folder = _folder(wid)
    src = provenance.source_file(folder)
    if src is None:
        raise HTTPException(404, "the original workbook is no longer stored")
    failed = _parse(folder, src)
    if failed:
        return {"id": wid, "failed": True, "error": failed, "counts": None, "status": "failed"}
    db = folder / "ir.db"
    return {"id": wid, "failed": False, "counts": _db_counts(db), "status": provenance.db_meta(db).get("parse_status")}


@app.get("/api/workbooks/{wid}/fixes")
def fix_suggestions(wid: str):
    """For each formula SheetIR cannot follow fully: what it does today and, where possible, a replacement."""
    folder = _folder(wid)
    return fixes.suggest(_db_path(wid), provenance.source_file(folder))


class ApplyFixes(BaseModel):
    cells: list[str]


@app.post("/api/workbooks/{wid}/fixes/apply")
def apply_fixes(wid: str, req: ApplyFixes):
    """Write the suggested formulas of `cells` into a copy of the workbook and ingest it as its next version.
    The formulas are recomputed here, never taken from the request."""
    folder = _folder(wid)
    src = provenance.source_file(folder)
    if src is None:
        raise HTTPException(404, "the original workbook is no longer stored")
    s = fixes.suggest(_db_path(wid), src)
    if not s["can_apply"]:
        raise HTTPException(400, "only .xlsx / .xlsm files can be edited")
    usable = {f["cell"]: f["fix"] for f in s["fixes"] if f["fix"] and not f.get("blocked")}
    edits = {c: usable[c] for c in req.cells if c in usable}
    if not edits:
        raise HTTPException(400, "none of these cells has a fix that can be applied")
    data = fixes.apply(src, edits)
    return {**_ingest(data, provenance.read(folder)["name"]), "applied": sorted(edits)}


@app.get("/api/workbooks/{wid}/resolve")
def resolve_issues(wid: str):
    """Every issue with a recommended fix (cells to change and their new contents), and the ignored issues."""
    return fixes.recommend(_db_path(wid), provenance.source_file(_folder(wid)))


MAX_EDITS = 2000


class Edits(BaseModel):
    edits: dict[str, str]  # cell id -> contents as typed in Excel ("=..." formula, "" to clear)


@app.post("/api/workbooks/{wid}/edits")
def edit_cells(wid: str, req: Edits):
    """Write the user's contents into a copy of the workbook and ingest it as its next version."""
    folder = _folder(wid)
    src = provenance.source_file(folder)
    if src is None:
        raise HTTPException(404, "the original workbook is no longer stored")
    if src.suffix.lower() not in fixes.EDITABLE:
        raise HTTPException(400, "only .xlsx / .xlsm files can be edited")
    if not req.edits or len(req.edits) > MAX_EDITS:
        raise HTTPException(400, f"send between 1 and {MAX_EDITS} edits")
    try:
        data = fixes.apply(src, req.edits)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**_ingest(data, provenance.read(folder)["name"]), "edited": sorted(req.edits)}


class IssueKey(BaseModel):
    cell: str
    kind: str
    detail: str
    note: str | None = None


@app.post("/api/workbooks/{wid}/ignored")
def ignore_issue(wid: str, req: IssueKey):
    """Mark an issue as intended: it is no longer reported, in this version and the ones after it."""
    folder = _folder(wid)
    con = _connect(wid)
    try:
        found = con.execute("SELECT 1 FROM issues WHERE cell_id = ? AND kind = ? AND detail = ?",
                            (req.cell, req.kind, req.detail)).fetchone()
    finally:
        con.close()
    if not found:
        raise HTTPException(404, "no such issue")
    if req.kind in NOT_UNDERSTOOD:
        raise HTTPException(400, "formulas SheetIR cannot follow cannot be ignored: fix them, or keep the partial status")
    items = [i for i in provenance.ignored(folder) if (i["cell"], i["kind"], i["detail"]) != (req.cell, req.kind, req.detail)]
    provenance.save_ignored(folder, [*items, {"cell": req.cell, "kind": req.kind, "detail": req.detail,
                                              "note": (req.note or "").strip() or None, "at": provenance.now()}])
    return rebuild(wid)


@app.post("/api/workbooks/{wid}/ignored/remove")
def unignore_issue(wid: str, req: IssueKey):
    """Report an ignored issue again."""
    folder = _folder(wid)
    items = provenance.ignored(folder)
    kept = [i for i in items if (i["cell"], i["kind"], i["detail"]) != (req.cell, req.kind, req.detail)]
    if len(kept) == len(items):
        raise HTTPException(404, "this issue is not ignored")
    provenance.save_ignored(folder, kept)
    return rebuild(wid)


class TableSpec(BaseModel):
    range: str
    header_rows: int = 1
    total: bool = False
    name: str | None = None


class SheetTables(BaseModel):
    tables: list[TableSpec]


def _check_tables(specs: list[TableSpec]) -> list[dict]:
    """Validate the user's tables on one sheet: well-formed ranges, a body row left, no overlaps."""
    from openpyxl.utils import range_boundaries
    boxes = []
    for i, t in enumerate(specs):
        try:
            c1, r1, c2, r2 = range_boundaries(t.range.replace("$", "").upper())
        except Exception:
            raise HTTPException(400, {"kind": "bad_range", "index": i, "message": f"not a cell range: {t.range}"})
        if None in (c1, r1, c2, r2):
            raise HTTPException(400, {"kind": "bad_range", "index": i, "message": f"not a cell range: {t.range}"})
        if t.header_rows < 0 or (r2 - r1 + 1) - t.header_rows - (1 if t.total else 0) < 1:
            raise HTTPException(400, {"kind": "too_small", "index": i, "message": f"{t.range} has no body row left"})
        for j, (d1, s1, d2, s2) in enumerate(boxes):
            if c1 <= d2 and d1 <= c2 and r1 <= s2 and s1 <= r2:
                raise HTTPException(400, {"kind": "overlap", "index": i, "other": j, "message": f"{t.range} overlaps {specs[j].range}"})
        boxes.append((c1, r1, c2, r2))
    return [{"range": t.range.replace("$", "").upper(), "header_rows": t.header_rows, "total": t.total,
             **({"name": t.name} if t.name else {})} for t in specs]


@app.get("/api/workbooks/{wid}/user-tables")
def get_user_tables(wid: str):
    """The tables the user defined, per sheet (sheets not listed use automatic detection)."""
    return {"sheets": provenance.user_tables(_folder(wid))}


def _reparse_with(wid: str, sheets: dict) -> dict:
    folder = _folder(wid)
    before = provenance.user_tables(folder)
    provenance.save_user_tables(folder, sheets)
    src = provenance.source_file(folder)
    failed = _parse(folder, src) if src else {"kind": "parse_failed", "detail": "the original workbook is no longer stored"}
    if failed:
        provenance.save_user_tables(folder, before)  # keep the last corrections that parsed
        if src:
            _parse(folder, src)
        raise HTTPException(422, {"kind": failed["kind"], "message": failed["detail"]})
    db = folder / "ir.db"
    return {"id": wid, "sheets": sheets, "status": provenance.db_meta(db).get("parse_status"), "counts": _db_counts(db)}


@app.put("/api/workbooks/{wid}/user-tables/{sheet}")
def set_user_tables(wid: str, sheet: str, body: SheetTables):
    """Replace table detection on one sheet with the given tables (an empty list: no tables there), then re-parse."""
    sheets = json.loads(provenance.db_meta(_db_path(wid)).get("sheets") or "[]")
    if sheet not in sheets:
        raise HTTPException(404, {"kind": "unknown_sheet", "message": f"no sheet named {sheet}"})
    current = provenance.user_tables(_folder(wid))
    return _reparse_with(wid, {**current, sheet: _check_tables(body.tables)})


@app.delete("/api/workbooks/{wid}/user-tables/{sheet}")
def reset_user_tables(wid: str, sheet: str):
    """Go back to automatic table detection on one sheet, then re-parse."""
    current = provenance.user_tables(_folder(wid))
    current.pop(sheet, None)
    return _reparse_with(wid, current)


@app.delete("/api/workbooks/{wid}")
def delete(wid: str):
    """Remove the stored copy and its database (the user's original file is never touched)."""
    folder = _folder(wid)
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


def _select_only(query: str) -> str:
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise HTTPException(400, "only SELECT / WITH queries are allowed")
    return q


@app.post("/api/workbooks/{wid}/sql/check")
def check_sql(wid: str, req: SqlRequest):
    """Compile the query without running it (EXPLAIN), for the editor's as-you-type error marks."""
    try:
        q = _select_only(req.query)
    except HTTPException as exc:
        return {"error": exc.detail}
    try:
        _connect(wid).execute("EXPLAIN " + q).fetchone()
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    return {"error": None}


@app.post("/api/workbooks/{wid}/sql")
def run_sql(wid: str, req: SqlRequest):
    q = _select_only(req.query)
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


@app.get("/api/llm")
def llm_status():
    """Whether chat answers come from a language model (OPENAI_API_KEY set) or from the built-in rules."""
    llm = get_llm()
    return {"enabled": llm is not None, "model": llm.model if llm else None}


def _only_cells(con: sqlite3.Connection, message: str) -> bool:
    """The message is nothing but cell ids: the rule-based cell card (formula, group, issues, links) answers it."""
    cells = find_cells(con, message)
    rest = re.sub(r"(?:'[^']+'|[A-Za-z_][\w.]*)![$A-Za-z]+\$?\d+", "", message)
    return bool(cells) and not re.sub(r"[\s,，、;；?？.。]", "", rest)


@app.post("/api/workbooks/{wid}/chat")
def chat(wid: str, req: ChatRequest):
    """Answer with the language model when one is configured (sheetir.understand.ask, grounded in the IR), else with
    the built-in rules. Bare cell ids always get the rule-based cell card; if the model fails, the rules answer and
    `fallback` says why."""
    con = _connect(wid)
    llm = get_llm()
    if llm is None or _only_cells(con, req.message):
        return {**answer(con, req.message, req.lang), "source": "rules"}
    try:
        r = ask(con, req.message, llm, req.lang)
    except Exception as e:  # any failure of the model path: the user still gets an answer, and the reason
        log.exception("LLM chat failed; answering with the rules")
        return {**answer(_connect(wid), req.message, req.lang), "source": "rules",
                "fallback": f"{type(e).__name__}: {e}"[:300]}
    # cell ids as code, which the chat UI makes clickable (the rule-based replies write them that way too)
    known = set(r["cells"])
    reply = re.sub(r"`[^`]*`|((?:'[^']+'|[A-Za-z_][\w.]*)![A-Z]{1,3}\d+)",
                   lambda m: f"`{m[1]}`" if m[1] in known else m[0], r["reply"])
    return {"reply": reply, "cells": r["cells"], "source": "llm", "model": r["model"], "route": r["route"],
            "sql": r["sql"], "grounded": r["grounded"], "ungrounded": r["ungrounded"]}


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
    "audit_report.html": ("稽核報告", "HTML", "text/html; charset=utf-8"),
    "audit_report.md": ("稽核報告", "Markdown", "text/markdown; charset=utf-8"),
}
REPORTS = {"audit_report.html": to_html, "audit_report.md": to_markdown}
CSV_SQL = {
    "cells.csv": "SELECT cell_id, sheet, a1, vtype, role, semantic, value, cached, formula, r1c1, group_id "
                 "FROM cells ORDER BY sheet, row, col",
    "issues.csv": "SELECT cell_id, group_id, kind, severity, detail FROM issues ORDER BY issue_id",
    "formula_groups.csv": "SELECT group_id, sheet, axis, anchor, last, n_cells, r1c1 FROM formula_groups "
                          "ORDER BY group_id",
}


def _export_bytes(wid: str, name: str, lang: str = "zh") -> bytes:
    if name in REPORTS:
        return REPORTS[name](audit_report(_connect(wid), lang)).encode("utf-8")
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
def list_exports(wid: str, lang: str = "zh"):
    db = _db_path(wid)
    out = [{"name": src.name, "label": "原始活頁簿", "format": src.suffix[1:].upper(), "size": src.stat().st_size,
            "source": True} for src in db.parent.iterdir() if src.suffix.lower() in SUFFIXES]
    for name, (label, fmt, _) in EXPORTS.items():
        size = db.stat().st_size if name == "ir.db" else len(_export_bytes(wid, name, lang))
        out.append({"name": name, "label": label, "format": fmt, "size": size, "source": False})
    return out


@app.get("/api/workbooks/{wid}/exports/{name}")
def download_export(wid: str, name: str, lang: str = "zh", inline: bool = False):
    """One converted file. Reports are written in `lang` (zh | en); with `inline`, a report opens in the browser
    instead of downloading (the HTML report can then be printed or saved as PDF)."""
    db = _db_path(wid)
    stem = Path(dict(_connect(wid).execute("SELECT key, value FROM meta").fetchall())["source"]).stem
    if name == "ir.db":
        return FileResponse(db, media_type=EXPORTS[name][2], filename=f"{stem}.{wid}.sheetir.db")
    if name in EXPORTS:
        filename = f"{stem}.{wid}.{name}"
        if name in REPORTS and lang == "en":
            filename = filename.replace("audit_report", "audit_report.en")
        disposition = "inline" if inline and name in REPORTS else "attachment"
        return Response(_export_bytes(wid, name, lang), media_type=EXPORTS[name][2],
                        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"})
    src = db.parent / Path(name).name
    if src.suffix.lower() in SUFFIXES and src.is_file():
        return FileResponse(src, filename=src.name)
    raise HTTPException(404, "no such export")


# Registered last so the API routes above take precedence over the static mount.
if (CHAT_UI / "index.html").exists():
    app.mount("/", StaticFiles(directory=CHAT_UI, html=True), name="chat-ui")
else:
    app.add_api_route("/", legacy, response_class=HTMLResponse, include_in_schema=False)
