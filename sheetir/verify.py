"""Check a SheetIR database against its workbook, re-read independently of SheetIR's parser.

This module imports nothing from the rest of SheetIR. It re-reads the .xlsx with plain openpyxl and the database with
plain SQL, and checks:

  content       every non-empty cell is in the IR with the same value or formula, and nothing else is
  rebuild       a workbook written from the IR alone has the same cells as the original
  references    the cells each formula reads (openpyxl's tokenizer; defined names and structured references resolved
                here) are the formula's precedents in the IR
  groups        translating each group's anchor formula to every member (openpyxl's Translator) gives the member's
                formula as stored

It also reports coverage: formulas SheetIR read but cannot fully follow (INDIRECT, links to other files, unresolved
names, …), and parts of the file it does not model (macros, pivot tables, charts, …).

Status: "verified" (all checks pass, every formula understood), "partial" (all checks pass, some formulas not fully
understood), "check_failed" (a check found a difference: the result may be wrong), "unverified" (the check itself could
not run; `error` says why).
"""

from __future__ import annotations

import bisect
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula

MAX_EXAMPLES = 8
MAX_NOT_UNDERSTOOD = 50

# Issue kinds meaning SheetIR read the formula but cannot follow all of what it depends on.
NOT_UNDERSTOOD = ("parse_error", "unresolved_ref", "unresolved_name", "dynamic_ref", "external_link",
                  "broken_ref", "unknown_sheet", "data_table")

# Parts of an .xlsx that SheetIR does not model, found by their file names inside the package.
IGNORED_PARTS = [
    ("macros", re.compile(r"xl/vbaProject\.bin$")),
    ("pivot_tables", re.compile(r"xl/pivotTables/pivotTable\d*\.xml$")),
    ("charts", re.compile(r"xl/charts/chart\d*\.xml$")),
    ("external_links", re.compile(r"xl/externalLinks/externalLink\d*\.xml$")),
    ("comments", re.compile(r"xl/(comments\d*\.xml|threadedComments/.*\.xml)$")),
    ("images", re.compile(r"xl/media/.+")),
    ("slicers", re.compile(r"xl/slicers/slicer\d*\.xml$")),
]


def cid(sheet: str, row: int, col: int) -> str:
    s = sheet if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", sheet) else "'" + sheet.replace("'", "''") + "'"
    return f"{s}!{get_column_letter(col)}{row}"


def split_id(cell_id: str) -> tuple[str, int, int]:
    s, a1 = cell_id.rsplit("!", 1)
    s = s[1:-1].replace("''", "'") if s.startswith("'") else s
    c, r, _, _ = range_boundaries(a1)
    return s, r, c


def _load(path: Path):
    """openpyxl's workbook; if the formatting part is unreadable, read again without it (cells are unaffected)."""
    try:
        return load_workbook(path, data_only=False)
    except Exception:  # noqa: BLE001
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "no_styles.xlsx"
            with zipfile.ZipFile(path) as zin, zipfile.ZipFile(fixed, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename != "xl/styles.xml":
                        zout.writestr(item, zin.read(item.filename))
            return load_workbook(fixed, data_only=False)


def read_xlsx(path: Path) -> tuple[dict, object]:
    """cell id -> ("formula", text) | ("value", python value), for every cell holding something."""
    wb = _load(path)
    out = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if v is None:
                    continue
                if isinstance(v, ArrayFormula):
                    out[cid(ws.title, c.row, c.column)] = ("formula", v.text)
                elif c.data_type == "f":
                    out[cid(ws.title, c.row, c.column)] = ("formula", str(v))
                else:
                    out[cid(ws.title, c.row, c.column)] = ("value", v)
    return out, wb


def norm(v):
    """Comparable form of a constant: SheetIR stores values as JSON (dates as text)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return str(v)


def excel_tables(wb) -> dict[str, dict]:
    """Excel Tables (ListObjects) as declared in the file: name -> sheet, data bounds, columns."""
    out = {}
    for ws in wb.worksheets:
        for t in ws.tables.values():
            c1, r1, c2, r2 = range_boundaries(t.ref)
            head = 1 if t.headerRowCount is None else t.headerRowCount
            out[t.displayName.upper()] = {"sheet": ws.title, "c1": c1, "r1": r1 + head, "c2": c2,
                                          "r2": r2 - (t.totalsRowCount or 0), "cols": [c.name.upper() for c in t.tableColumns]}
    return out


_STRUCT = re.compile(r"^(?P<table>[A-Za-z_][\w.]*)?\[(?P<spec>.*)\]$")


def structured(text: str, host: str, row: int, col: int, tables: dict) -> tuple[str, int, int, int, int] | None:
    """Resolve Table[Column], Table[[#This Row],[Column]], [@Column] and [@[Column name]] to (sheet, c1, r1, c2, r2)."""
    m = _STRUCT.match(text)
    if not m:
        return None
    if m["table"]:
        t = tables.get(m["table"].upper())
    else:  # unqualified: the table the formula sits in
        t = next((t for t in tables.values() if t["sheet"] == host and t["c1"] <= col <= t["c2"] and t["r1"] <= row <= t["r2"]), None)
    if t is None:
        return None
    spec = m["spec"]
    this_row = spec.startswith("@") or "#THIS ROW" in spec.upper()
    names = [n.strip().upper() for n in re.findall(r"\[([^\[\]]+)\]", spec) if not n.startswith("#")] or [spec.lstrip("@").strip().upper()]
    names = [n for n in names if n and not n.startswith("#")]
    if not names:
        return t["sheet"], t["c1"], t["r1"], t["c2"], t["r2"]
    if any(n not in t["cols"] for n in names):
        return None
    cs = [t["c1"] + t["cols"].index(n) for n in names]
    r1, r2 = (row, row) if this_row else (t["r1"], t["r2"])
    return t["sheet"], min(cs), r1, max(cs), r2


class Occupied:
    """Non-empty cells per sheet, indexed column -> sorted rows, to expand ranges without scanning every cell."""

    def __init__(self, ids):
        self.cols: dict[str, dict[int, list[int]]] = {}
        for i in ids:
            s, r, c = split_id(i)
            self.cols.setdefault(s, {}).setdefault(c, []).append(r)
        for by_col in self.cols.values():
            for rows in by_col.values():
                rows.sort()

    def cells(self, sheet, c1, r1, c2, r2):
        for c, rows in self.cols.get(sheet, {}).items():
            if c1 is not None and not c1 <= c <= c2:
                continue
            lo = 0 if r1 is None else bisect.bisect_left(rows, r1)
            hi = len(rows) if r2 is None else bisect.bisect_right(rows, r2)
            for r in rows[lo:hi]:
                yield cid(sheet, r, c)


def references(formula: str, host: str, at: tuple[int, int], names: dict, tables: dict, occ: Occupied) -> tuple[set, list]:
    """Cells a formula reads, from openpyxl's tokenizer; references it cannot resolve are returned apart."""
    found, unresolved = set(), []
    try:
        items = Tokenizer(formula).items
    except Exception:
        return found, [formula]
    for tok in items:
        if tok.type != "OPERAND" or tok.subtype != "RANGE":
            continue
        text = names.get(tok.value.upper(), tok.value)
        hit = structured(text, host, at[0], at[1], tables)
        if hit:
            found.update(occ.cells(*hit))
            continue
        sheet, ref = host, text
        if "!" in text:
            sheet, ref = text.rsplit("!", 1)
            sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
        try:
            c1, r1, c2, r2 = range_boundaries(ref.replace("$", ""))
        except Exception:
            unresolved.append(tok.value)  # external link, a name holding a formula, …
            continue
        if sheet not in occ.cols:
            unresolved.append(tok.value)
            continue
        found.update(occ.cells(sheet, c1, r1, c2, r2))
    return found, unresolved


def verify(xlsx: str | Path, db: str | Path) -> dict:
    t0 = time.perf_counter()
    xlsx, db = Path(xlsx), Path(db)
    xl, wb = read_xlsx(xlsx)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ir = {r["cell_id"]: r for r in con.execute("SELECT cell_id, sheet, row, col, value, formula FROM cells")}
        checks = {}

        # content
        missing = [c for c in xl if c not in ir]
        extra = [c for c in ir if c not in xl]
        wrong = []
        for c, (kind, v) in xl.items():
            r = ir.get(c)
            if r is None:
                continue
            if kind == "formula":
                got, ok = r["formula"], r["formula"] == v
            else:
                got = json.loads(r["value"]) if r["value"] is not None else None
                ok = r["formula"] is None and norm(got) == norm(v)
            if not ok:
                wrong.append({"cell": c, "excel": str(v)[:120], "ir": str(got)[:120]})
        n_f = sum(k == "formula" for k, _ in xl.values())
        checks["content"] = {"cells": len(xl), "formulas": n_f, "matched": len(xl) - len(missing) - len(wrong),
                             "n_missing": len(missing), "n_extra": len(extra), "n_wrong": len(wrong),
                             "missing": missing[:MAX_EXAMPLES], "extra": extra[:MAX_EXAMPLES], "wrong": wrong[:MAX_EXAMPLES],
                             "pass": not missing and not extra and not wrong}

        # rebuild from the IR alone
        out = Workbook()
        out.remove(out.active)
        for s in json.loads(dict(con.execute("SELECT key, value FROM meta").fetchall())["sheets"]):
            out.create_sheet(s)
        for r in ir.values():
            out[r["sheet"]].cell(r["row"], r["col"], r["formula"] if r["formula"] is not None else json.loads(r["value"]))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rebuilt.xlsx"
            out.save(p)
            rebuilt, _ = read_xlsx(p)
        same = sum(1 for c, (k, v) in xl.items() if c in rebuilt and rebuilt[c][0] == k and
                   (rebuilt[c][1] == v if k == "formula" else norm(rebuilt[c][1]) == norm(v)))
        checks["rebuild"] = {"cells": len(xl), "identical": same, "pass": same == len(xl) == len(rebuilt)}

        # references
        names = {n.upper(): d.attr_text for n, d in wb.defined_names.items()}
        tables = excel_tables(wb)
        occ = Occupied(xl)
        deps: dict[str, set] = {}
        for src, dst in con.execute("SELECT src_cell, dst_cell FROM deps"):
            if src in xl:
                deps.setdefault(dst, set()).add(src)
        agree, differ, skipped = 0, [], 0
        for c, (kind, f) in xl.items():
            if kind != "formula":
                continue
            host, row, col = split_id(c)
            mine, unresolved = references(f, host, (row, col), names, tables, occ)
            if unresolved:
                skipped += 1
                continue
            theirs = deps.get(c, set())
            if mine == theirs:
                agree += 1
            else:
                differ.append({"cell": c, "formula": f[:160], "only_tokenizer": sorted(mine - theirs)[:5], "only_ir": sorted(theirs - mine)[:5]})
        checks["references"] = {"formulas": n_f, "agree": agree, "checked": agree + len(differ), "skipped": skipped,
                                "differ": differ[:MAX_EXAMPLES], "pass": not differ}

        # formula groups
        ok, members, bad = 0, 0, []
        for g in con.execute("SELECT group_id, anchor FROM formula_groups WHERE n_cells > 1"):
            anchor = xl.get(g["anchor"], ("value", ""))[1]
            for (m,) in con.execute("SELECT cell_id FROM cells WHERE group_id = ? AND cell_id != ?", (g["group_id"], g["anchor"])):
                members += 1
                moved = Translator(str(anchor), origin=g["anchor"].rsplit("!", 1)[1]).translate_formula(m.rsplit("!", 1)[1])
                actual = ir[m]["formula"] if m in ir else None
                if moved == actual:
                    ok += 1
                else:
                    bad.append({"cell": m, "expected": moved, "actual": actual})
        checks["groups"] = {"groups": con.execute("SELECT COUNT(*) FROM formula_groups").fetchone()[0], "members": members,
                            "ok": ok, "bad": bad[:MAX_EXAMPLES], "pass": ok == members}

        # coverage
        marks = ",".join("?" * len(NOT_UNDERSTOOD))
        rows = con.execute(f"SELECT DISTINCT cell_id, kind, detail FROM issues WHERE kind IN ({marks}) ORDER BY cell_id",
                           NOT_UNDERSTOOD).fetchall()
        not_understood = [{"cell": r["cell_id"], "kind": r["kind"], "detail": r["detail"]} for r in rows]
        formulas_affected = len({r["cell"] for r in not_understood if r["cell"] in xl and xl[r["cell"]][0] == "formula"})
    finally:
        con.close()
    with zipfile.ZipFile(xlsx) as z:
        parts = z.namelist()
    ignored = [{"kind": k, "count": n} for k, rx in IGNORED_PARTS if (n := sum(1 for p in parts if rx.search(p)))]

    passed = all(c["pass"] for c in checks.values())
    status = "check_failed" if not passed else "partial" if not_understood else "verified"
    return {
        "status": status, "checks": checks,
        "coverage": {"formulas": n_f, "understood": n_f - formulas_affected, "n_not_understood": len(not_understood),
                     "not_understood": not_understood[:MAX_NOT_UNDERSTOOD], "ignored": ignored},
        "seconds": round(time.perf_counter() - t0, 2),
    }


def record(db: str | Path, report: dict) -> None:
    """Store the report in the database's meta table (keys parse_status and verification)."""
    con = sqlite3.connect(db)
    try:
        con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        [("parse_status", report["status"]), ("verification", json.dumps(report, ensure_ascii=False, default=str))])
        con.commit()
    finally:
        con.close()
