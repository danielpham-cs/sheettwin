"""A project's history of what-if analyses and data corrections: append-only, so every step can be seen and gone back to.

    data/projects/<id>/history.db

Two tables, neither of which can be updated or deleted from (triggers refuse it):

    records   one row per analysis or correction, as it was made
        kind            scenario | sensitivity | goal_seek | correction | revert
        workbook        the workbook result it was made on (data/<id>/, the SHA-256 of the file's bytes), its file name
                        and full SHA-256: a record always says which version of the data it used
        parent          (analyses) the record it continues: scenarios form a tree whose root is the workbook as stored
        changes         scenario kinds: every changed cell -> its value, counted from the workbook (not from the
                        parent), so a record can be run on its own; corrections: cell -> contents as typed in Excel
        params          what else was asked (target, step, goal, …; a correction's reason, a revert's target version)
        result          what came out (scenario kinds: changed cells and key outputs, as computed then)
        fingerprint     digest of every computed value: replaying the record with the same twin gives the same one
        author, source  who made it (the signed-in user, or the name given when users are not separated) and how
                        (user | agent | api)
        sheetir_version, twin_status, created
    events    what happened to a record afterwards: proposed, approved, rejected, withdrawn (corrections), replayed,
              exported, reapplied — each with who, when, a note and data (an approval names the new workbook result)

A correction is proposed first (usually by the chat's model, on the user's request: source "agent") and becomes the
official data only when the user confirms it; the model can propose but never approve. SheetIR then writes its edits
into a copy of the workbook, which is stored as the file's next version (the version before it stays, and can be
brought back the same way: a revert is a correction whose edits restore an older version's cells).

Going back to a scenario is choosing it: every record keeps all its changes, so running it again, comparing it with
another, continuing from it (a new record with it as parent), or exporting it as a copy of the workbook all work from
the record alone. Nothing is ever rewritten.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path

from . import __version__

FILE = "history.db"
ANALYSES = ("scenario", "sensitivity", "goal_seek")
CHANGES = ("correction", "revert")
KINDS = ANALYSES + CHANGES
REVIEW = ("proposed", "approved", "rejected", "withdrawn")
ACTIONS = REVIEW + ("replayed", "exported", "reapplied")
_REF = re.compile(r"^[SsCc]?(\d+)$")
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS records(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    workbook TEXT NOT NULL, workbook_name TEXT, sha256 TEXT,
    parent INTEGER REFERENCES records(id),
    author TEXT NOT NULL, source TEXT NOT NULL,
    created TEXT NOT NULL,
    note TEXT,
    changes TEXT NOT NULL, params TEXT, result TEXT, fingerprint TEXT,
    sheetir_version TEXT, twin_status TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record INTEGER NOT NULL REFERENCES records(id),
    action TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL, note TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_records_workbook ON records(workbook);
CREATE INDEX IF NOT EXISTS ix_events_record ON events(record);
CREATE TRIGGER IF NOT EXISTS records_append_only_u BEFORE UPDATE ON records
    BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_append_only_d BEFORE DELETE ON records
    BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class HistoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def ref(rec_id: int, kind: str) -> str:
    """How a record is cited: S12 for an analysis, C13 for a correction or revert."""
    return f"{'C' if kind in CHANGES else 'S'}{rec_id}"


def parse_ref(text) -> int:
    m = _REF.match(str(text or "").strip())
    if not m:
        raise HistoryError(400, f"{text!r} is not a history record (S12, C13 or 12)")
    return int(m.group(1))


def _dumps(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def _loads(v):
    return None if v is None else json.loads(v)


class History:
    def __init__(self, folder: Path):
        self.path = Path(folder) / FILE

    def _con(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        return con

    # ---- writing ------------------------------------------------------------------------------------------------

    def add(self, kind: str, workbook: dict, author: str, source: str, changes: dict, *, parent: int | None = None,
            note: str | None = None, params: dict | None = None, result: dict | None = None,
            fingerprint: str | None = None, twin_status: str | None = None) -> dict:
        """Record an analysis or a correction. `workbook`: {"id", "name", "sha256"}. A correction starts proposed."""
        if kind not in KINDS:
            raise HistoryError(400, f"unknown record kind {kind!r}")
        with _lock:
            con = self._con()
            try:
                if parent is not None:
                    p = con.execute("SELECT kind FROM records WHERE id = ?", (parent,)).fetchone()
                    if p is None or p["kind"] not in ANALYSES:
                        raise HistoryError(400, f"parent {parent} is not an analysis in this project's history")
                cur = con.execute(
                    "INSERT INTO records(kind, workbook, workbook_name, sha256, parent, author, source, created, "
                    "note, changes, params, result, fingerprint, sheetir_version, twin_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, workbook["id"], workbook.get("name"), workbook.get("sha256"), parent, author, source, now(),
                     (note or "").strip()[:2000] or None, _dumps(changes), _dumps(params), _dumps(result),
                     fingerprint, __version__, twin_status))
                rid = cur.lastrowid
                if kind in CHANGES:
                    con.execute("INSERT INTO events(record, action, by, at, note) VALUES (?,?,?,?,?)",
                                (rid, "proposed", author, now(), None))
                con.commit()
            finally:
                con.close()
        return self.get(rid)

    def event(self, rec_id: int, action: str, by: str, note: str | None = None, data: dict | None = None) -> dict:
        if action not in ACTIONS:
            raise HistoryError(400, f"unknown action {action!r}")
        with _lock:
            con = self._con()
            try:
                con.execute("INSERT INTO events(record, action, by, at, note, data) VALUES (?,?,?,?,?,?)",
                            (rec_id, action, by, now(), (note or "").strip()[:2000] or None, _dumps(data)))
                con.commit()
            finally:
                con.close()
        return self.get(rec_id)

    def review(self, rec_id: int, action: str, by: str, note: str | None = None, data: dict | None = None,
               check=None) -> dict:
        """Approve, reject or withdraw a pending correction. `check(record)` runs under the lock before the event is
        written (an approval applies the edits there), so one proposal cannot be applied twice. Anyone who can see
        the project may approve or reject, the one who asked for it included; only that person can withdraw it."""
        if action not in ("approved", "rejected", "withdrawn"):
            raise HistoryError(400, f"unknown review action {action!r}")
        with _lock:
            rec = self._get(rec_id)
            if rec["kind"] not in CHANGES:
                raise HistoryError(400, f"{rec['ref']} is an analysis: only corrections are reviewed")
            if rec["status"] != "pending":
                raise HistoryError(409, f"{rec['ref']} is already {rec['status']}")
            proposer = rec["author"].strip().lower()
            if action == "withdrawn" and by.strip().lower() != proposer:
                raise HistoryError(403, f"only {rec['author']}, who proposed {rec['ref']}, can withdraw it")
            if check is not None:
                data = {**(data or {}), **(check(rec) or {})}
            con = self._con()
            try:
                con.execute("INSERT INTO events(record, action, by, at, note, data) VALUES (?,?,?,?,?,?)",
                            (rec_id, action, by, now(), (note or "").strip()[:2000] or None, _dumps(data)))
                con.commit()
            finally:
                con.close()
        return self.get(rec_id)

    # ---- reading ------------------------------------------------------------------------------------------------

    def _row(self, r: sqlite3.Row, events: list[dict]) -> dict:
        d = dict(r)
        for k in ("changes", "params", "result"):
            d[k] = _loads(d[k])
        d["ref"] = ref(d["id"], d["kind"])
        d["events"] = events
        d["status"] = _status(d["kind"], events)
        applied = next((e for e in events if e["action"] == "approved"), None)
        d["applied_as"] = (applied.get("data") or {}).get("workbook") if applied else None
        return d

    def _get(self, rec_id: int) -> dict:
        if not self.path.exists():
            raise HistoryError(404, f"no record {rec_id} in this project's history")
        con = self._con()
        try:
            r = con.execute("SELECT * FROM records WHERE id = ?", (rec_id,)).fetchone()
            if r is None:
                raise HistoryError(404, f"no record {rec_id} in this project's history")
            events = [{**dict(e), "data": _loads(e["data"])} for e in
                      con.execute("SELECT action, by, at, note, data FROM events WHERE record = ? ORDER BY id",
                                  (rec_id,))]
        finally:
            con.close()
        return self._row(r, events)

    def get(self, rec_id: int) -> dict:
        return self._get(rec_id)

    def list(self, workbook: str | None = None, kind: str | None = None, status: str | None = None,
             limit: int = 500) -> list[dict]:
        """Records, newest first, without their full results (get() has those)."""
        if not self.path.exists():
            return []
        con = self._con()
        try:
            sql, args = "SELECT * FROM records WHERE 1=1", []
            if workbook:
                sql += " AND workbook = ?"
                args.append(workbook)
            if kind:
                sql += " AND kind = ?"
                args.append(kind)
            rows = con.execute(sql + " ORDER BY id DESC", args).fetchall()
            events: dict[int, list] = {}
            for e in con.execute("SELECT record, action, by, at, note, data FROM events ORDER BY id"):
                events.setdefault(e["record"], []).append(
                    {"action": e["action"], "by": e["by"], "at": e["at"], "note": e["note"], "data": _loads(e["data"])})
        finally:
            con.close()
        out = []
        for r in rows:
            d = self._row(r, events.get(r["id"], []))
            if status and d["status"] != status:
                continue
            d["summary"] = summary(d)
            d.pop("result", None)
            out.append(d)
            if len(out) >= limit:
                break
        return out


def _status(kind: str, events: list[dict]) -> str:
    if kind in ANALYSES:
        return "recorded"
    last = next((e["action"] for e in reversed(events) if e["action"] in REVIEW), "proposed")
    return {"proposed": "pending"}.get(last, last)


def summary(rec: dict) -> str:
    """One line for a history list."""
    n = len(rec.get("changes") or {})
    p = rec.get("params") or {}
    if rec["kind"] == "scenario":
        return f"{n} cell{'s' if n != 1 else ''} changed"
    if rec["kind"] == "sensitivity":
        return f"sensitivity of {p.get('target')} (±{round(100 * float(p.get('step') or 0), 4)} %)"
    if rec["kind"] == "goal_seek":
        return f"goal seek: {p.get('target')} = {p.get('goal')} by changing {p.get('changing')}"
    if rec["kind"] == "revert":
        return f"bring back version {p.get('to_name') or p.get('to')} ({n} cells)"
    return f"correction of {n} cell{'s' if n != 1 else ''}: {p.get('reason') or ''}".strip().rstrip(":")
