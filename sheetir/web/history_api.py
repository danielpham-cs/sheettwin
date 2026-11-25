"""HTTP API of the twin compute engine (sheetir.whatif) and of a project's history (sheetir.history).

Analyses (nothing in the workbook changes):

    POST /api/projects/{pid}/scenarios        {workbook, changes, parent?, reset?, outputs?, note?, save?}
    POST /api/projects/{pid}/sensitivity      {workbook, target, inputs?, step?, parent?, note?, save?}
    POST /api/projects/{pid}/goal-seek        {workbook, target, goal, changing, low?, high?, parent?, note?, save?}

Corrections (proposed — by the chat's model or by a person — then confirmed by a person: the approved edits become the
file's next version):

    POST /api/projects/{pid}/corrections         {workbook, edits | from_scenario, reason}
    POST /api/projects/{pid}/corrections/revert  {workbook, to, reason}       bring an older version's cells back
    POST /api/projects/{pid}/history/{ref}/approve | reject | withdraw   {note?}

History:

    GET  /api/projects/{pid}/model-cells                ?workbook= &q=: inputs and formula cells to pick from
    GET  /api/projects/{pid}/history                    ?workbook= &kind= &status=
    GET  /api/projects/{pid}/history/{ref}              one record with its result and events
    GET  /api/projects/{pid}/history/compare?a=&b=      two analyses side by side
    POST /api/projects/{pid}/history/{ref}/replay       compute again and say whether the result is the same
    POST /api/projects/{pid}/history/{ref}/reapply      {to}: the same changes on another version of the file
    GET  /api/projects/{pid}/history/{ref}/export       a copy of the workbook with the record's values typed in

`parent` continues a recorded analysis: `changes` are then added to the parent's, and `reset` drops some of them.
Who did what comes from the signed-in user (users.py); when users are not separated, from the `by` name the client
sends (any name: there is no sign-in to check it against), "local" if none.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import fixes
from ..history import ANALYSES, CHANGES, History, HistoryError, parse_ref, summary
from ..transpile import decode
from ..whatif import Engine, WhatIfError, as_typed, result_fingerprint, typed
from . import projects, provenance, users

router = APIRouter()
MAX_EDITS = 2000


def _web():
    from . import app  # the app module includes this router at its end

    return app


@contextmanager
def _errors():
    try:
        yield
    except (HistoryError, projects.ProjectError) as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except WhatIfError as exc:
        raise HTTPException(400, str(exc)) from exc


def who(by: str | None) -> str:
    """The signed-in user; without users, the name the client gave."""
    user = users.current_user()
    if user:
        return user
    name = " ".join((by or "").split())[:80]
    return name or "local"


def _history(pid: str) -> History:
    data = _web().data_dir()
    projects.workbooks_of(data, pid, set())  # 404 for an unknown project
    return History(projects.folder(data, pid))


def _workbook(pid: str, wid: str) -> dict:
    """{"id", "name", "sha256"} of a parsed workbook of the project."""
    web = _web()
    data = web.data_dir()
    if wid not in projects.workbooks_of(data, pid, web._workbook_ids()):
        raise HTTPException(404, f"workbook {wid} is not in this project")
    folder = web._folder(wid)
    web._db_path(wid)  # 404 if it was never parsed
    rec = provenance.read(folder)
    return {"id": wid, "name": rec["name"], "sha256": rec["sha256"]}


def _engine(wid: str) -> Engine:
    return Engine.cached(_web()._db_path(wid))


def _record(h: History, ref) -> dict:
    return h.get(parse_ref(ref))


def _start(h: History, parent, wid: str, changes: dict | None, reset: list[str] | None) -> tuple[int | None, dict]:
    """The parent's id and the changes counted from the workbook: the parent's, then `reset` dropped, then `changes`."""
    if parent in (None, ""):
        if reset:
            raise HTTPException(400, "reset drops changes of a parent: give the parent")
        return None, dict(changes or {})
    p = _record(h, parent)
    if p["kind"] not in ANALYSES:
        raise HTTPException(400, f"{p['ref']} is a correction: a scenario continues an analysis")
    if p["workbook"] != wid:
        raise HTTPException(400, f"{p['ref']} was made on workbook {p['workbook']}: reapply it to this one first")
    eng = _engine(wid)
    drop = {eng.key(c) for c in (reset or [])}
    base = {c: v for c, v in (p["changes"] or {}).items() if eng.key(c) not in drop}
    merged = {eng.cell_id(eng.key(c)): v for c, v in base.items()}
    for c, v in (changes or {}).items():
        merged[eng.cell_id(eng.key(c))] = v
    return p["id"], merged


def _canonical(eng: Engine, changes: dict) -> dict:
    """{canonical cell id: value as given}, every cell and value checked."""
    eng._changes(changes)  # raises WhatIfError for a bad cell or value
    return {eng.cell_id(eng.key(c)): v for c, v in changes.items()}


# ---- analyses -------------------------------------------------------------------------------------------------------

class Analysis(BaseModel):
    workbook: str
    parent: str | int | None = None
    note: str | None = None
    save: bool = True
    by: str | None = None


class ScenarioRequest(Analysis):
    changes: dict = Field(default_factory=dict)
    reset: list[str] | None = None
    outputs: list[str] | None = None


class SensitivityRequest(Analysis):
    target: str
    inputs: list[str] | None = None
    step: float = 0.1


class GoalSeekRequest(Analysis):
    target: str
    goal: float
    changing: str
    low: float | None = None
    high: float | None = None


def _save(h, kind, req: Analysis, wb, eng: Engine, parent, changes, params, result, fp, source="user") -> dict:
    if not req.save:
        return {"saved": False, "kind": kind, "changes": changes, "params": params, "result": result}
    rec = h.add(kind, wb, who(req.by), source, changes, parent=parent, note=req.note, params=params, result=result,
                fingerprint=fp, twin_status=eng.check["status"])
    return {"saved": True, **rec}


@router.post("/api/projects/{pid}/scenarios")
def run_scenario(pid: str, req: ScenarioRequest):
    """Compute the workbook with some values changed and, unless `save` is false, record it in the history."""
    with _errors():
        h = _history(pid)
        wb = _workbook(pid, req.workbook)
        parent, changes = _start(h, req.parent, wb["id"], req.changes, req.reset)
        eng = _engine(wb["id"])
        changes = _canonical(eng, changes)
        result = eng.run(changes, req.outputs)
        return _save(h, "scenario", req, wb, eng, parent, changes,
                     {"delta": req.changes, "reset": req.reset, "outputs": req.outputs}, result, result["fingerprint"])


@router.post("/api/projects/{pid}/sensitivity")
def run_sensitivity(pid: str, req: SensitivityRequest):
    with _errors():
        h = _history(pid)
        wb = _workbook(pid, req.workbook)
        parent, changes = _start(h, req.parent, wb["id"], None, None)
        eng = _engine(wb["id"])
        changes = _canonical(eng, changes)
        result = eng.sensitivity(req.target, req.inputs, req.step, changes)
        params = {"target": result["target"]["cell"], "inputs": req.inputs, "step": req.step}
        return _save(h, "sensitivity", req, wb, eng, parent, changes, params, result, result_fingerprint(result))


@router.post("/api/projects/{pid}/goal-seek")
def run_goal_seek(pid: str, req: GoalSeekRequest):
    """The value of `changing` that makes `target` reach `goal`. The record's changes include that value, so a
    scenario continued from it starts there."""
    with _errors():
        h = _history(pid)
        wb = _workbook(pid, req.workbook)
        parent, changes = _start(h, req.parent, wb["id"], None, None)
        eng = _engine(wb["id"])
        changes = _canonical(eng, changes)
        result = eng.goal_seek(req.target, req.goal, req.changing, changes, req.low, req.high)
        changes = {**changes, result["changing"]["cell"]: result["changing"]["value"]}
        params = {"target": result["target"]["cell"], "goal": req.goal, "changing": result["changing"]["cell"],
                  "low": req.low, "high": req.high}
        return _save(h, "goal_seek", req, wb, eng, parent, changes, params, result, result_fingerprint(result))


@router.get("/api/projects/{pid}/model-cells")
def model_cells(pid: str, workbook: str, q: str | None = None, limit: int = 200):
    """The cells a what-if screen offers: inputs a scenario can change and formula cells to analyze (key outputs
    first), with their values and trust statuses; `q` searches every cell by id or label."""
    with _errors():
        wb = _workbook(pid, workbook)
        return _engine(wb["id"]).catalog(q, max(1, min(limit, 1000)))


# ---- history ----------------------------------------------------------------------------------------------------

@router.get("/api/projects/{pid}/history")
def list_history(pid: str, workbook: str | None = None, kind: str | None = None, status: str | None = None):
    """Every analysis and correction of the project, newest first (without full results)."""
    with _errors():
        h = _history(pid)
        ids = _web()._workbook_ids()
        out = h.list(workbook, kind, status)
        for r in out:
            r["workbook_stored"] = r["workbook"] in ids
        return {"records": out}


@router.get("/api/projects/{pid}/history/compare")
def compare(pid: str, a: str, b: str):
    """Two analyses side by side. On the same workbook both are computed again, so every cell that differs is listed;
    on different versions only what was recorded is compared."""
    with _errors():
        h = _history(pid)
        ra, rb = _record(h, a), _record(h, b)
        for r in (ra, rb):
            if r["kind"] not in ANALYSES:
                raise HTTPException(400, f"{r['ref']} is a correction: compare analyses")
        head = {"a": {k: ra[k] for k in ("ref", "kind", "workbook", "note", "author", "created")},
                "b": {k: rb[k] for k in ("ref", "kind", "workbook", "note", "author", "created")}}
        if ra["workbook"] == rb["workbook"] and ra["workbook"] in _web()._workbook_ids():
            return {**head, "same_workbook": True, **_engine(ra["workbook"]).compare(ra["changes"], rb["changes"])}
        ca, cb = ra["changes"] or {}, rb["changes"] or {}
        inputs = [{"cell": c, "a": ca.get(c), "b": cb.get(c)} for c in sorted(set(ca) | set(cb)) if ca.get(c) != cb.get(c)]
        oa = {o["cell"]: o for o in (ra["result"] or {}).get("outputs", [])}
        ob = {o["cell"]: o for o in (rb["result"] or {}).get("outputs", [])}
        cells = [{"cell": c, "label": (oa.get(c) or ob.get(c)).get("label"), "a": (oa.get(c) or {}).get("value"),
                  "b": (ob.get(c) or {}).get("value")} for c in sorted(set(oa) | set(ob))
                 if (oa.get(c) or {}).get("value") != (ob.get(c) or {}).get("value")]
        return {**head, "same_workbook": False, "inputs": inputs, "cells": cells, "n_cells": len(cells),
                "truncated": False, "note": "different versions of the file: compared as recorded (key outputs only)"}


@router.get("/api/projects/{pid}/history/{ref}")
def get_record(pid: str, ref: str):
    with _errors():
        r = _record(_history(pid), ref)
        r["summary"] = summary(r)
        r["workbook_stored"] = r["workbook"] in _web()._workbook_ids()
        return r


def _rerun(rec: dict) -> dict:
    eng = _engine(rec["workbook"])
    p = rec["params"] or {}
    if rec["kind"] == "scenario":
        return eng.run(rec["changes"], p.get("outputs"))
    if rec["kind"] == "sensitivity":
        return eng.sensitivity(p["target"], p.get("inputs"), p.get("step", 0.1), rec["changes"])
    start = {c: v for c, v in (rec["changes"] or {}).items() if c != p["changing"]}
    return eng.goal_seek(p["target"], p["goal"], p["changing"], start, p.get("low"), p.get("high"))


class ByRequest(BaseModel):
    by: str | None = None
    note: str | None = None


@router.post("/api/projects/{pid}/history/{ref}/replay")
def replay(pid: str, ref: str, req: ByRequest | None = None):
    """Compute a recorded analysis again with today's twin: `same` says whether every value agrees with the record
    (a twin upgrade or a new check can change results; the workbook result itself never changes)."""
    req = req or ByRequest()
    with _errors():
        h = _history(pid)
        rec = _record(h, ref)
        if rec["kind"] not in ANALYSES:
            raise HTTPException(400, f"{rec['ref']} is a correction: there is nothing to compute")
        if rec["workbook"] not in _web()._workbook_ids():
            raise HTTPException(409, f"workbook {rec['workbook']} is no longer stored")
        now = _rerun(rec)
        fp = now["fingerprint"] if rec["kind"] == "scenario" else result_fingerprint(now)
        same = fp == rec["fingerprint"]
        h.event(rec["id"], "replayed", who(req.by), req.note, {"same": same, "fingerprint": fp})
        return {"ref": rec["ref"], "same": same, "recorded": rec["fingerprint"], "now": fp, "result": now}


class ReapplyRequest(ByRequest):
    to: str
    force: bool = False


@router.post("/api/projects/{pid}/history/{ref}/reapply")
def reapply(pid: str, ref: str, req: ReapplyRequest):
    """Run a recorded analysis on another version of the file. Each changed cell is checked first: it must still
    exist as a value (not a formula) and, when it had a label, keep it. With problems nothing is recorded unless
    `force` (the cells that cannot take a value are then left out)."""
    with _errors():
        h = _history(pid)
        rec = _record(h, ref)
        if rec["kind"] not in ANALYSES:
            raise HTTPException(400, f"{rec['ref']} is a correction: propose it again on the new version")
        wb = _workbook(pid, req.to)
        eng = _engine(wb["id"])
        old = _engine(rec["workbook"]) if rec["workbook"] in _web()._workbook_ids() else None
        checks, keep = [], {}
        for cell, v in (rec["changes"] or {}).items():
            try:
                ck = eng.key(cell)
            except WhatIfError as exc:
                checks.append({"cell": cell, "ok": False, "problem": str(exc)})
                continue
            if eng.is_formula(ck):
                checks.append({"cell": cell, "ok": False, "problem": "now holds a formula"})
                continue
            was = old.label(old.key(cell)) if old is not None else None
            now_label = eng.label(ck)
            if was and now_label and was != now_label:
                checks.append({"cell": cell, "ok": False, "problem": f"label changed: {was!r} -> {now_label!r}"})
                continue
            checks.append({"cell": cell, "ok": True, "label": now_label})
            keep[cell] = v
        problems = [c for c in checks if not c["ok"]]
        if problems and not req.force:
            return {"reapplied": False, "checks": checks}
        p = dict(rec["params"] or {})
        p["reapplied_from"] = rec["ref"]
        if rec["kind"] == "scenario":
            result = eng.run(keep, p.get("outputs"))
            fp = result["fingerprint"]
        elif rec["kind"] == "sensitivity":
            result = eng.sensitivity(p["target"], p.get("inputs"), p.get("step", 0.1), keep)
            fp = result_fingerprint(result)
        else:
            start = {c: v for c, v in keep.items() if c != p["changing"]}
            result = eng.goal_seek(p["target"], p["goal"], p["changing"], start, p.get("low"), p.get("high"))
            keep = {**start, result["changing"]["cell"]: result["changing"]["value"]}
            fp = result_fingerprint(result)
        new = h.add(rec["kind"], wb, who(req.by), "user", keep, parent=rec["id"], note=req.note or rec["note"],
                    params=p, result=result, fingerprint=fp, twin_status=eng.check["status"])
        h.event(rec["id"], "reapplied", who(req.by), None, {"to": wb["id"], "record": new["ref"]})
        return {"reapplied": True, "checks": checks, "record": new}


@router.get("/api/projects/{pid}/history/{ref}/export")
def export_record(pid: str, ref: str, by: str | None = None):
    """A copy of the workbook with the record's values typed in (Excel recalculates when it opens it). The stored
    workbook is not changed."""
    with _errors():
        h = _history(pid)
        rec = _record(h, ref)
        web = _web()
        if rec["kind"] in CHANGES:
            edits = rec["changes"] or {}
        else:
            edits = {c: as_typed(typed(v)) for c, v in (rec["changes"] or {}).items()}
        folder = web._folder(rec["workbook"])
        src = provenance.source_file(folder)
        if src is None:
            raise HTTPException(404, "the original workbook is no longer stored")
        if src.suffix.lower() not in fixes.EDITABLE:
            raise HTTPException(400, "only .xlsx / .xlsm files can be exported with changed values")
        try:
            data = fixes.apply(src, edits) if edits else src.read_bytes()
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        h.event(rec["id"], "exported", who(by))
        name = f"{Path(provenance.read(folder)['name']).stem}.{rec['ref']}{src.suffix.lower()}"
        return StreamingResponse(io.BytesIO(data), media_type="application/vnd.openxmlformats-officedocument."
                                 "spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---- corrections ------------------------------------------------------------------------------------------------------

class CorrectionRequest(BaseModel):
    workbook: str
    edits: dict[str, str] | None = None  # cell id -> contents as typed in Excel ("=..." formula, "" to clear)
    from_scenario: str | int | None = None
    reason: str
    by: str | None = None


def _contents(db: Path) -> dict:
    """cell id -> contents as typed in Excel, for every stored cell."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for cid, vtype, value, formula in con.execute("SELECT cell_id, vtype, value, formula FROM cells"):
            out[cid] = formula if formula is not None else as_typed(decode(value, vtype))
        return out
    finally:
        con.close()


def _editable(wid: str) -> Path:
    src = provenance.source_file(_web()._folder(wid))
    if src is None:
        raise HTTPException(404, "the original workbook is no longer stored")
    if src.suffix.lower() not in fixes.EDITABLE:
        raise HTTPException(400, "only .xlsx / .xlsm files can be corrected")
    return src


def make_proposal(h: History, kind: str, wb: dict, edits: dict, params: dict, author: str, source: str,
                  folder: Path | None = None) -> dict:
    """Record a pending correction of workbook `wb` ({"id", "name", "sha256"}): the edits are checked (each cell exists
    and can be written), the old contents kept, and the effect on the key outputs computed. Nothing changes yet."""
    if not isinstance(edits, dict) or not edits or len(edits) > MAX_EDITS:
        raise HistoryError(400, f"a correction changes between 1 and {MAX_EDITS} cells")
    folder = folder or _web()._folder(wb["id"])
    src = provenance.source_file(folder)
    if src is None:
        raise HistoryError(404, "the original workbook is no longer stored")
    if src.suffix.lower() not in fixes.EDITABLE:
        raise HistoryError(400, "only .xlsx / .xlsm files can be corrected")
    eng = Engine.cached(folder / "ir.db")
    canon = {}
    for cell, text in edits.items():
        if not isinstance(text, str):
            text = as_typed(text)
        canon[eng.cell_id(eng.key(cell))] = text
    try:
        fixes.apply(src, canon)  # every edit can be written
    except (ValueError, KeyError) as exc:
        raise HistoryError(400, str(exc)) from exc
    before = _contents(folder / "ir.db")
    params = {**params, "old": {c: before.get(c, "") for c in canon}}
    values = {c: t for c, t in canon.items() if not t.startswith("=") and not eng.is_formula(eng.key(c))}
    impact = None
    if values:
        r = eng.run(values)
        impact = {k: r[k] for k in ("outputs", "changed", "n_changed", "truncated", "warnings", "notices", "trust")}
    result = {"impact": impact, "formula_edits": len(canon) - len(values)}
    return h.add(kind, wb, author, source, canon, params=params, result=result, twin_status=eng.check["status"])


def _propose(pid: str, kind: str, wb: dict, edits: dict, params: dict, by: str | None) -> dict:
    return make_proposal(_history(pid), kind, wb, edits, params, who(by), "user")


@router.post("/api/projects/{pid}/corrections")
def propose_correction(pid: str, req: CorrectionRequest):
    """Propose changing cells of the workbook (values or formulas). Nothing changes until it is approved; the
    proposal shows what the change would do to the key outputs."""
    with _errors():
        if not req.reason.strip():
            raise HTTPException(400, "say why the data should change (reason)")
        wb = _workbook(pid, req.workbook)
        edits = dict(req.edits or {})
        params = {"reason": req.reason.strip()[:2000]}
        if req.from_scenario not in (None, ""):
            s = _record(_history(pid), req.from_scenario)
            if s["kind"] not in ANALYSES or s["workbook"] != wb["id"]:
                raise HTTPException(400, f"{s['ref']} is not an analysis of this workbook")
            edits = {**{c: as_typed(typed(v)) for c, v in (s["changes"] or {}).items()}, **edits}
            params["from_scenario"] = s["ref"]
        return _propose(pid, "correction", wb, edits, params, req.by)


class RevertRequest(BaseModel):
    workbook: str  # the current version
    to: str        # the older version whose cells come back
    reason: str
    by: str | None = None


@router.post("/api/projects/{pid}/corrections/revert")
def propose_revert(pid: str, req: RevertRequest):
    """Propose bringing back an older version of the file: every cell that differs gets the older contents (cells
    the older version did not have are cleared). Approved like any correction, it becomes the newest version; the
    versions in between stay stored."""
    with _errors():
        if not req.reason.strip():
            raise HTTPException(400, "say why the older version should come back (reason)")
        wb = _workbook(pid, req.workbook)
        web = _web()
        old_db = web._db_path(req.to)
        old_rec = provenance.read(web._folder(req.to))
        if old_rec["name"].lower() != wb["name"].lower():
            raise HTTPException(400, f"{req.to} is not a version of {wb['name']}")
        d = provenance.diff(web._db_path(wb["id"]), old_db)
        if d["sheets_added"] or d["sheets_removed"]:
            raise HTTPException(400, "the versions have different sheets: upload the older file again instead")
        new, old = _contents(web._db_path(wb["id"])), _contents(old_db)
        edits = {c: old.get(c, "") for c in sorted(set(new) | set(old)) if new.get(c) != old.get(c)}
        if not edits:
            raise HTTPException(400, "the two versions hold the same cells")
        version = next((e["version"] for e in provenance.catalog(web.data_dir()) if e["id"] == req.to), None)
        params = {"reason": req.reason.strip()[:2000], "to": req.to, "to_name": f"v{version}" if version else req.to}
        return _propose(pid, "revert", wb, edits, params, req.by)


def _latest(wid: str) -> str | None:
    """The newest version of the file `wid` is a version of."""
    entries = provenance.catalog(_web().data_dir())
    me = next((e for e in entries if e["id"] == wid), None)
    if me is None:
        return None
    same = [e for e in entries if e["name"].lower() == me["name"].lower() and e["status"] != "failed"]
    return max(same, key=lambda e: e["version"])["id"]


@router.post("/api/projects/{pid}/history/{ref}/approve")
def approve(pid: str, ref: str, req: ByRequest | None = None):
    """Approve a pending correction: its edits are written into a copy of the workbook, which is stored as the file's
    next version and joins the projects of the version it was made on. Only a person approves (this endpoint); the
    chat's model can propose a correction but has no tool to approve one."""
    req = req or ByRequest()
    web = _web()

    def apply(rec: dict) -> dict:
        if rec["workbook"] not in web._workbook_ids():
            raise HistoryError(409, f"workbook {rec['workbook']} is no longer stored")
        latest = _latest(rec["workbook"])
        if latest != rec["workbook"]:
            raise HistoryError(409, f"{rec['ref']} was proposed on an older version; the newest is {latest}: "
                                    "propose it again on that one")
        src = _editable(rec["workbook"])
        data = fixes.apply(src, rec["changes"])
        res = web._ingest(data, provenance.read(web._folder(rec["workbook"]))["name"])
        if res.get("failed"):
            raise HistoryError(422, f"the corrected workbook could not be parsed: {res.get('error')}")
        projects.follow(web.data_dir(), rec["workbook"], res["id"])
        return {"workbook": res["id"], "duplicate": res["duplicate"]}

    with _errors():
        h = _history(pid)
        rec = h.review(parse_ref(ref), "approved", who(req.by), req.note, check=apply)
        return rec


@router.post("/api/projects/{pid}/history/{ref}/reject")
def reject(pid: str, ref: str, req: ByRequest):
    with _errors():
        if not (req.note or "").strip():
            raise HTTPException(400, "say why the correction is rejected (note)")
        return _history(pid).review(parse_ref(ref), "rejected", who(req.by), req.note)


@router.post("/api/projects/{pid}/history/{ref}/withdraw")
def withdraw(pid: str, ref: str, req: ByRequest | None = None):
    req = req or ByRequest()
    with _errors():
        return _history(pid).review(parse_ref(ref), "withdrawn", who(req.by), req.note)

