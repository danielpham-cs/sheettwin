"""Suggest edits that let SheetIR follow every formula, and apply them to a copy of the workbook.

For each formula the check reports as not fully understood (sheetir/verify.py, NOT_UNDERSTOOD), `suggest` says what
it does today and, where it can, proposes a replacement formula:

  dynamic_ref     INDIRECT / OFFSET whose arguments are constants or cells holding constants: the reference they build
                  from the current values (INDIRECT("Quote!"&B5) with B5 = "J5" -> Quote!J5)
  unresolved_name a defined name one or two typos away from an existing one
  unknown_sheet   a sheet name one or two typos away from an existing sheet

Everything else (links to other files, #REF!, structured references to missing tables, …) gets advice only.

`recommend` covers every issue, one at a time, each with the cells a fix would change and their new contents:

  inconsistent_formula, hardcoded_in_formula_column, missing_formula
                     the column's formula, copied from the nearest cell that follows it
  hardcoded_constant the constant moved into an empty input cell (next to the parameters), and every formula of the
                     group pointing at it
  approximate_lookup an exact-match argument added (VLOOKUP / HLOOKUP: FALSE, MATCH: 0)
  orphan             the unused value cleared
  volatile           the formula replaced by its last result (when the file has one)
  not understood     as `suggest`

The user may change any proposed content before applying it. Contents are written as typed in Excel: "=..." is a
formula, "" clears the cell, numbers and TRUE / FALSE are typed, anything else is text.

`apply` writes a new .xlsx in which only the edited cells differ, editing the package's XML in place: re-saving with
openpyxl would drop charts, images and macros. Excel is told to recalculate when it opens the file.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import Token

from .refs import Ref, num_to_col, parse_ref, quote_sheet
from .verify import NOT_UNDERSTOOD, split_id

DYNAMIC = {"INDIRECT", "OFFSET"}
EDITABLE = (".xlsx", ".xlsm")


class Unresolvable(Exception):
    """An argument depends on something other than constants and cell values."""


# ---- formula tokens ----------------------------------------------------------------------------------------------

def _tokens(formula: str) -> list[Token] | None:
    """openpyxl's tokens, or None if joining them back would not give the same formula (then we do not edit it)."""
    try:
        items = Tokenizer(formula).items
    except Exception:  # noqa: BLE001
        return None
    return items if _render(items) == formula else None


def _well_formed(formula: str) -> bool:
    """Tokenizes, and every function or parenthesis it opens is closed."""
    items = _tokens(formula)
    if not items:
        return False
    depth = 0
    for t in items:
        if t.type in (Token.FUNC, Token.PAREN):
            depth += 1 if t.subtype == Token.OPEN else -1
            if depth < 0:
                return False
    return depth == 0


def _render(items: list[Token]) -> str:
    return "=" + "".join(t.value for t in items)


def _fname(tok: Token) -> str:
    name = tok.value[:-1].upper()
    return name.removeprefix("_XLFN.").removeprefix("_XLWS.")


def _call_end(items: list[Token], i: int) -> int:
    """Index of the token closing the function opened at i."""
    depth = 0
    for j in range(i, len(items)):
        t = items[j]
        if t.type in (Token.FUNC, Token.PAREN) and t.subtype == Token.OPEN:
            depth += 1
        elif t.type in (Token.FUNC, Token.PAREN) and t.subtype == Token.CLOSE:
            depth -= 1
            if depth == 0:
                return j
    raise Unresolvable("unbalanced")


def _args(items: list[Token], i: int, end: int) -> list[list[Token]]:
    """The argument token lists of the call spanning items[i..end]."""
    args, cur, depth = [], [], 0
    for t in items[i + 1:end]:
        if t.type in (Token.FUNC, Token.PAREN) and t.subtype == Token.OPEN:
            depth += 1
        elif t.type in (Token.FUNC, Token.PAREN) and t.subtype == Token.CLOSE:
            depth -= 1
        if depth == 0 and t.type == Token.SEP and t.subtype == Token.ARG:
            args.append(cur)
            cur = []
        elif t.type != Token.WSPACE:
            cur.append(t)
    args.append(cur)
    return args


# ---- evaluating constant expressions -----------------------------------------------------------------------------

class _Eval:
    """Evaluate an argument built from literals, cell values, & + - * / and parentheses."""

    def __init__(self, items: list[Token], book: Book, host: str):
        self.items, self.pos, self.book, self.host = items, 0, book, host
        self.inputs: list[str] = []  # cells whose values were read

    def run(self):
        v = self.concat()
        if self.pos != len(self.items):
            raise Unresolvable("unexpected token")
        return v

    def _peek(self, *ops: str) -> str | None:
        if self.pos < len(self.items):
            t = self.items[self.pos]
            if t.type in (Token.OP_IN, Token.OP_PRE) and t.value in ops:
                return t.value
        return None

    def concat(self):
        v = self.additive()
        while self._peek("&"):
            self.pos += 1
            v = _text(v) + _text(self.additive())
        return v

    def additive(self):
        v = self.term()
        while op := self._peek("+", "-"):
            self.pos += 1
            w = self.term()
            v = _num(v) + _num(w) if op == "+" else _num(v) - _num(w)
        return v

    def term(self):
        v = self.unary()
        while op := self._peek("*", "/"):
            self.pos += 1
            w = _num(self.unary())
            if op == "/" and w == 0:
                raise Unresolvable("division by zero")
            v = _num(v) * w if op == "*" else _num(v) / w
        return v

    def unary(self):
        if op := self._peek("-", "+"):
            self.pos += 1
            v = _num(self.unary())
            return -v if op == "-" else v
        return self.primary()

    def primary(self):
        if self.pos >= len(self.items):
            raise Unresolvable("missing operand")
        t = self.items[self.pos]
        self.pos += 1
        if t.type == Token.PAREN and t.subtype == Token.OPEN:
            v = self.concat()
            if self.pos >= len(self.items) or self.items[self.pos].subtype != Token.CLOSE:
                raise Unresolvable("unclosed parenthesis")
            self.pos += 1
            return v
        if t.type != Token.OPERAND:
            raise Unresolvable(t.value)
        if t.subtype == Token.TEXT:
            return t.value[1:-1].replace('""', '"')
        if t.subtype == Token.NUMBER:
            return float(t.value)
        if t.subtype == Token.LOGICAL:
            return t.value.upper() == "TRUE"
        if t.subtype == Token.RANGE:
            ref = self.book.ref(t.value, self.host)
            if ref is None or not ref.is_cell:
                raise Unresolvable(t.value)
            cell = f"{quote_sheet(ref.sheet)}!{num_to_col(ref.c1)}{ref.r1}"
            if cell not in self.book.values:
                raise Unresolvable(f"{cell} has no value")
            self.inputs.append(cell)
            return self.book.values[cell]
        raise Unresolvable(t.value)


def _num(v) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        raise Unresolvable(f"{v!r} is not a number") from None


def _text(v) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _int(v) -> int:
    n = _num(v)
    if not n.is_integer():
        raise Unresolvable(f"{v!r} is not a whole number")
    return int(n)


# ---- the workbook as stored in the IR ----------------------------------------------------------------------------

class Book:
    def __init__(self, db: Path):
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        try:
            self.sheets: list[str] = json.loads(dict(con.execute("SELECT key, value FROM meta").fetchall())["sheets"])
            self.formulas: dict[str, str] = {}
            self.values: dict[str, object] = {}  # constants, and formulas' cached results
            for cell, value, cached, formula in con.execute("SELECT cell_id, value, cached, formula FROM cells"):
                if formula is not None:
                    self.formulas[cell] = formula
                raw = cached if formula is not None else value
                if raw is not None:
                    self.values[cell] = json.loads(raw)
            self.cells = {r[0]: {"sheet": r[1], "row": r[2], "col": r[3], "r1c1": r[4], "table_id": r[5], "group_id": r[6]}
                          for r in con.execute("SELECT cell_id, sheet, row, col, r1c1, table_id, group_id FROM cells")}
            self.fields = {(t, c): key for t, c, key in con.execute("SELECT table_id, col, r1c1 FROM fields")}
            self.params = [c for (c,) in con.execute("SELECT cell_id FROM params")]
            self.literals: dict[str, list[float]] = {}
            for cell, v in con.execute("SELECT cell_id, value FROM literals"):
                self.literals.setdefault(cell, []).append(v)
            self.all_issues = [dict(zip(("cell", "group_id", "kind", "severity", "detail"), r)) for r in con.execute(
                "SELECT cell_id, group_id, kind, severity, detail FROM issues ORDER BY issue_id")]
            has_ignored = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'ignored_issues'").fetchone()
            self.ignored = [dict(zip(("cell", "kind", "severity", "detail", "note", "at"), r)) for r in con.execute(
                "SELECT cell_id, kind, severity, detail, note, ignored_at FROM ignored_issues")] if has_ignored else []
            self.names = [(n, s) for n, s in con.execute("SELECT name, scope FROM names")]
            self.issues: dict[str, list[tuple[str, str]]] = {}
            marks = ",".join("?" * len(NOT_UNDERSTOOD))
            for cell, kind, detail in con.execute(
                    f"SELECT DISTINCT cell_id, kind, detail FROM issues WHERE kind IN ({marks}) ORDER BY cell_id",
                    NOT_UNDERSTOOD):
                self.issues.setdefault(cell, []).append((kind, detail))
        finally:
            con.close()

    def sheet(self, name: str) -> str | None:
        return next((s for s in self.sheets if s.lower() == name.lower()), None)

    def ref(self, text: str, host: str) -> Ref | None:
        """A1 text as an absolute reference on an existing sheet (host when unqualified), or None."""
        ref = parse_ref(text)
        if ref is None or ref.r1 is None or ref.c1 is None:
            return None
        sheet = host if ref.sheet is None else self.sheet(ref.sheet)
        return None if sheet is None else ref.with_sheet(sheet)


# ---- fixers ------------------------------------------------------------------------------------------------------

def _resolve_dynamic(items: list[Token], i: int, book: Book, host: str) -> tuple[int, str, list[str]]:
    """The INDIRECT / OFFSET call opened at i as a static reference: (index of its closing token, A1 text, inputs)."""
    end = _call_end(items, i)
    args = _args(items, i, end)
    name = _fname(items[i])
    if any(_fname(t) in DYNAMIC for a in args for t in a if t.type == Token.FUNC and t.subtype == Token.OPEN):
        raise Unresolvable("nested")
    if name == "INDIRECT":
        ev = _Eval(args[0], book, host)
        text = _text(ev.run())
        if len(args) > 1 and args[1]:
            a1 = _Eval(args[1], book, host)
            style = a1.run()
            ev.inputs += a1.inputs
            if not (style is True or (not isinstance(style, str) and _num(style) != 0)):
                raise Unresolvable("R1C1-style INDIRECT")
        ref = book.ref(text, host)
        if ref is None:
            raise Unresolvable(f"{text!r} is not a reference")
        return end, ref.a1(host), ev.inputs
    # OFFSET(reference, rows, cols, [height], [width])
    if len(args) < 3 or len(args[0]) != 1 or args[0][0].subtype != Token.RANGE:
        raise Unresolvable("OFFSET base is not a plain reference")
    base = book.ref(args[0][0].value, host)
    if base is None:
        raise Unresolvable("OFFSET base")
    inputs: list[str] = []

    def num(arg, default):
        if not arg:
            return default
        ev = _Eval(arg, book, host)
        v = _int(ev.run())
        inputs.extend(ev.inputs)
        return v

    dr, dc = num(args[1], 0), num(args[2], 0)
    h = num(args[3] if len(args) > 3 else [], base.r2 - base.r1 + 1)
    w = num(args[4] if len(args) > 4 else [], base.c2 - base.c1 + 1)
    r1, c1 = base.r1 + dr, base.c1 + dc
    if h < 1 or w < 1 or r1 < 1 or c1 < 1:
        raise Unresolvable("OFFSET leaves the sheet")
    return end, Ref(base.sheet, r1, c1, r1 + h - 1, c1 + w - 1).a1(host), inputs


def _fix_dynamic(formula: str, book: Book, host: str) -> tuple[str, list[dict], list[str]]:
    """Replace every INDIRECT / OFFSET by the reference it builds now, innermost first."""
    targets, inputs = [], []
    for _ in range(20):
        items = _tokens(formula)
        if items is None:
            raise Unresolvable("formula cannot be rewritten")
        calls = [i for i, t in enumerate(items) if t.type == Token.FUNC and t.subtype == Token.OPEN and _fname(t) in DYNAMIC]
        if not calls:
            return formula, targets, inputs
        i = calls[-1]  # the last one opened has no dynamic call inside
        end, a1, used = _resolve_dynamic(items, i, book, host)
        targets.append({"function": _fname(items[i]), "target": a1})
        inputs += [c for c in used if c not in inputs]
        items[i:end + 1] = [Token(a1, Token.OPERAND, Token.RANGE)]
        formula = _render(items)
    raise Unresolvable("too many dynamic references")


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def closest(word: str, candidates: list[str]) -> str | None:
    """The candidate a typo away (one edit for short words, two for longer), if exactly one is nearest."""
    reach = 2 if len(word) >= 6 else 1
    scored = sorted((d, c) for c in candidates if 0 < (d := _distance(word.lower(), c.lower())) <= reach)
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


def _replace_operand(formula: str, old: str, new: str) -> str:
    items = _tokens(formula)
    if items is None:
        raise Unresolvable("formula cannot be rewritten")
    hit = False
    for t in items:
        if t.type == Token.OPERAND and t.subtype == Token.RANGE and t.value == old:
            t.value, hit = new, True
    if not hit:
        raise Unresolvable(f"{old} not found")
    return _render(items)


def _suggest_cell(cell: str, issues: list[tuple[str, str]], book: Book) -> dict:
    host = split_id(cell)[0]
    formula = book.formulas.get(cell)
    out = {"cell": cell, "formula": formula, "issues": [{"kind": k, "detail": d} for k, d in issues],
           "fix": None, "changes": [], "inputs": [], "advice": []}
    if formula is None:
        out["advice"] = sorted({k for k, _ in issues})
        return out
    new = formula
    for kind in dict.fromkeys(k for k, _ in issues):
        details = [d for k, d in issues if k == kind]
        try:
            if kind == "dynamic_ref":
                new, targets, inputs = _fix_dynamic(new, book, host)
                out["changes"] += [{"kind": kind, **t} for t in targets]
                out["inputs"] = [{"cell": c, "value": book.values[c]} for c in inputs]
            elif kind == "unresolved_name":
                names = [n for n, scope in book.names if scope in (None, host)]
                for d in details:
                    good = closest(d, names)
                    if good is None:
                        raise Unresolvable(d)
                    new = _replace_operand(new, d, good)
                    out["changes"].append({"kind": kind, "old": d, "new": good})
            elif kind == "unknown_sheet":
                for d in details:
                    sheet, rest = d.rsplit("!", 1)
                    bare = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
                    good = closest(bare, book.sheets)
                    if good is None:
                        raise Unresolvable(d)
                    new = _replace_operand(new, d, f"{quote_sheet(good)}!{rest}")
                    out["changes"].append({"kind": kind, "old": bare, "new": good})
            else:
                raise Unresolvable(kind)
        except Unresolvable:
            out["advice"].append(kind)
    if not out["advice"] and new != formula:
        out["fix"] = new
    return out


def suggest(db: str | Path, src: str | Path | None = None) -> dict:
    """Fix suggestions for every not-understood formula; `src` (the stored workbook) says whether they can be applied."""
    book = Book(Path(db))
    fixes = [_suggest_cell(cell, issues, book) for cell, issues in book.issues.items()]
    editable = src is not None and Path(src).suffix.lower() in EDITABLE
    if editable:
        blocked = _blocked(Path(src), [f["cell"] for f in fixes if f["fix"]])
        for f in fixes:
            if f["fix"] and f["cell"] in blocked:
                f["blocked"] = blocked[f["cell"]]
    return {"can_apply": editable, "fixes": fixes}


# ---- one issue at a time ----------------------------------------------------------------------------------------

def content(book: Book, cell: str) -> str:
    """A cell's contents as typed in Excel."""
    if cell in book.formulas:
        return book.formulas[cell]
    if cell not in book.values:
        return ""
    v = book.values[cell]
    return _text(v) if isinstance(v, (bool, float, int)) else str(v)


def _a1(cell: str) -> str:
    return cell.rsplit("!", 1)[1]


def _follow_column(book: Book, cell: str) -> dict | None:
    """The column's formula at `cell`, translated from the nearest cell of the same table column that follows it."""
    from openpyxl.formula.translate import Translator

    me = book.cells.get(cell)
    key = me and book.fields.get((me["table_id"], me["col"]))
    if not key:
        return None
    donors = [c for c, m in book.cells.items() if m["table_id"] == me["table_id"] and m["col"] == me["col"]
              and m["r1c1"] == key and c in book.formulas and c != cell]
    if not donors:
        return None
    donor = min(donors, key=lambda c: abs(book.cells[c]["row"] - me["row"]))
    formula = Translator(book.formulas[donor], origin=_a1(donor)).translate_formula(_a1(cell))
    return {"type": "follow_column", "params": {"donor": donor}, "edits": {cell: formula}}


def _input_cell(book: Book, near: str) -> tuple[str, str | None]:
    """An empty cell for a new input: below the parameters (the column most of them are in), else beside `near`'s
    data. Returns it and the empty cell to its left, for a label (None when that is taken)."""
    def taken(sheet, row, col):
        return f"{quote_sheet(sheet)}!{num_to_col(col)}{row}" in book.cells

    spots = [(book.cells[c]["sheet"], book.cells[c]["col"]) for c in book.params if c in book.cells]
    if spots:
        sheet, col = max(set(spots), key=spots.count)
        row = max(book.cells[c]["row"] for c in book.params if (book.cells[c]["sheet"], book.cells[c]["col"]) == (sheet, col)) + 1
    else:
        sheet = book.cells[near]["sheet"]
        col = max(m["col"] for m in book.cells.values() if m["sheet"] == sheet) + 2
        row = 1
    while taken(sheet, row, col) or (col > 1 and taken(sheet, row, col - 1) and not taken(sheet, row - 1, col - 1)):
        row += 1
    target = f"{quote_sheet(sheet)}!{num_to_col(col)}{row}"
    label = f"{quote_sheet(sheet)}!{num_to_col(col - 1)}{row}" if col > 1 and not taken(sheet, row, col - 1) else None
    return target, label


def _extract_constant(book: Book, issue: dict) -> dict | None:
    m = re.match(r"literal (\S+) in ", issue["detail"] or "")
    anchor = issue["cell"]
    value = next((v for v in book.literals.get(anchor, []) if m and f"{v:g}" == m.group(1)), None)
    if value is None:
        return None
    # Every formula with the anchor's pattern, not just its group: a cell that breaks a column splits it into groups
    # with the same pattern, and changing only one of them would make the others inconsistent.
    pattern = book.cells[anchor]["r1c1"]
    members = [c for c, x in book.cells.items() if x["r1c1"] == pattern and c in book.formulas] or [anchor]
    target, label = _input_cell(book, anchor)
    t_sheet, t_a1 = split_id(target)[0], _a1(target)
    absolute = re.sub(r"([A-Z]+)(\d+)", r"$\1$\2", t_a1)
    edits = {target: _text(value)}
    for c in members:
        items = _tokens(book.formulas[c])
        if items is None:
            return None
        ref = absolute if split_id(c)[0] == t_sheet else f"{quote_sheet(t_sheet)}!{absolute}"
        hit = False
        for tok in items:
            if tok.type == Token.OPERAND and tok.subtype == Token.NUMBER and float(tok.value) == value:
                tok.value, hit = ref, True
        if not hit:
            return None
        edits[c] = _render(items)
    return {"type": "extract_constant", "params": {"value": _text(value), "target": target, "label_cell": label,
                                                   "cells": len(members)}, "edits": edits}


EXACT = {"VLOOKUP": (4, "FALSE"), "HLOOKUP": (4, "FALSE"), "MATCH": (3, "0")}


def _exact_match(book: Book, issue: dict) -> dict | None:
    gid = issue["group_id"]
    members = [c for c, x in book.cells.items() if gid is not None and x["group_id"] == gid] or [issue["cell"]]
    edits = {}
    for c in members:
        items = _tokens(book.formulas.get(c, ""))
        if items is None:
            return None
        changed = False
        for i in reversed(range(len(items))):
            t = items[i]
            if not (t.type == Token.FUNC and t.subtype == Token.OPEN and _fname(t) in EXACT):
                continue
            n, exact = EXACT[_fname(t)]
            end = _call_end(items, i)
            args = _args(items, i, end)
            if len(args) == n - 1:
                items[end:end] = [Token(",", Token.SEP, Token.ARG), Token(exact, Token.OPERAND, Token.LOGICAL if exact == "FALSE" else Token.NUMBER)]
                changed = True
            elif len(args) == n and len(args[-1]) == 1 and args[-1][0].value.upper() in ("TRUE", "1"):
                args[-1][0].value = exact
                changed = True
        if changed:
            edits[c] = _render(items)
    return {"type": "exact_match", "params": {"cells": len(edits)}, "edits": edits} if edits else None


def _recommend_one(book: Book, issue: dict, understood: dict[str, dict]) -> dict | None:
    kind, cell = issue["kind"], issue["cell"]
    try:
        if kind in NOT_UNDERSTOOD:
            f = understood.get(cell)
            if f and f["fix"]:
                return {"type": "rewrite", "params": {"changes": f["changes"], "inputs": f["inputs"]}, "edits": {cell: f["fix"]}}
            return None
        if kind in ("inconsistent_formula", "hardcoded_in_formula_column", "missing_formula"):
            return _follow_column(book, cell)
        if kind == "hardcoded_constant":
            return _extract_constant(book, issue)
        if kind == "approximate_lookup":
            return _exact_match(book, issue)
        if kind == "orphan":
            return {"type": "clear", "params": {}, "edits": {cell: ""}}
        if kind == "volatile" and cell in book.values:
            return {"type": "fixed_value", "params": {"value": content(book, cell) if cell not in book.formulas
                                                       else _text(book.values[cell])},
                    "edits": {cell: _text(book.values[cell])}}
    except Unresolvable:
        return None
    return None


def recommend(db: str | Path, src: str | Path | None = None) -> dict:
    """Every issue with a recommended fix where there is one (see the module docstring), the current contents of the
    cells involved, and the issues the user ignored."""
    book = Book(Path(db))
    understood = {cell: _suggest_cell(cell, issues, book) for cell, issues in book.issues.items()}
    editable = src is not None and Path(src).suffix.lower() in EDITABLE
    out = []
    for issue in book.all_issues:
        if issue["cell"] is None:
            continue
        rec = _recommend_one(book, issue, understood)
        cells = list(dict.fromkeys([issue["cell"], *(rec["edits"] if rec else [])]))
        entry = {**issue, "recommendation": rec, "current": {c: content(book, c) for c in cells},
                 "ignorable": issue["kind"] not in NOT_UNDERSTOOD}
        if rec and editable:
            blocked = _blocked(Path(src), [c for c in rec["edits"] if c in book.cells])
            if blocked:
                entry["blocked"] = blocked
        out.append(entry)
    return {"can_apply": editable, "issues": out, "ignored": book.ignored}


# ---- editing the .xlsx package -----------------------------------------------------------------------------------

def _sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> path of its XML part inside the package."""
    wb = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {}
    for m in re.finditer(r"<Relationship\b[^>]*>", rels):
        tag = m.group(0)
        rid, target = re.search(r'\bId="([^"]+)"', tag), re.search(r'\bTarget="([^"]+)"', tag)
        if rid and target:
            t = target.group(1)
            targets[rid.group(1)] = t.lstrip("/") if t.startswith("/") else "xl/" + t
    out = {}
    for m in re.finditer(r"<(?:\w+:)?sheet\b[^>]*>", wb):
        tag = m.group(0)
        name = re.search(r'\bname="([^"]*)"', tag)
        rid = re.search(r'\b\w+:id="([^"]+)"', tag)
        if name and rid and rid.group(1) in targets:
            out[_unescape(name.group(1))] = targets[rid.group(1)]
    return out


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def _cell_re(a1: str) -> re.Pattern:
    return re.compile(r'<c\b(?P<attrs>(?=[^>]*\br="' + a1 + r'")[^>]*?)(?:/>|>(?P<body>.*?)</c>)', re.S)


_F_RE = re.compile(r"<f\b(?P<attrs>[^>]*?)(?:/>|>(?P<text>.*?)</f>)", re.S)


def _formula_block(xml: str, a1: str) -> tuple[re.Match, re.Match] | str:
    """The cell's element and its <f> element, or why it cannot be edited."""
    c = _cell_re(a1).search(xml)
    if c is None or not c.group("body"):
        return "not_found"
    f = _F_RE.search(c.group("body"))
    if f is None:
        return "not_found"
    return _shared_or_array(f.group("attrs")) or (c, f)


def _shared_or_array(attrs: str) -> str | None:
    if re.search(r'\bt="array"', attrs) or re.search(r'\bt="dataTable"', attrs):
        return "array_formula"
    if re.search(r'\bt="shared"', attrs) and re.search(r'\bref="', attrs):
        return "shared_formula"  # other cells copy this one's text
    return None


def _blocked(src: Path, cells: list[str]) -> dict[str, str]:
    """Cells among `cells` whose element cannot be rewritten: the anchor of a shared formula, an array formula."""
    out = {}
    with zipfile.ZipFile(src) as z:
        parts = _sheet_parts(z)
        cache: dict[str, str] = {}
        for cell in cells:
            sheet, row, col = split_id(cell)
            part = parts.get(sheet)
            if part is None:
                out[cell] = "not_found"
                continue
            xml = cache.setdefault(part, z.read(part).decode("utf-8"))
            c = _cell_re(f"{num_to_col(col)}{row}").search(xml)
            f = c and c.group("body") and _F_RE.search(c.group("body"))
            why = f and _shared_or_array(f.group("attrs"))
            if why:
                out[cell] = why
    return out


def _recalc_on_load(wb: str) -> str:
    """Ask Excel to recalculate everything when it opens the workbook (cached results of edited cells are stale)."""
    calc = re.search(r"<calcPr\b[^>]*?/?>", wb)
    if calc:
        tag = calc.group(0)
        if "fullCalcOnLoad=" in tag:
            new = re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', tag)
        else:
            new = tag[:7] + ' fullCalcOnLoad="1"' + tag[7:]  # after "<calcPr"
        return wb[:calc.start()] + new + wb[calc.end():]
    # calcPr comes right after these elements in the schema's order
    ends = [m.end() for m in re.finditer(r"</(?:sheets|functionGroups|externalReferences|definedNames)>|"
                                         r"<(?:functionGroups|externalReferences|definedNames)\b[^>]*/>", wb)]
    if not ends:
        return wb
    at = max(ends)
    return wb[:at] + '<calcPr fullCalcOnLoad="1"/>' + wb[at:]


def _cell_xml(a1: str, style: str, text: str) -> str:
    """A <c> element holding `text` as typed in Excel."""
    head = f'<c r="{a1}"{style}'
    if text.startswith("=") and len(text) > 1:
        if not _well_formed(text):
            raise ValueError(f"{a1}: {text} is not a formula Excel can read")
        return f"{head}><f>{escape(text[1:])}</f></c>"
    if text == "":
        return f"{head}/>"
    if text.upper() in ("TRUE", "FALSE"):
        return f'{head} t="b"><v>{int(text.upper() == "TRUE")}</v></c>'
    try:
        n = float(text)
    except ValueError:
        n = None
    if n is not None and text.strip().lower() not in ("nan", "inf", "-inf", "infinity", "-infinity"):
        return f"{head}><v>{text.strip()}</v></c>"
    return f'{head} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _set_cell(xml: str, row: int, col: int, text: str) -> str:
    a1 = f"{num_to_col(col)}{row}"
    c = _cell_re(a1).search(xml)
    if c is not None:
        body = c.group("body") or ""
        f = _F_RE.search(body)
        why = f and _shared_or_array(f.group("attrs"))
        if why:
            raise ValueError(f"{a1}: {why}")
        style = re.search(r'\ss="[^"]*"', c.group("attrs"))
        return xml[:c.start()] + _cell_xml(a1, style.group(0) if style else "", text) + xml[c.end():]
    if text == "":
        return xml  # already empty
    new = _cell_xml(a1, "", text)
    r = re.search(r'<row\b(?=[^>]*\br="' + str(row) + r'")[^>]*?(?P<close>/?)>', xml)
    if r is not None:
        if r.group("close"):  # <row .../> -> <row ...>cell</row>
            return xml[:r.start()] + r.group(0)[:-2] + ">" + new + "</row>" + xml[r.end():]
        end = xml.index("</row>", r.end())
        at = end
        for m in re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', xml[r.end():end]):
            if _col(m.group(1)) > col:
                at = r.end() + m.start()
                break
        return xml[:at] + new + xml[at:]
    row_xml = f'<row r="{row}">{new}</row>'
    if re.search(r"<sheetData\s*/>", xml):
        return re.sub(r"<sheetData\s*/>", f"<sheetData>{row_xml}</sheetData>", xml, count=1)
    at = xml.index("</sheetData>")
    for m in re.finditer(r'<row\b[^>]*\br="(\d+)"', xml):
        if int(m.group(1)) > row:
            at = m.start()
            break
    return xml[:at] + row_xml + xml[at:]


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _grow_dimension(xml: str, row: int, col: int) -> str:
    m = re.search(r'<dimension ref="([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?"', xml)
    if not m:
        return xml
    c1, r1 = _col(m.group(1)), int(m.group(2))
    c2, r2 = (_col(m.group(3)), int(m.group(4))) if m.group(3) else (c1, r1)
    c1, r1, c2, r2 = min(c1, col), min(r1, row), max(c2, col), max(r2, row)
    return xml[:m.start(1)] + f"{num_to_col(c1)}{r1}:{num_to_col(c2)}{r2}" + xml[m.end() - 1:]  # keep the closing quote


def apply(src: str | Path, edits: dict[str, str]) -> bytes:
    """A copy of the .xlsx at `src` with each cell in `edits` (cell id -> contents as typed in Excel) set."""
    src = Path(src)
    with zipfile.ZipFile(src) as z:
        parts = _sheet_parts(z)
        by_part: dict[str, list[tuple[int, int, str]]] = {}
        for cell, text in edits.items():
            sheet, row, col = split_id(cell)
            if sheet not in parts:
                raise ValueError(f"{cell}: sheet not found in the package")
            by_part.setdefault(parts[sheet], []).append((row, col, text))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in z.infolist():
                data = z.read(info.filename)
                if info.filename in by_part:
                    xml = data.decode("utf-8")
                    for row, col, text in by_part[info.filename]:
                        xml = _set_cell(xml, row, col, text)
                        if text != "":
                            xml = _grow_dimension(xml, row, col)
                    data = xml.encode("utf-8")
                elif info.filename == "xl/workbook.xml":
                    data = _recalc_on_load(data.decode("utf-8")).encode("utf-8")
                zout.writestr(info, data)
    return out.getvalue()
