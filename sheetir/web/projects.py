"""Projects: named groups of stored workbooks that the user works on together.

    data/projects/<id>/project.json     {"id", "name", "created", "workbooks": [<workbook id>, ...]}

A project only lists workbook ids; each workbook stays in data/<id>/ (see provenance.py), so one workbook can belong to
several projects and every per-workbook view keeps working. The project "default" always exists and also holds every
workbook that is in no other project, so results stored before projects existed, and a file removed from its last
project, are never lost from sight. The query agent of a project sees the tables of all its workbooks.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from pathlib import Path

from .provenance import now

DEFAULT = "default"
FILE = "project.json"
ID_RE = re.compile(r"^(default|[0-9a-f]{12})$")
MAX_NAME = 80
_lock = threading.Lock()  # sync FastAPI endpoints run in a thread pool


class ProjectError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _root(data: Path) -> Path:
    return data / "projects"


def _path(data: Path, pid: str) -> Path:
    if not ID_RE.match(pid):
        raise ProjectError(400, "bad project id")
    return _root(data) / pid / FILE


def _read(data: Path, pid: str) -> dict | None:
    p = _path(data, pid)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _write(data: Path, rec: dict) -> None:
    p = _path(data, rec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _all(data: Path) -> list[dict]:
    out = []
    root = _root(data)
    if root.is_dir():
        for p in root.glob(f"*/{FILE}"):
            if ID_RE.match(p.parent.name):
                out.append(json.loads(p.read_text(encoding="utf-8")))
    if not any(r["id"] == DEFAULT for r in out):
        rec = {"id": DEFAULT, "name": "Default", "created": now(), "workbooks": []}
        _write(data, rec)
        out.append(rec)
    return out


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise ProjectError(400, "a project needs a name")
    return name


def listing(data: Path, workbook_ids: set[str]) -> list[dict]:
    """Every project with the stored workbooks it holds: "default" first (plus the workbooks in no other project),
    then the others by creation time."""
    with _lock:
        recs = _all(data)
    claimed = {w for r in recs if r["id"] != DEFAULT for w in r["workbooks"]}
    out = []
    for r in recs:
        wids = [w for w in r["workbooks"] if w in workbook_ids]
        if r["id"] == DEFAULT:
            wids += sorted(w for w in workbook_ids if w not in claimed and w not in wids)
        out.append({"id": r["id"], "name": r["name"], "created": r.get("created"), "workbooks": wids})
    out.sort(key=lambda r: (r["id"] != DEFAULT, r["created"] or ""))
    return out


def workbooks_of(data: Path, pid: str, workbook_ids: set[str]) -> list[str]:
    for r in listing(data, workbook_ids):
        if r["id"] == pid:
            return r["workbooks"]
    raise ProjectError(404, "project not found")


def create(data: Path, name: str) -> dict:
    rec = {"id": secrets.token_hex(6), "name": _clean_name(name), "created": now(), "workbooks": []}
    with _lock:
        _all(data)
        _write(data, rec)
    return rec


def rename(data: Path, pid: str, name: str) -> dict:
    with _lock:
        rec = _read(data, pid)
        if rec is None:
            raise ProjectError(404, "project not found")
        rec["name"] = _clean_name(name)
        _write(data, rec)
    return rec


def delete(data: Path, pid: str) -> None:
    """Remove a project (not "default"). Its files are kept: those in no other project show up in "default"."""
    if pid == DEFAULT:
        raise ProjectError(400, "the default project cannot be deleted")
    with _lock:
        if _read(data, pid) is None:
            raise ProjectError(404, "project not found")
        shutil.rmtree(_path(data, pid).parent)


def add(data: Path, pid: str, wid: str) -> None:
    with _lock:
        rec = _read(data, pid) if pid != DEFAULT else next(r for r in _all(data) if r["id"] == DEFAULT)
        if rec is None:
            raise ProjectError(404, "project not found")
        if wid not in rec["workbooks"]:
            rec["workbooks"].append(wid)
            _write(data, rec)


def remove(data: Path, pid: str, wid: str) -> None:
    with _lock:
        rec = _read(data, pid)
        if rec is None:
            raise ProjectError(404, "project not found")
        if wid in rec["workbooks"]:
            rec["workbooks"].remove(wid)
            _write(data, rec)


def folder(data: Path, pid: str) -> Path:
    """The project's own folder (its project.json, and the documents of knowledge.py)."""
    return _path(data, pid).parent


def _existing(data: Path, pid: str) -> dict:
    rec = next((r for r in _all(data) if r["id"] == pid), None)
    if rec is None:
        raise ProjectError(404, "project not found")
    return rec


def instructions(data: Path, pid: str) -> str:
    """What the user wrote for the language model about this project ("" when nothing)."""
    with _lock:
        return _existing(data, pid).get("instructions", "")


def set_instructions(data: Path, pid: str, text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        raise ProjectError(400, f"instructions can be up to {limit} characters")
    with _lock:
        rec = _existing(data, pid)
        rec["instructions"] = text
        _write(data, rec)
    return text


def holders(data: Path, wid: str) -> list[str]:
    """Ids of the projects that list `wid` explicitly."""
    with _lock:
        return [r["id"] for r in _all(data) if wid in r["workbooks"]]


def follow(data: Path, src: str, new: str) -> None:
    """A new version made from `src` (an edit or a fix in SheetTwin) joins the projects `src` is in."""
    for pid in holders(data, src):
        add(data, pid, new)


def forget(data: Path, wid: str) -> None:
    """A deleted workbook leaves every project."""
    with _lock:
        for rec in _all(data):
            if wid in rec["workbooks"]:
                rec["workbooks"].remove(wid)
                _write(data, rec)
