"""Local web UI over SheetIR databases (FastAPI). Nothing is fetched from the internet.

Pages:
    /         landing page and, at /app/, the chat UI: the static build of frontend/ in static/app
              (falls back to the legacy page if not built)
    /legacy   structure browser, static/index.html

Layout of the data directory (default ./data, override with SHEETIR_DATA):
    data/<id>/<original file name>.xlsx   copy of the upload (the user's file is never touched)
    data/<id>/ir.db                       SheetIR database
where <id> is the first 12 hex chars of the workbook's SHA-256. With SHEETIR_USER_HEADER set (see users.py), each
signed-in user gets the same layout under data/users/<user>/.
"""

from __future__ import annotations

import contextvars
import csv
import datetime as dt
import gc
import hashlib
import io
import json
import logging
import os
import queue
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, fixes
from ..agent.loop import answer as agent_answer
from ..agent.tools import jsonable
from ..agent.whatif_tools import WhatIfTools
from ..build import build_ir
from ..catalog.store import CatalogHandle, build_catalog
from ..config import is_local_url
from ..history import History
from ..convert import SUFFIXES
from ..llm import get_llm, llm_status as _llm_settings_status
from ..loader import WorkbookError
from ..report import audit_report, to_html, to_markdown
from ..sample import make_quotation
from ..store import IR_SCHEMA, TRACE_SQL
from ..twin import check as check_twin, export_module, stored as stored_twin
from ..understand import ask
from ..verify import NOT_UNDERSTOOD
from ..catalog.project import ProjectCatalog
from . import graph, knowledge, projects, provenance, users
from .basepath import BasePathMiddleware
from .charts import charts
from .history_api import record_edit, router as history_router, who as history_who
from .chat import answer, find_cells

STATIC = Path(__file__).parent / "static"
CHAT_UI = STATIC / "app"  # written by `npm run build` in frontend/
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_SQL_ROWS = 500
MAX_LAYOUT_CELLS = 30_000  # per sheet, for the overview minimaps

app = FastAPI(title="SheetTwin · SheetIR")
app.add_middleware(BasePathMiddleware)  # UI behind a proxy prefix (see basepath.py)
app.add_middleware(users.UserScopeMiddleware)  # per-user data behind a sign-in gateway (see users.py)
log = logging.getLogger("sheetir.web")


def data_dir() -> Path:
    """The data directory of this request: the signed-in user's own (users.py), or the shared one."""
    d = users.scoped(Path(os.environ.get("SHEETIR_DATA", "data")))
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
    h = _catalog(wid)
    return {**base, "failed": False, "counts": _db_counts(db), "status": provenance.db_meta(db).get("parse_status", "unverified"),
            "catalog": h.stats if h else None}


def _parse(folder: Path, src: Path) -> dict | None:
    """Build (and verify) the IR for a stored workbook, then its catalog. On failure of the IR, record why in
    error.json and return it; a catalog that fails to build only costs the agent (see _build_catalog)."""
    try:
        build_ir(src, folder / "ir.db", user_tables=provenance.user_tables(folder), ignored=provenance.ignored(folder))
    except WorkbookError as exc:
        return provenance.record_failure(folder, exc.kind, exc.detail, __version__)
    except Exception as exc:  # noqa: BLE001 - a SheetIR bug: keep the upload, say so, retry after an upgrade
        return provenance.record_failure(folder, "parse_failed", f"{type(exc).__name__}: {exc}", __version__)
    provenance.clear_failure(folder)
    _build_catalog(folder)
    return None


CATALOG_ERROR = "catalog_error.txt"


def _build_catalog(folder: Path) -> CatalogHandle | None:
    """Clean tables, families, relations, cards and index for the agent (sheetir.catalog). The language model, when
    configured, writes the cards and reads unsure tables; without it the rules do. A failure is logged and kept in
    catalog_error.txt; chat then answers with the IR-only path."""
    err = folder / CATALOG_ERROR
    try:
        h = build_catalog(folder / "ir.db", folder, llm=get_llm())
    except Exception as exc:  # noqa: BLE001
        log.exception("catalog build failed for %s", folder.name)
        err.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        return None
    if err.exists():
        err.unlink()
    return h


def _catalog(wid: str, build: bool = True) -> CatalogHandle | None:
    """The workbook's catalog; built now when missing or made by an older pipeline (and `build`)."""
    folder = _folder(wid)
    h = CatalogHandle.open(folder)
    if h is None and build and (folder / "ir.db").exists() and not (folder / CATALOG_ERROR).exists():
        h = _build_catalog(folder)
    return h


def _catalog_brief(h: CatalogHandle | None, folder: Path) -> dict:
    err = folder / CATALOG_ERROR
    if h is None:
        return {"built": False, "error": err.read_text(encoding="utf-8") if err.exists() else None}
    return {"built": True, "stats": h.stats, "errors": h.data.get("errors", [])[:10], "model": h.data.get("model"),
            "cards": [{"card_id": c.card_id, "sql_name": c.sql_name, "title": c.title, "row_count": c.row_count,
                       "time_coverage": c.time_coverage, "low_confidence": c.low_confidence,
                       "sheets": list(dict.fromkeys(s["sheet"] for s in c.sources)), "warnings": len(c.warnings)}
                      for c in h.cards]}


@app.get("/legacy", response_class=HTMLResponse)
def legacy():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/workbooks")
def list_workbooks():
    """Every result with its source (file name, SHA-256, size, upload times) and its version among results
    of the same file name, most recently uploaded first."""
    return provenance.catalog(data_dir())


def _workbook_ids() -> set[str]:
    """Every stored upload, parsed or not."""
    return {p.name for p in data_dir().iterdir()
            if _ID_RE.match(p.name) and ((p / "ir.db").exists() or (p / provenance.MANIFEST).exists())}


def _in_project(res: dict, project: str | None) -> dict:
    """Put an ingested upload into `project` (none: it shows up in "default" unless another project holds it)."""
    if project:
        with _project_errors():
            projects.add(data_dir(), project, res["id"])
    return res


class _project_errors:
    """Turn projects.ProjectError into the HTTP error it describes."""
    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if isinstance(exc, projects.ProjectError):
            raise HTTPException(exc.status, str(exc)) from exc
        return False


@app.post("/api/workbooks")
def upload(file: UploadFile, last_modified: int | None = Form(None), project: str | None = Form(None)):
    """`last_modified`: the file's modification time on the uploader's machine, in ms since the epoch. `project`: the
    project the file is added to."""
    if project:
        with _project_errors():
            projects.workbooks_of(data_dir(), project, set())  # 404 before parsing
    modified = (dt.datetime.fromtimestamp(last_modified / 1000).isoformat(timespec="seconds")
                if last_modified else None)
    return _in_project(_ingest(file.file.read(), file.filename, modified), project)


@app.post("/api/workbooks/sample")
def sample(project: str | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        p = make_quotation(Path(tmp) / "quotation_sample.xlsx")
        return _in_project(_ingest(p.read_bytes(), p.name), project)


# ---- projects ---------------------------------------------------------------------------------------------------------

class ProjectName(BaseModel):
    name: str


class ProjectMember(BaseModel):
    workbook: str
    move_from: str | None = None  # the project the workbook leaves, for a move


@app.get("/api/projects")
def list_projects():
    """Every project with the ids of its workbooks; "default" first, holding every workbook no other project holds."""
    return projects.listing(data_dir(), _workbook_ids())


@app.post("/api/projects")
def create_project(req: ProjectName):
    with _project_errors():
        return projects.create(data_dir(), req.name)


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, req: ProjectName):
    with _project_errors():
        return projects.rename(data_dir(), pid, req.name)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    """Remove a project; its files stay stored, in "default" when no other project holds them."""
    with _project_errors():
        projects.delete(data_dir(), pid)
    return {"deleted": pid}


@app.post("/api/projects/{pid}/workbooks")
def add_to_project(pid: str, req: ProjectMember):
    """Add a stored workbook (all its versions) to a project, or move it there from `move_from`."""
    _folder(req.workbook)
    wids = _versions_of(req.workbook)
    with _project_errors():
        for w in wids:
            projects.add(data_dir(), pid, w)
            if req.move_from and req.move_from != pid:
                projects.remove(data_dir(), req.move_from, w)
    return {"project": pid, "workbooks": wids}


@app.delete("/api/projects/{pid}/workbooks/{wid}")
def remove_from_project(pid: str, wid: str):
    """Take a workbook (all its versions) out of a project; the files stay stored."""
    _folder(wid)
    with _project_errors():
        for w in _versions_of(wid):
            projects.remove(data_dir(), pid, w)
    return {"project": pid, "removed": wid}


def _versions_of(wid: str) -> list[str]:
    """`wid` and the other stored versions of the same file (same name), which move between projects together."""
    entries = provenance.catalog(data_dir())
    name = next((e["name"].lower() for e in entries if e["id"] == wid), None)
    return [e["id"] for e in entries if e["name"].lower() == name] or [wid]


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
        "catalog": _catalog_brief(_catalog(wid, build=False), _folder(wid)),
    }


@app.get("/api/workbooks/{wid}/catalog")
def catalog(wid: str):
    """The catalog the agent works from: build statistics, and each card in full (columns, units, sample rows,
    relations, source sheets, warnings). Built on first request when missing."""
    h = _catalog(wid)
    if h is None:
        return _catalog_brief(None, _folder(wid))
    return {**_catalog_brief(h, _folder(wid)), "cards": [c.model_dump(mode="json") for c in h.cards],
            "families": [f.model_dump() for f in h.families], "relations": [r.model_dump() for r in h.relations]}


@app.post("/api/workbooks/{wid}/catalog/rebuild")
def rebuild_catalog(wid: str):
    folder = _folder(wid)
    if (folder / CATALOG_ERROR).exists():
        (folder / CATALOG_ERROR).unlink()
    h = _build_catalog(folder)
    return _catalog_brief(h, folder)


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
    by: str | None = None  # who, when users are not separated (recorded in the project history)


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
    res = _ingest(data, provenance.read(folder)["name"])
    projects.follow(data_dir(), wid, res["id"])
    record_edit(wid, res["id"], res["duplicate"], edits, "fix", req.by)
    return {**res, "applied": sorted(edits)}


@app.get("/api/workbooks/{wid}/resolve")
def resolve_issues(wid: str):
    """Every issue with a recommended fix (cells to change and their new contents), and the ignored issues."""
    return fixes.recommend(_db_path(wid), provenance.source_file(_folder(wid)))


MAX_EDITS = 2000


class Edits(BaseModel):
    edits: dict[str, str]  # cell id -> contents as typed in Excel ("=..." formula, "" to clear)
    by: str | None = None  # who, when users are not separated (recorded in the project history)


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
    res = _ingest(data, provenance.read(folder)["name"])
    projects.follow(data_dir(), wid, res["id"])
    record_edit(wid, res["id"], res["duplicate"], req.edits, "edit", req.by)
    return {**res, "edited": sorted(req.edits)}


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
    # Endpoints that read ir.db leave their sqlite3 connection to be freed by the garbage collector: a connection and
    # its statement cache reference each other, so it is not closed when the request ends. On Windows an open
    # connection keeps the file locked and unlink fails, so collect them first.
    gc.collect()
    for p in folder.iterdir():
        p.unlink()
    folder.rmdir()
    projects.forget(data_dir(), wid)
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
    lang: str = "zh"  # UI language (zh | en | vi), used when the question's own language cannot be told


@app.get("/api/me")
def me():
    """Who the data belongs to: the signed-in user when users are separated (users.py), else null (shared data)."""
    return {"user": users.current_user(), "separated": bool(users.header_name())}


@app.get("/api/llm")
def llm_status():
    """Whether chat answers come from a language model or from the built-in rules, and if not, why."""
    llm = get_llm()
    if llm is None:
        reason = _llm_settings_status()["reason"] or "no language model is configured"
        return {"enabled": False, "model": None, "endpoint": None, "reason": reason, "local": True}
    return {"enabled": True, "model": llm.model, "endpoint": llm.base_url, "reason": None,
            "local": is_local_url(llm.base_url)}


def _only_cells(con: sqlite3.Connection, message: str) -> bool:
    """The message is nothing but cell ids: the rule-based cell card (formula, group, issues, links) answers it."""
    cells = find_cells(con, message)
    rest = re.sub(r"(?:'[^']+'|[A-Za-z_][\w.]*)![$A-Za-z]+\$?\d+", "", message)
    return bool(cells) and not re.sub(r"[\s,，、;；?？.。]", "", rest)


ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["route"],
                "properties": {"route": {"type": "string", "enum": ["data", "what_if", "structure", "issues"]}}}
ROUTE_SYSTEM = """Classify a question about an Excel workbook.
- data: about the values in its tables: lookups, counts, totals, averages, comparisons, rankings, trends, periods,
  or a request to chart or visualize them.
- structure: what the workbook is or does, its sheets, columns, formulas, how a cell is calculated, parameters.
- what_if: what would happen to results if some values changed, how sensitive a result is to its inputs, or what
  value an input needs for a result to reach a target ("what if the rate were 3%", "what price gives a 20% margin").
  Also a request to change or correct the stored data ("change B3 to 120", "the rate should be 3%, fix it").
- issues: errors, risks, problems or warnings found in the workbook.
Return JSON {"route": ...}."""


def _route(question: str, llm, h: CatalogHandle) -> str:
    titles = "; ".join(c.title for c in h.cards[:30])
    try:
        return llm.json(ROUTE_SYSTEM, f"Tables: {titles}\n\nQuestion: {question}", ROUTE_SCHEMA, "route")["route"]
    except Exception:  # noqa: BLE001 - the agent is the safer default for a question about data
        return "data"


def _whatif_tools(pid: str | None, wids: list[str], question: str) -> WhatIfTools | None:
    """The agent's what-if tools over the parsed workbooks `wids` (the one in view first), recording in the history
    of project `pid` (by default the first project that lists the first workbook, else "default")."""
    books = []
    for wid in dict.fromkeys(wids):
        db = data_dir() / wid / "ir.db"
        if _ID_RE.match(wid) and db.exists():
            books.append((wid, provenance.read(db.parent)["name"], db))
    if not books:
        return None
    if pid is None:
        pid = next(iter(projects.holders(data_dir(), books[0][0])), projects.DEFAULT)
    return WhatIfTools(books, History(projects.folder(data_dir(), pid)), history_who(None), question)


def _agent_reply(question: str, catalog, llm, lang: str, on_step, context: str = "", whatif=None) -> dict:
    r = agent_answer(question, catalog, llm, lang=lang, on_step=on_step, context=context, whatif=whatif)
    last_sql = next((x["sql"] for x in reversed(r.results) if x.get("sql")), None)
    return jsonable({"records": whatif.records if whatif is not None else [],
        "reply": r.answer, "cells": [], "source": "agent", "model": r.model, "route": "data", "sql": last_sql,
        "grounded": r.grounded, "ungrounded": r.ungrounded, "steps": [s.model_dump() for s in r.steps],
        "results": [{**x, "rows": x["rows"][:50]} for x in r.results], "chart": r.chart,
        "provenance": r.provenance, "stopped": r.stopped, "seconds": r.seconds})


def _chat(wid: str, req: ChatRequest, on_step=None, route: str | None = None, context: str = "",
          pid: str | None = None) -> dict:
    """Bare cell ids get the rule-based cell card; with no model, the rules answer. With a model, questions about the
    data go to the agent over the catalog (sheetir.agent), questions about structure and issues to the IR-grounded
    Q&A (sheetir.understand.ask). If the model path fails, the rules answer and `fallback` says why. `route`: the
    question's route when the caller already knows it."""
    con = _connect(wid)
    llm = get_llm()
    if llm is None or _only_cells(con, req.message):
        return {**answer(con, req.message, req.lang), "source": "rules"}
    try:
        h = _catalog(wid)
        if route is None:
            route = _route(req.message, llm, h) if h is not None and h.cards else "ir"
        if route in ("data", "what_if") and h is not None and h.cards:
            return _agent_reply(req.message, h, llm, req.lang, on_step, context,
                                _whatif_tools(pid, [wid], req.message))
        r = ask(con, req.message, llm, req.lang, project_context=context)
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


@app.post("/api/workbooks/{wid}/chat")
def chat(wid: str, req: ChatRequest):
    """One answer (see _chat). /chat/stream gives the same answer with the agent's steps as they happen."""
    return _chat(wid, req)


@app.post("/api/workbooks/{wid}/chat/stream")
def chat_stream(wid: str, req: ChatRequest):
    """Server-sent events: `step` for each agent step ({n, thought, tool, args, result, seconds}) as it finishes,
    then `final` with the body /chat returns (or `error`)."""
    _connect(wid).close()  # 404 before the stream starts
    return _sse(lambda on_step: _chat(wid, req, on_step=on_step))


class ProjectChatRequest(ChatRequest):
    workbook: str | None = None  # the workbook the user is looking at: cell ids and structure questions are about it


def _project_members(pid: str) -> list[tuple[str, str, CatalogHandle]]:
    """The parsed workbooks of a project that have a catalog, the latest version of each file only, as
    (id, file name, catalog)."""
    with _project_errors():
        wids = set(projects.workbooks_of(data_dir(), pid, _workbook_ids()))
    out, names = [], set()
    for e in provenance.catalog(data_dir()):  # most recently uploaded first
        if e["id"] not in wids or e["status"] == "failed" or e["name"].lower() in names:
            continue
        h = _catalog(e["id"])
        if h is not None and h.cards:
            names.add(e["name"].lower())
            out.append((e["id"], e["name"], h))
    return out


def _project_chat(pid: str, req: ProjectChatRequest, on_step=None) -> dict:
    """Questions about the data go to one agent over the tables of every workbook in the project, so it can compare
    and join them; everything else (bare cell ids, structure, issues, or no model) is answered about the workbook the
    user is looking at, as /workbooks/{id}/chat does. The language model gets the project's instructions and documents
    with the question; `knowledge` in the reply lists the documents it was given."""
    context, used = knowledge.context_for(data_dir(), pid, req.message)
    res = _project_answer(pid, req, on_step, context)
    if used and res.get("source") in ("llm", "agent"):  # the rules read none of it
        res["knowledge"] = used
    return res


def _project_answer(pid: str, req: ProjectChatRequest, on_step, context: str) -> dict:
    members = _project_members(pid)
    focus = req.workbook if req.workbook and _ID_RE.match(req.workbook) and (data_dir() / req.workbook / "ir.db").exists() \
        else (members[0][0] if members else None)
    llm = get_llm()
    if len(members) <= 1 or llm is None:
        if focus is None:
            raise HTTPException(400, "this project has no parsed workbook to answer from")
        return {**_chat(focus, req, on_step, context=context, pid=pid), "workbook": focus}
    if focus is not None:
        con = _connect(focus)
        try:
            if _only_cells(con, req.message):
                return {**_chat(focus, req, on_step), "workbook": focus}
        finally:
            con.close()
    cat = ProjectCatalog(members)
    try:
        route = _route(req.message, llm, cat)
        if route in ("data", "what_if") or focus is None:
            wids = ([focus] if focus else []) + [m[0] for m in members]
            return _agent_reply(req.message, cat, llm, req.lang, on_step, context, _whatif_tools(pid, wids, req.message))
    except Exception as e:  # noqa: BLE001 - as in _chat: the file in view still gets an answer, and the reason
        log.exception("project agent failed; answering about the workbook in view")
        if focus is None:
            raise
        return {**_chat(focus, req, on_step, route="ir", context=context), "workbook": focus,
                "fallback": f"{type(e).__name__}: {e}"[:300]}
    return {**_chat(focus, req, on_step, route=route, context=context, pid=pid), "workbook": focus}


# ---- project knowledge: instructions and documents --------------------------------------------------------------------

class _doc_errors(_project_errors):
    def __exit__(self, kind, exc, tb):
        if isinstance(exc, knowledge.DocError):
            raise HTTPException(exc.status, {"kind": exc.kind, "message": str(exc)}) from exc
        return super().__exit__(kind, exc, tb)


class Instructions(BaseModel):
    instructions: str


@app.get("/api/projects/{pid}/knowledge")
def project_knowledge(pid: str):
    """The project's instructions and documents (without their text)."""
    with _doc_errors():
        return {"instructions": projects.instructions(data_dir(), pid), "docs": knowledge.listing(data_dir(), pid),
                "limits": {"instructions": knowledge.MAX_INSTRUCTIONS, "doc_bytes": knowledge.MAX_DOC_BYTES,
                           "suffixes": list(knowledge.DOC_SUFFIXES)}}


@app.put("/api/projects/{pid}/instructions")
def set_project_instructions(pid: str, req: Instructions):
    with _doc_errors():
        return {"instructions": projects.set_instructions(data_dir(), pid, req.instructions, knowledge.MAX_INSTRUCTIONS)}


@app.post("/api/projects/{pid}/docs")
def add_project_doc(pid: str, file: UploadFile):
    """Add a .md, .txt, .docx or .pdf document; its text is extracted now. The same document added again is kept once."""
    with _doc_errors():
        return knowledge.add(data_dir(), pid, file.filename, file.file.read())


@app.get("/api/projects/{pid}/docs/{doc_id}/text")
def project_doc_text(pid: str, doc_id: str):
    """The text extracted from a document, as the language model reads it."""
    with _doc_errors():
        return {"text": knowledge.text(data_dir(), pid, doc_id)}


@app.delete("/api/projects/{pid}/docs/{doc_id}")
def remove_project_doc(pid: str, doc_id: str):
    with _doc_errors():
        knowledge.remove(data_dir(), pid, doc_id)
    return {"deleted": doc_id}


@app.post("/api/projects/{pid}/chat/stream")
def project_chat_stream(pid: str, req: ProjectChatRequest):
    """As /workbooks/{id}/chat/stream, over a whole project (see _project_chat)."""
    with _project_errors():
        projects.workbooks_of(data_dir(), pid, set())  # 404 before the stream starts
    return _sse(lambda on_step: _project_chat(pid, req, on_step=on_step))


def _sse(run) -> StreamingResponse:
    """Run `run(on_step)` in a thread and stream its steps, then its result, as server-sent events."""
    events: queue.Queue = queue.Queue()

    def work():
        try:
            events.put(("final", run(lambda s: events.put(("step", s.model_dump())))))
        except Exception as e:  # noqa: BLE001
            events.put(("error", {"error": f"{type(e).__name__}: {e}"[:300]}))
        finally:
            events.put(None)

    # A plain thread starts with an empty context: carry this request's (its user, users.py) into it.
    threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()

    def stream():
        while True:
            item = events.get()
            if item is None:
                return
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(jsonable(data), ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


class TwinRequest(BaseModel):
    scenarios: int = 10
    seed: int = 0
    v2: bool = True


@app.get("/api/workbooks/{wid}/twin")
def get_twin(wid: str):
    """The Python twin's last check (translation coverage, V1 vs Excel, V2 vs pycel, per-group results), or
    {"status": "not_run"} if it was never checked."""
    return stored_twin(_db_path(wid)) or {"status": "not_run"}


@app.post("/api/workbooks/{wid}/twin")
def run_twin(wid: str, req: TwinRequest | None = None):
    """Translate the workbook's formulas to Python and verify them; the result is kept in ir.db, and the twin as
    twin.py next to it (downloadable from the exports)."""
    req = req or TwinRequest()
    db = _db_path(wid)
    src = provenance.source_file(db.parent)
    check_twin(db, src, v2=req.v2, scenarios=max(0, min(req.scenarios, 100)), seed=req.seed)
    (db.parent / "twin.py").write_text(export_module(db), encoding="utf-8")
    return stored_twin(db)


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
    twin = db.parent / "twin.py"
    if twin.exists() and twin.stat().st_mtime >= db.stat().st_mtime - 1:  # written by the last twin check
        out.append({"name": "twin.py", "label": "Python 分身", "format": "Python", "size": twin.stat().st_size,
                    "source": False})
    return out


@app.get("/api/workbooks/{wid}/exports/{name}")
def download_export(wid: str, name: str, lang: str = "zh", inline: bool = False):
    """One converted file. Reports are written in `lang` (zh | en); with `inline`, a report opens in the browser
    instead of downloading (the HTML report can then be printed or saved as PDF)."""
    db = _db_path(wid)
    stem = Path(dict(_connect(wid).execute("SELECT key, value FROM meta").fetchall())["source"]).stem
    if name == "ir.db":
        return FileResponse(db, media_type=EXPORTS[name][2], filename=f"{stem}.{wid}.sheetir.db")
    if name == "twin.py":
        if not (db.parent / "twin.py").exists():
            raise HTTPException(404, "the twin has not been built yet: POST /api/workbooks/{id}/twin")
        return FileResponse(db.parent / "twin.py", media_type="text/x-python; charset=utf-8",
                            filename=f"{stem}.{wid}.twin.py")
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


# The twin compute engine and the project history (what-if, sensitivity, goal seek, corrections): history_api.py.
app.include_router(history_router)

# Registered last so the API routes above take precedence over the static mount.
if (CHAT_UI / "index.html").exists():
    app.mount("/", StaticFiles(directory=CHAT_UI, html=True), name="chat-ui")
else:
    app.add_api_route("/", legacy, response_class=HTMLResponse, include_in_schema=False)
