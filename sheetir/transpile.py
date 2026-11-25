"""Translate formula groups into Python: one function per group, run at every member's position.

A group (sheetir.groups) is a run of cells with the same R1C1 formula, so one translation serves all of them: the
anchor's formula is parsed into an expression tree, references are kept relative to the host cell the way R1C1 has
them, and the tree is written out as a Python function `g<id>(m, row, col)` over the runtime in sheetir.xl.

    =C2*Inputs!$B$3+D2*Inputs!$B$4          (Quote!F2:F21)
    def g3(m, row, col):
        return xl.add(xl.mul(m.c('Quote', row, col - 3), m.c('Inputs', 3, 2)), xl.mul(...))

A group that uses something the twin cannot evaluate faithfully (a function outside sheetir.xl.FUNCTIONS, a volatile
function, INDIRECT / OFFSET, a link to another file, a structured reference, an array formula) is not translated: its
cells keep the values Excel saved ("keep baseline"), and the reason is recorded.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl.formula.tokenizer import Token, Tokenizer

from . import __version__, xl
from .refs import Ref, RelRef, parse_ref

# ---------------------------------------------------------------- values stored in the IR ---------------------------

_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2}(\.\d+)?)?$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")


def decode(text: str | None, vtype: str | None = None):
    """A cell value as SheetIR stored it (JSON; dates as their text) back to what xl computes with."""
    if text is None:
        return None
    v = json.loads(text)
    if isinstance(v, str):
        if v in xl.ERRORS:
            return xl.XLError(v)
        if _DATETIME.match(v) and vtype in (None, "date", "formula"):
            return xl.date_serial(dt.datetime.fromisoformat(v))
        if _TIME.match(v) and vtype in (None, "date", "formula"):
            return xl.date_serial(dt.time.fromisoformat(v))
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


# ---------------------------------------------------------------- parsing -------------------------------------------

class Untranslatable(Exception):
    """The formula uses something the twin does not evaluate. `reason` is the kind (what coverage counts), `detail`
    the instance (the reference, name or token)."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(reason if detail is None else f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


@dataclass
class Node:
    kind: str  # num | str | bool | err | missing | ref | call | bin | neg | pct | array
    value: object = None
    args: list = field(default_factory=list)


_BIN = {"+": ("add", 3), "-": ("sub", 3), "*": ("mul", 4), "/": ("div", 4), "^": ("power", 5), "&": ("concat", 2),
        "=": ("eq", 1), "<>": ("ne", 1), "<": ("lt", 1), "<=": ("le", 1), ">": ("gt", 1), ">=": ("ge", 1)}
_PREFIX_BP = 7  # Excel: negation binds tighter than ^ (-2^2 = 4)
_POSTFIX_BP = 6


class _Names:
    """Defined names from the IR: a name is a cell or range (absolute reference), or a number constant."""

    def __init__(self, rows: list[tuple[str, str | None, str]], sheets: list[str]):
        self.sheets = {s.lower(): s for s in sheets}
        self.names = {(scope, name.upper()): text for name, scope, text in rows}
        self.used: set[str] = set()

    def sheet(self, name: str) -> str | None:
        return self.sheets.get(name.lower())

    def resolve(self, name: str, host: str) -> Node:
        if (None, name.upper()) in self.names and self.names[(None, name.upper())] is None:
            # a workbook-level name with no readable definition: which one Excel used cannot be known from the file
            raise Untranslatable("a defined name that is not a plain cell or range", f"{name} (unreadable)")
        text = self.names.get((host, name.upper()))
        if text is None:
            text = self.names.get((None, name.upper()))
        if text is None:
            raise Untranslatable("unknown name", name)
        self.used.add(name.upper())
        t = text.strip().lstrip("=")
        try:
            return Node("num", float(t))
        except ValueError:
            pass
        ref = parse_ref(t)
        if ref is None or ref.sheet is None or self.sheet(ref.sheet) is None or "," in t:
            raise Untranslatable("a defined name that is not a plain cell or range", f"{name} = {text}")
        ref = ref.with_sheet(self.sheet(ref.sheet))
        return Node("ref", _absolute(ref))


def _absolute(ref: Ref) -> RelRef:
    """A reference that stays put wherever the formula is (a defined name always does)."""
    return RelRef(ref.sheet, ref.r1, ref.c1, ref.r2, ref.c2, True, True, True, True, ref.is_cell)


class Parser:
    def __init__(self, formula: str, host: str, row: int, col: int, names: _Names):
        self.host, self.row, self.col, self.names = host, row, col, names
        try:
            toks = Tokenizer(formula).items
        except Exception as exc:  # noqa: BLE001
            raise Untranslatable("formula cannot be parsed", str(exc)) from exc
        self.toks = [t for t in toks if t.type != Token.WSPACE]
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self) -> Node:
        node = self.expr(0)
        if self.peek() is not None:
            raise Untranslatable("formula cannot be parsed", f"unexpected {self.peek().value!r}")
        return node

    def expr(self, min_bp: int) -> Node:
        left = self.prefix()
        while True:
            t = self.peek()
            if t is None:
                break
            if t.type == Token.OP_POST and t.value == "%":
                if _POSTFIX_BP < min_bp:
                    break
                self.next()
                left = Node("pct", args=[left])
                continue
            if t.type != Token.OP_IN:
                break
            if t.value not in _BIN:
                raise Untranslatable("range or intersection operator", t.value)
            name, bp = _BIN[t.value]
            if bp < min_bp:
                break
            self.next()
            right = self.expr(bp + 1)  # all Excel binary operators are left-associative
            left = Node("bin", name, [left, right])
        return left

    def prefix(self) -> Node:
        t = self.next()
        if t is None:
            raise Untranslatable("formula cannot be parsed", "it ends early")
        if t.type == Token.OP_PRE:
            operand = self.expr(_PREFIX_BP)
            return Node("neg", args=[operand]) if t.value == "-" else operand
        if t.type == Token.PAREN and t.subtype == Token.OPEN:
            node = self.expr(0)
            close = self.next()
            if close is None or close.type != Token.PAREN:
                raise Untranslatable("formula cannot be parsed", "unbalanced parenthesis")
            return node
        if t.type == Token.FUNC and t.subtype == Token.OPEN:
            return self.call(t.value[:-1])
        if t.type == Token.ARRAY and t.subtype == Token.OPEN:
            return self.array()
        if t.type == Token.OPERAND:
            return self.operand(t)
        raise Untranslatable("formula cannot be parsed", f"unexpected {t.value!r}")

    def call(self, raw: str) -> Node:
        name = raw.upper()
        for prefix in ("_XLFN._XLWS.", "_XLFN.", "_XLWS."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        args: list[Node] = []
        t = self.peek()
        if t is not None and t.type == Token.FUNC and t.subtype == Token.CLOSE:
            self.next()
            return Node("call", name, args)
        while True:
            t = self.peek()
            if t is not None and ((t.type == Token.SEP and t.subtype == Token.ARG)
                                  or (t.type == Token.FUNC and t.subtype == Token.CLOSE)):
                args.append(Node("missing"))
            else:
                args.append(self.expr(0))
            t = self.next()
            if t is None:
                raise Untranslatable("formula cannot be parsed", f"{name}( is not closed")
            if t.type == Token.FUNC and t.subtype == Token.CLOSE:
                return Node("call", name, args)
            if not (t.type == Token.SEP and t.subtype == Token.ARG):
                raise Untranslatable("formula cannot be parsed", f"unexpected {t.value!r} in {name}(")

    def array(self) -> Node:
        rows, row = [], []
        while True:
            t = self.next()
            if t is None:
                raise Untranslatable("formula cannot be parsed", "array constant is not closed")
            if t.type == Token.ARRAY and t.subtype == Token.CLOSE:
                rows.append(row)
                break
            if t.type == Token.SEP:
                if t.subtype == Token.ROW:
                    rows.append(row)
                    row = []
                continue
            if t.type == Token.OP_PRE and t.value == "-":
                n = self.next()
                row.append(-float(n.value))
                continue
            node = self.operand(t)
            if node.kind not in ("num", "str", "bool", "err"):
                raise Untranslatable("array constant with references")
            row.append(node.value)
        return Node("array", rows)

    def operand(self, t) -> Node:
        v = t.value
        if t.subtype == Token.NUMBER:
            return Node("num", float(v))
        if t.subtype == Token.TEXT:
            return Node("str", v[1:-1].replace('""', '"'))
        if t.subtype == Token.LOGICAL:
            return Node("bool", v.upper() == "TRUE")
        if t.subtype == Token.ERROR:
            return Node("err", v.upper())
        # RANGE: a reference, a name, a structured reference or an external link
        if v.startswith("[") or ("[" in v and "!" in v and v.index("[") < v.index("!")):
            raise Untranslatable("link to another workbook", v)
        if "#REF!" in v.upper():
            return Node("err", "#REF!")
        ref = parse_ref(v)
        if ref is not None:
            target = self.host if ref.sheet is None else self.names.sheet(ref.sheet)
            if target is None:
                raise Untranslatable("reference to a sheet that does not exist", v)
            return Node("ref", ref.with_sheet(target).relativize(self.row, self.col))
        if "[" in v:
            raise Untranslatable("structured reference (Table[Column])", v)
        if v.upper() in ("TRUE", "FALSE"):
            return Node("bool", v.upper() == "TRUE")
        return self.names.resolve(v, self.host)


# ---------------------------------------------------------------- code generation ----------------------------------

def _coord(v, absolute: bool, var: str) -> str:
    if v is None:
        return "None"
    if absolute:
        return str(v)
    return var if v == 0 else f"{var} {'+' if v > 0 else '-'} {abs(v)}"


def _ref_code(rel: RelRef) -> str:
    sheet = repr(rel.sheet)
    if rel.single:
        return f"m.c({sheet}, {_coord(rel.r1, rel.ar1, 'row')}, {_coord(rel.c1, rel.ac1, 'col')})"
    return (f"m.r({sheet}, {_coord(rel.r1, rel.ar1, 'row')}, {_coord(rel.c1, rel.ac1, 'col')}, "
            f"{_coord(rel.r2, rel.ar2, 'row')}, {_coord(rel.c2, rel.ac2, 'col')})")


# Argument positions where a function takes a range as a range; anywhere else a multi-cell reference is reduced to
# the cell in the host's row or column (Excel's implicit intersection), and "all" means every argument.
RANGE_ARGS: dict[str, object] = {
    **{f: "all" for f in ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "PRODUCT", "MEDIAN", "STDEV", "STDEVP",
                          "SUMSQ", "AND", "OR", "SUMPRODUCT", "SUMIFS", "COUNTIFS", "AVERAGEIFS", "MAXA", "MINA",
                          "AVERAGEA", "VAR", "VARP")},
    "SUBTOTAL": "rest", "PERCENTILE": (0,), "QUARTILE": (0,), "RANK": (1,), "WORKDAY": (2,), "NETWORKDAYS": (2,),
    "NPV": "rest", "SUMIF": (0, 2), "COUNTIF": (0,), "AVERAGEIF": (0, 2), "VLOOKUP": (1,), "HLOOKUP": (1,),
    "MATCH": (1,), "LOOKUP": (1, 2), "INDEX": (0,), "ROWS": (0,), "COLUMNS": (0,), "LARGE": (0,), "SMALL": (0,),
    "IRR": (0,), "XNPV": (1, 2), "XIRR": (0, 1), "COUNTBLANK": (0,), "IFERROR": (0, 1), "IFNA": (0, 1),
    "IF": (1, 2), "CHOOSE": "rest",
}
ARRAY_FUNCTIONS = {"SUMPRODUCT"}  # their arguments are evaluated as arrays: (A1:A9="x")*B1:B9 works element-wise


def _takes_range(name: str, i: int) -> bool:
    spec = RANGE_ARGS.get(name)
    return spec == "all" or (spec == "rest" and i >= 1) or (isinstance(spec, tuple) and i in spec)


def _py(node: Node, used: set[str], array: bool = False, keep_range: bool = False) -> str:
    """Python for `node`. `array`: inside an array-evaluating function (ranges stay ranges in operators).
    `keep_range`: this position takes a range as is; otherwise a multi-cell reference is implicitly intersected."""
    k = node.kind
    if k == "num":
        return repr(float(node.value))
    if k == "str":
        return repr(node.value)
    if k == "bool":
        return repr(bool(node.value))
    if k == "err":
        return f"xl.XLError({node.value!r})"
    if k == "missing":
        return "None"
    if k == "array":
        rows = ", ".join("[" + ", ".join(_lit(v) for v in row) + "]" for row in node.value)
        return f"xl.Range([{rows}])"
    if k == "ref":
        code = _ref_code(node.value)
        if node.value.single or keep_range or array:
            return code
        return f"xl.scalar({code}, row, col)"
    if k == "neg":
        return f"xl.neg({_py(node.args[0], used, array)})"
    if k == "pct":
        return f"xl.percent({_py(node.args[0], used, array)})"
    if k == "bin":
        a, b = (_py(x, used, array) for x in node.args)
        return f"xl.{node.value}({a}, {b})"
    if k == "call":
        name = node.value
        if name in xl.VOLATILE:
            raise Untranslatable(f"{name}: its result changes on every recalculation or cannot be traced")
        fn = xl.FUNCTIONS.get(name)
        if fn is None:
            raise Untranslatable(f"function {name} is not supported yet")
        used.add(name)
        lazy = xl.LAZY.get(name, ())
        parts = []
        inner = array or name in ARRAY_FUNCTIONS
        for i, a in enumerate(node.args):
            code = _py(a, used, inner, keep_range=_takes_range(name, i))
            if lazy == "rest" and i >= 1 or (lazy != "rest" and i in lazy):
                code = f"lambda: {code}"
            parts.append(code)
        attr = {"NA": "NA_", "VALUE": "VALUE_"}.get(name, name)
        return f"xl.{attr}({', '.join(parts)})"
    raise Untranslatable("formula cannot be parsed", f"node {k}")


def translate_formula(formula: str, sheet: str, row: int, col: int, names: _Names) -> tuple[str, set[str]]:
    """Python expression (over m, row, col) for `formula` as written at (sheet, row, col), and the functions it uses."""
    used: set[str] = set()
    tree = Parser(formula, sheet, row, col, names).parse()
    return _py(tree, used), used


# ---------------------------------------------------------------- whole workbook -----------------------------------

@dataclass
class GroupCode:
    group_id: int
    sheet: str
    anchor: str
    last: str
    n_cells: int
    r1c1: str
    status: str  # translated | untranslated
    reason: str | None  # why it is not translated (the kind)
    detail: str | None  # the instance: which reference, name or token
    expr: str | None
    functions: list[str]
    members: list[tuple[int, int]]
    names: list[str] = field(default_factory=list)  # defined names the formula uses

    @property
    def source(self) -> str | None:
        if self.expr is None:
            return None
        span = self.anchor if self.n_cells == 1 else f"{self.anchor}:{self.last.split('!')[-1]}"
        return (f"def g{self.group_id}(m, row, col):\n"
                f"    # {span} ({self.n_cells} cell{'s' if self.n_cells != 1 else ''})  {self.r1c1}\n"
                f"    return {self.expr}\n")


@dataclass
class Twin:
    """Everything needed to compute a workbook without Excel: its constants, translated groups, and baselines."""

    source: str
    sheets: list[str]
    constants: dict  # (sheet, row, col) -> value
    baseline: dict  # (sheet, row, col) -> Excel's saved value of each formula cell (None if never calculated)
    group_of: dict  # (sheet, row, col) -> group_id
    groups: dict[int, GroupCode]
    extent: dict[str, tuple[int, int]]
    order: list  # formula cells, precedents first
    roles: dict  # (sheet, row, col) -> role
    semantic: dict  # (sheet, row, col) -> semantic name
    precedents: dict  # formula cell -> set of formula cells it reads
    names: list = field(default_factory=list)  # (name, scope, refers_to) of every defined name
    functions: dict = field(default_factory=dict)  # group_id -> compiled function

    @property
    def translated(self) -> list[GroupCode]:
        return [g for g in self.groups.values() if g.status == "translated"]

    def compile(self) -> None:
        ns: dict = {"xl": xl}
        code = "\n".join(g.source for g in self.translated)
        exec(compile(code, "<sheetir twin>", "exec"), ns)  # noqa: S102 - code generated from the workbook's formulas
        self.functions = {g.group_id: ns[f"g{g.group_id}"] for g in self.translated}

    def model(self, frozen: bool = False, changes: dict | None = None) -> xl.Model:
        if not self.functions and self.translated:
            self.compile()
        formulas = {ck: (self.functions.get(gid), self.baseline.get(ck)) for ck, gid in self.group_of.items()}
        m = xl.Model(self.constants, formulas, self.extent, frozen=frozen)
        for ck, v in (changes or {}).items():
            m.constants[ck] = v
        return m

    def coverage(self) -> dict:
        cells = sum(g.n_cells for g in self.groups.values())
        done = sum(g.n_cells for g in self.translated)
        reasons: dict[str, int] = {}
        for g in self.groups.values():
            if g.status != "translated":
                reasons[g.reason] = reasons.get(g.reason, 0) + g.n_cells
        return {"groups": len(self.groups), "groups_translated": len(self.translated),
                "cells": cells, "cells_translated": done,
                "untranslated": [{"reason": r, "cells": n} for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])]}


def load_twin(db: str | Path) -> Twin:
    """Translate every formula group of a SheetIR database."""
    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        sheets = json.loads(meta.get("sheets") or "[]")
        name_rows = con.execute("SELECT name, scope, refers_to FROM names").fetchall()
        names = _Names(name_rows, sheets)
        constants, baseline, group_of, roles, semantic = {}, {}, {}, {}, {}
        extent: dict[str, list[int]] = {}
        formula_at: dict = {}
        for cid, sheet, row, col, vtype, role, value, cached, formula, gid, sem in con.execute(
                "SELECT cell_id, sheet, row, col, vtype, role, value, cached, formula, group_id, semantic FROM cells"):
            key = (sheet, row, col)
            e = extent.setdefault(sheet, [0, 0])
            e[0], e[1] = max(e[0], row), max(e[1], col)
            roles[key] = role
            if sem:
                semantic[key] = sem
            if formula is not None:
                baseline[key] = decode(cached, "formula")
                group_of[key] = gid
                formula_at[cid] = (key, formula)
            else:
                constants[key] = decode(value, vtype)
        members: dict[int, list] = {}
        for key, gid in group_of.items():
            members.setdefault(gid, []).append((key[1], key[2]))
        groups: dict[int, GroupCode] = {}
        flags = {cid for (cid,) in con.execute(
            "SELECT DISTINCT cell_id FROM issues WHERE kind IN ('array_formula', 'data_table')")}
        for gid, sheet, anchor, last, n, r1c1 in con.execute(
                "SELECT group_id, sheet, anchor, last, n_cells, r1c1 FROM formula_groups ORDER BY group_id"):
            (_, arow, acol), formula = formula_at[anchor]
            status, reason, detail, expr, used = "translated", None, None, None, set()
            names.used = set()
            if anchor in flags:
                status, reason = "untranslated", "array formula or data table"
            else:
                try:
                    expr, used = translate_formula(formula, sheet, arow, acol, names)
                except Untranslatable as exc:
                    status, reason, detail = "untranslated", exc.reason, exc.detail
                except Exception as exc:  # noqa: BLE001 - a translator bug must not stop the others
                    status, reason, detail = "untranslated", "translator error", f"{type(exc).__name__}: {exc}"
            groups[gid] = GroupCode(gid, sheet, anchor, last, n, r1c1, status, reason, detail, expr, sorted(used),
                                    sorted(members.get(gid, [])), sorted(names.used))
        precedents: dict = {}
        for src, dst in con.execute("SELECT src_cell, dst_cell FROM deps"):
            if dst in formula_at and src in formula_at:
                precedents.setdefault(formula_at[dst][0], set()).add(formula_at[src][0])
    finally:
        con.close()
    order = _topological(list(group_of), precedents)
    return Twin(meta.get("source", ""), sheets, constants, baseline, group_of, groups,
                {s: (r, c) for s, (r, c) in extent.items()}, order, roles, semantic, precedents, name_rows)


def _topological(cells: list, precedents: dict) -> list:
    """Formula cells ordered so each comes after the formula cells it reads (cycles appended at the end)."""
    from collections import defaultdict

    dependents = defaultdict(list)
    indeg = {ck: 0 for ck in cells}
    for ck in cells:
        for p in precedents.get(ck, ()):
            if p in indeg and p != ck:
                indeg[ck] += 1
                dependents[p].append(ck)
    ready = sorted(ck for ck, n in indeg.items() if n == 0)
    out = []
    while ready:
        ck = ready.pop()
        out.append(ck)
        for d in dependents[ck]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    seen = set(out)
    out.extend(ck for ck in sorted(cells) if ck not in seen)
    return out


# ---------------------------------------------------------------- export --------------------------------------------

def _lit(v) -> str:
    if isinstance(v, xl.XLError):
        return f"xl.XLError({str(v)!r})"
    return repr(v)


def module_source(twin: Twin, report: dict | None = None) -> str:
    """A self-contained Python module of the twin (needs only sheetir.xl at run time).

    Run it to print the outputs, or import it: `compute(changes={("Inputs", 3, 2): 300.0})` returns every formula
    cell's value with those constants changed."""
    cov = twin.coverage()
    lines = [
        f'"""Python twin of {Path(twin.source).name or "a workbook"}, generated by SheetIR {__version__} '
        f'on {dt.date.today().isoformat()}.',
        "",
        f"{cov['groups_translated']}/{cov['groups']} formula groups ({cov['cells_translated']}/{cov['cells']} cells) "
        "are translated; the other cells keep the values Excel saved (BASELINE).",
    ]
    if report:
        for level in ("v1", "v2"):
            r = report.get(level) or {}
            if r.get("status"):
                lines.append(f"{level.upper()}: {r['status']} ({r.get('summary', '')})")
    lines += [
        "",
        "Cells are (sheet, row, column) tuples; values follow Excel (None = blank, errors are xl.XLError).",
        '"""',
        "",
        "from sheetir import xl",
        "",
        "CONSTANTS = {",
    ]
    lines += [f"    {k!r}: {_lit(v)}," for k, v in sorted(twin.constants.items(), key=lambda kv: kv[0])]
    lines += ["}", "", "EXTENT = " + repr(twin.extent), "", "", "# ---- translated formula groups ----", ""]
    for g in twin.translated:
        lines.append(g.source)
    lines += ["", "# group id -> (function or None, member (row, col) positions, reason it is not translated)",
              "GROUPS = {"]
    for g in twin.groups.values():
        fn = f"g{g.group_id}" if g.status == "translated" else "None"
        lines.append(f"    {g.group_id}: ({fn}, {g.sheet!r}, {g.members!r}, {g.reason!r}),")
    lines += ["}", "", "# Excel's saved values of the cells whose group is not translated", "BASELINE = {"]
    lines += [f"    {ck!r}: {_lit(twin.baseline.get(ck))},"
              for ck, gid in sorted(twin.group_of.items()) if twin.groups[gid].status != "translated"]
    lines += ["}", "", "ORDER = " + repr(twin.order), "",
              "OUTPUTS = " + repr(sorted(ck for ck, r in twin.roles.items() if r == "output")), "",
              "SEMANTIC = " + repr({ck: s for ck, s in sorted(twin.semantic.items()) if ck in twin.group_of}), "",
              "",
              "def compute(changes=None):",
              '    """Every formula cell\'s value, with `changes` ({(sheet, row, col): value}) applied to the constants."""',
              "    formulas = {}",
              "    for fn, sheet, members, _ in GROUPS.values():",
              "        for row, col in members:",
              "            formulas[(sheet, row, col)] = (fn, BASELINE.get((sheet, row, col)))",
              "    m = xl.Model(CONSTANTS, formulas, EXTENT)",
              "    for cell, value in (changes or {}).items():",
              "        m.constants[cell] = value",
              "    return m.compute_all(ORDER)",
              "",
              "",
              "def a1(cell):",
              '    """("Quote", 13, 10) -> "Quote!J13"."""',
              "    sheet, row, col = cell",
              '    letters = ""',
              "    while col:",
              "        col, r = divmod(col - 1, 26)",
              "        letters = chr(65 + r) + letters",
              '    return f"{sheet}!{letters}{row}"',
              "",
              "",
              'if __name__ == "__main__":',
              "    values = compute()",
              "    for cell in OUTPUTS or ORDER[-20:]:",
              "        print(f\"{a1(cell)}  {SEMANTIC.get(cell, '')}  {values.get(cell)!r}\")",
              ""]
    return "\n".join(lines)
