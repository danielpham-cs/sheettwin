"""Which workbook file produced which SheetIR result.

Each result lives in data/<id>/, where <id> is the first 12 hex digits of the SHA-256 of the file's bytes,
so the same bytes always map to the same result. Next to the copy of the file and its ir.db, a
source.json records where the result came from:

    name            file name at the first upload
    aliases         other names the same bytes were uploaded under
    sha256, size    of the bytes
    file_modified   the file's last-modified time on the uploader's machine (when the browser sends it)
    first_uploaded, last_uploaded, uploads
    seq, last_seq   upload counters across the data folder: they order uploads made within the same second

Results with the same file name are versions of one workbook, numbered by first upload (v1, v2, …).
`diff` compares two results cell by cell.

Issues the user marked as intended are kept in ignored.json ([{cell, kind, detail, note, at}, ...]) and applied on
every parse; a new version of a workbook starts with the ignores of the version before it.

An upload that could not be parsed keeps its folder (the copy and source.json) plus error.json: {kind, detail, at,
sheetir_version}. It is listed with status "failed", so the user sees why and can retry once SheetIR handles it.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from ..convert import SUFFIXES

MANIFEST = "source.json"
FAILURE = "error.json"
OVERRIDES = "overrides.json"  # tables the user defined, per sheet: applied on every parse
IGNORED = "ignored.json"  # issues the user marked as intended: applied on every parse
MAX_DIFF_ROWS = 200


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def db_meta(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def read(folder: Path) -> dict:
    """The folder's source record; for results stored before source.json existed, rebuilt from ir.db."""
    p = folder / MANIFEST
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    meta = db_meta(folder / "ir.db")
    src = folder / Path(meta["source"]).name
    stamp = meta.get("built_at")
    return {"name": src.name, "aliases": [], "sha256": meta["sha256"],
            "size": src.stat().st_size if src.exists() else None, "file_modified": None,
            "first_uploaded": stamp, "last_uploaded": stamp, "uploads": 1}


def record_upload(folder: Path, name: str, sha256: str, size: int, file_modified: str | None) -> tuple[dict, bool]:
    """Write or update source.json for an upload. Returns (record, whether these bytes were seen before)."""
    seen = (folder / "ir.db").exists()
    known = seen or (folder / MANIFEST).exists()  # parsed before, or tried before and failed
    counter = 1 + max((read(f).get("last_seq", 0) for f in folder.parent.iterdir() if (f / MANIFEST).exists()), default=0)
    rec = read(folder) if known else {"name": name, "aliases": [], "sha256": sha256, "size": size,
                                     "file_modified": file_modified, "first_uploaded": now(), "uploads": 0,
                                     "seq": counter}
    if name != rec["name"] and name not in rec["aliases"]:
        rec["aliases"].append(name)
    rec["file_modified"] = rec.get("file_modified") or file_modified
    rec["last_uploaded"] = now()
    rec["uploads"] = rec.get("uploads", 0) + 1
    rec["last_seq"] = counter
    (folder / MANIFEST).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec, seen


def record_failure(folder: Path, kind: str, detail: str, version: str) -> dict:
    rec = {"kind": kind, "detail": detail, "at": now(), "sheetir_version": version}
    (folder / FAILURE).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def clear_failure(folder: Path) -> None:
    (folder / FAILURE).unlink(missing_ok=True)


def failure(folder: Path) -> dict | None:
    p = folder / FAILURE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def user_tables(folder: Path) -> dict[str, list[dict]]:
    """The user's table corrections: sheet -> [{"range", "header_rows", "total", "name"?}, ...]."""
    p = folder / OVERRIDES
    return json.loads(p.read_text(encoding="utf-8")).get("sheets", {}) if p.exists() else {}


def save_user_tables(folder: Path, sheets: dict[str, list[dict]]) -> None:
    p = folder / OVERRIDES
    if sheets:
        p.write_text(json.dumps({"sheets": sheets, "updated": now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)


def ignored(folder: Path) -> list[dict]:
    p = folder / IGNORED
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_ignored(folder: Path, items: list[dict]) -> None:
    p = folder / IGNORED
    if items:
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)


def inherit_ignored(folder: Path) -> None:
    """Give a new version the ignores of the most recent other upload with the same file name."""
    if (folder / IGNORED).exists():
        return
    name = read(folder)["name"].lower()
    others = [f for f in folder.parent.iterdir() if f != folder and (f / MANIFEST).exists() and (f / IGNORED).exists()
              and read(f)["name"].lower() == name]
    if others:
        latest = max(others, key=lambda f: read(f).get("last_seq", 0))
        save_ignored(folder, ignored(latest))


def source_file(folder: Path) -> Path | None:
    """The stored copy of the workbook (named as at the first upload)."""
    rec = read(folder)
    p = folder / rec["name"]
    if p.exists():
        return p
    return next((q for q in folder.iterdir() if q.suffix.lower() in SUFFIXES), None)


def catalog(data: Path) -> list[dict]:
    """Every stored result with its source record, grouped into versions by file name."""
    entries = []
    for folder in sorted({p.parent for p in [*data.glob("*/ir.db"), *data.glob(f"*/{MANIFEST}")]}):
        db = folder / "ir.db"
        rec = read(folder) if (folder / MANIFEST).exists() or db.exists() else None
        if rec is None:
            continue
        if not db.exists():  # an upload that could not be parsed
            entries.append({
                "id": folder.name, "name": rec["name"], "aliases": rec.get("aliases", []), "sha256": rec["sha256"],
                "size": rec.get("size"), "file_modified": rec.get("file_modified"),
                "first_uploaded": rec.get("first_uploaded"), "last_uploaded": rec.get("last_uploaded"),
                "uploads": rec.get("uploads", 1), "built_at": None, "sheetir_version": None, "ir_schema": None,
                "table_scorer": "hand", "high_issues": 0, "status": "failed", "error": failure(folder),
                "_seq": rec.get("seq", 0), "_last_seq": rec.get("last_seq", 0),
            })
            continue
        meta = db_meta(db)
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        high = con.execute("SELECT COUNT(*) FROM issues WHERE severity='high'").fetchone()[0]
        con.close()
        entries.append({
            "id": folder.name, "name": rec["name"], "aliases": rec.get("aliases", []), "sha256": rec["sha256"],
            "size": rec.get("size"), "file_modified": rec.get("file_modified"),
            "first_uploaded": rec.get("first_uploaded"), "last_uploaded": rec.get("last_uploaded"),
            "uploads": rec.get("uploads", 1), "built_at": meta.get("built_at"),
            "sheetir_version": meta.get("sheetir_version"), "ir_schema": meta.get("ir_schema"),
            "table_scorer": meta.get("table_scorer", "hand"), "high_issues": high,
            "status": meta.get("parse_status", "unverified"), "error": failure(folder),
            "_seq": rec.get("seq", 0), "_last_seq": rec.get("last_seq", 0),
        })
    by_name: dict[str, list[dict]] = {}
    for e in entries:
        by_name.setdefault(e["name"].lower(), []).append(e)
    for group in by_name.values():
        group.sort(key=lambda e: (e["first_uploaded"] or "", e["_seq"], e["id"]))
        for i, e in enumerate(group, start=1):
            e["version"], e["versions"] = i, len(group)
            e["previous"] = group[i - 2]["id"] if i > 1 else None
    entries.sort(key=lambda e: (e["last_uploaded"] or "", e["_last_seq"]), reverse=True)
    for e in entries:
        del e["_seq"], e["_last_seq"]
    return entries


def _cells(db: Path) -> dict[str, tuple]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {r[0]: r[1:] for r in con.execute("SELECT cell_id, vtype, value, formula, sheet FROM cells")}
    finally:
        con.close()


def diff(new_db: Path, old_db: Path) -> dict:
    """Cells added, removed or changed (value or formula) from the old result to the new one."""
    new, old = _cells(new_db), _cells(old_db)
    rows = []
    for cid in sorted(new.keys() | old.keys()):
        a, b = old.get(cid), new.get(cid)
        if a == b:
            continue
        change = "added" if a is None else "removed" if b is None else "changed"
        show = lambda c: None if c is None else (c[2] if c[0] == "formula" else c[1])  # noqa: E731
        rows.append({"cell_id": cid, "change": change, "old": show(a), "new": show(b)})
    counts = {k: sum(r["change"] == k for r in rows) for k in ("added", "removed", "changed")}
    sheets_new = {c[3] for c in new.values()}
    sheets_old = {c[3] for c in old.values()}
    return {"counts": counts, "identical": not rows,
            "sheets_added": sorted(sheets_new - sheets_old), "sheets_removed": sorted(sheets_old - sheets_new),
            "cells": rows[:MAX_DIFF_ROWS], "truncated": len(rows) > MAX_DIFF_ROWS}
