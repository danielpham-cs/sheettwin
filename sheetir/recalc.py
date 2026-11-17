"""Values for formula cells Excel never stored a result for (design §6.1.4), computed from the IR.

A file written by a program other than Excel (openpyxl, many exporters) holds formulas without results, so every
computed column would be empty. This evaluates those formulas from the workbook's own constants: arithmetic, comparison,
&, %, ranges, defined names, and the functions business sheets use most

    SUM AVERAGE MIN MAX COUNT COUNTA COUNTBLANK PRODUCT SUMPRODUCT ROUND ROUNDUP ROUNDDOWN INT ABS MOD POWER SQRT
    IF IFERROR IFNA AND OR NOT SUMIF SUMIFS COUNTIF COUNTIFS AVERAGEIF AVERAGEIFS VLOOKUP HLOOKUP INDEX MATCH XLOOKUP
    CONCATENATE CONCAT LEFT RIGHT MID LEN UPPER LOWER TRIM TEXT VALUE TODAY NOW YEAR MONTH DAY DATE

A formula with anything else (array arithmetic, INDIRECT, structured references, unknown functions) is left without a
value, as before. Results Excel did store are never replaced: they are used as they are.

    computed = recalc_missing(con)      # {(sheet, row, col): value} for formula cells that had no stored result
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import Token
from openpyxl.utils.datetime import from_excel, to_excel

from .refs import parse_ref

MAX_DEPTH = 400


class XLError(Exception):
    """An Excel error value (#DIV/0!, #N/A, #VALUE!, …), raised through the evaluation."""


class Unsupported(Exception):
    """Something this evaluator does not handle: the cell keeps no value."""


class Area:
    """A range's values, row by row."""

    def __init__(self, rows: list[list]):
        self.rows = rows

    def flat(self):
        return [v for r in self.rows for v in r]

    @property
    def shape(self):
        return len(self.rows), len(self.rows[0]) if self.rows else 0


# ---- parsing -------------------------------------------------------------------------------------------------------

_PREC = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2, "+": 3, "-": 3, "*": 4, "/": 4, "^": 5}


class _Parser:
    def __init__(self, formula: str):
        try:
            items = Tokenizer(formula).items
        except Exception as e:  # noqa: BLE001
            raise Unsupported(f"tokenize: {e}") from e
        self.items = [t for t in items if t.type != Token.WSPACE]
        self.pos = 0

    def parse(self):
        node = self.expr(0)
        if self.pos != len(self.items):
            raise Unsupported(f"unexpected {self.items[self.pos].value!r}")
        return node

    def peek(self):
        return self.items[self.pos] if self.pos < len(self.items) else None

    def expr(self, min_prec: int):
        left = self.prefix()
        while True:
            t = self.peek()
            if t is None:
                return left
            if t.type == Token.OP_POST and t.value == "%":
                self.pos += 1
                left = ("pct", left)
                continue
            if t.type != Token.OP_IN or t.value not in _PREC or _PREC[t.value] < min_prec:
                return left
            self.pos += 1
            right = self.expr(_PREC[t.value] + 1)
            left = ("bin", t.value, left, right)

    def prefix(self):
        t = self.peek()
        if t is None:
            raise Unsupported("missing operand")
        self.pos += 1
        if t.type == Token.OP_PRE:
            return ("neg" if t.value == "-" else "pos", self.expr(6))
        if t.type == Token.PAREN and t.subtype == Token.OPEN:
            node = self.expr(0)
            close = self.peek()
            if close is None or close.type != Token.PAREN or close.subtype != Token.CLOSE:
                raise Unsupported("unclosed parenthesis")
            self.pos += 1
            return node
        if t.type == Token.FUNC and t.subtype == Token.OPEN:
            name = t.value[:-1].upper().removeprefix("_XLFN.").removeprefix("_XLWS.")
            args = []
            if self.peek() is not None and self.peek().type == Token.FUNC and self.peek().subtype == Token.CLOSE:
                self.pos += 1
                return ("fn", name, args)
            while True:
                nxt = self.peek()
                if nxt is not None and nxt.type == Token.SEP and nxt.subtype == Token.ARG:
                    args.append(("empty",))
                else:
                    args.append(self.expr(0))
                nxt = self.peek()
                if nxt is None:
                    raise Unsupported("unclosed call")
                self.pos += 1
                if nxt.type == Token.FUNC and nxt.subtype == Token.CLOSE:
                    return ("fn", name, args)
                if not (nxt.type == Token.SEP and nxt.subtype == Token.ARG):
                    raise Unsupported(f"unexpected {nxt.value!r} in call")
        if t.type == Token.OPERAND:
            if t.subtype == Token.NUMBER:
                return ("lit", float(t.value))
            if t.subtype == Token.TEXT:
                return ("lit", t.value[1:-1].replace('""', '"'))
            if t.subtype == Token.LOGICAL:
                return ("lit", t.value.upper() == "TRUE")
            if t.subtype == Token.ERROR:
                return ("err", t.value)
            if t.subtype == Token.RANGE:
                return ("ref", t.value)
        raise Unsupported(f"token {t.value!r}")


# ---- values --------------------------------------------------------------------------------------------------------

def _num(v) -> float:
    if isinstance(v, Area):
        vals = v.flat()
        if len(vals) != 1:
            raise Unsupported("a range where a single value is needed")
        v = vals[0]
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return float(to_excel(v))
    try:
        return float(str(v).strip())
    except ValueError:
        raise XLError("#VALUE!") from None


def _text(v) -> str:
    if isinstance(v, Area):
        vals = v.flat()
        if len(vals) != 1:
            raise Unsupported("a range where a single value is needed")
        v = vals[0]
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _scalar(v):
    if isinstance(v, Area):
        vals = v.flat()
        if len(vals) != 1:
            raise Unsupported("a range where a single value is needed")
        return vals[0]
    return v


def _truthy(v) -> bool:
    v = _scalar(v)
    if isinstance(v, str):
        if v.upper() in ("TRUE", "FALSE"):
            return v.upper() == "TRUE"
        raise XLError("#VALUE!")
    return bool(_num(v))


def _rank(v):
    """Excel's ordering across types: numbers < text < logical; text compared without case."""
    if v is None:
        return (0, 0.0)
    if isinstance(v, bool):
        return (2, int(v))
    if isinstance(v, (int, float, dt.date)):
        return (0, _num(v))
    return (1, str(v).lower())


def _compare(op, a, b) -> bool:
    a, b = _scalar(a), _scalar(b)
    if a is None and isinstance(b, str):
        a = ""
    if b is None and isinstance(a, str):
        b = ""
    ra, rb = _rank(a), _rank(b)
    return {"=": ra == rb, "<>": ra != rb, "<": ra < rb, ">": ra > rb, "<=": ra <= rb, ">=": ra >= rb}[op]


def _round(x: float, digits: int, mode=ROUND_HALF_UP) -> float:
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(x)).quantize(q, rounding=mode))


def _criteria(c):
    """A SUMIF/COUNTIF criterion as a predicate: 5, ">=5", "<>x", "a*", "=b"."""
    c = _scalar(c)
    if not isinstance(c, str):
        return lambda v: _rank(_scalar(v)) == _rank(c)
    m = re.match(r"^(<=|>=|<>|<|>|=)?(.*)$", c, re.S)
    op, rest = m.group(1) or "=", m.group(2)
    try:
        target = float(rest)
    except ValueError:
        target = rest
    if isinstance(target, str) and op in ("=", "<>") and any(ch in target for ch in "*?"):
        rx = re.compile("^" + re.escape(target).replace(r"\*", ".*").replace(r"\?", ".") + "$", re.I | re.S)
        hit = lambda v: v is not None and bool(rx.match(_text(v)))  # noqa: E731
        return hit if op == "=" else (lambda v: not hit(v))
    if isinstance(target, str) and target == "" and op == "=":
        return lambda v: v is None or v == ""
    def pred(v):
        if isinstance(target, float) and (v is None or isinstance(v, str)):
            if isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    return op == "<>"
            else:
                return op == "<>"
        return _compare(op, v, target)
    return pred


# ---- the evaluator -------------------------------------------------------------------------------------------------

class Evaluator:
    """`ignore_cached` evaluates every formula, stored result or not (to check the evaluator against Excel)."""

    def __init__(self, con: sqlite3.Connection, ignore_cached: bool = False):
        self.values: dict[tuple[str, int, int], object] = {}
        self.formulas: dict[tuple[str, int, int], str] = {}
        self.max_row: dict[str, int] = {}
        self.sheets: list[str] = json.loads((con.execute("SELECT value FROM meta WHERE key = 'sheets'").fetchone()
                                             or ["[]"])[0])
        for sheet, row, col, vtype, value, cached, formula in con.execute(
                "SELECT sheet, row, col, vtype, value, cached, formula FROM cells"):
            key = (sheet, row, col)
            self.max_row[sheet] = max(self.max_row.get(sheet, 0), row)
            raw = cached if formula is not None else value
            v = None if raw is None else json.loads(raw)
            if vtype == "date" and isinstance(v, str):
                try:
                    v = dt.datetime.fromisoformat(v)
                except ValueError:
                    pass
            if formula is not None and (raw is None or ignore_cached):
                self.formulas[key] = formula
            else:
                self.values[key] = v
        self.names: dict[tuple[str | None, str], str] = {
            (scope, name.lower()): refers for name, scope, refers in con.execute("SELECT name, scope, refers_to FROM names")}
        self.computed: dict[tuple[str, int, int], object] = {}
        self._stack: set = set()
        self._ast: dict = {}

    def _sheet(self, name: str | None, host: str) -> str:
        if name is None:
            return host
        for s in self.sheets:
            if s.lower() == name.lower():
                return s
        raise XLError("#REF!")

    def cell(self, key):
        if key in self.values:
            return self.values[key]
        if key in self.computed:
            v = self.computed[key]
            if isinstance(v, XLError):
                raise v
            return v
        if key not in self.formulas:
            return None
        if key in self._stack or len(self._stack) > MAX_DEPTH:
            raise Unsupported("circular reference or too deep")
        self._stack.add(key)
        try:
            v = self.eval_formula(self.formulas[key], key[0])
            self.computed[key] = v
            return v
        except XLError as e:
            self.computed[key] = e
            raise
        finally:
            self._stack.discard(key)

    def eval_formula(self, formula: str, host: str):
        ast = self._ast.get(formula)
        if ast is None:
            ast = _Parser(formula).parse()
            self._ast[formula] = ast
        v = self.ev(ast, host)
        if isinstance(v, Area):
            v = _scalar(v)
        return v

    def ref(self, text: str, host: str):
        r = parse_ref(text)
        if r is None:
            scope_name = (host, text.lower())
            refers = self.names.get(scope_name) or self.names.get((None, text.lower()))
            if refers is None:
                raise Unsupported(f"unknown name {text}")
            r = parse_ref(refers.lstrip("="))
            if r is None:
                raise Unsupported(f"name {text} is not a range")
            host = r.sheet or host
        sheet = self._sheet(r.sheet, host)
        if r.is_cell:
            return self.cell((sheet, r.r1, r.c1))
        r1 = r.r1 or 1
        r2 = r.r2 if r.r2 is not None else self.max_row.get(sheet, 1)
        c1, c2 = r.c1 or 1, r.c2 or 1
        if r.c1 is None:
            raise Unsupported("whole-row reference")
        if (r2 - r1 + 1) * (c2 - c1 + 1) > 200_000:
            raise Unsupported("range too large")
        return Area([[self.cell((sheet, row, col)) for col in range(c1, c2 + 1)] for row in range(r1, r2 + 1)])

    def ev(self, node, host):
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "err":
            raise XLError(node[1])
        if kind == "empty":
            return None
        if kind == "ref":
            return self.ref(node[1], host)
        if kind == "neg":
            return -_num(self.ev(node[1], host))
        if kind == "pos":
            return self.ev(node[1], host)
        if kind == "pct":
            return _num(self.ev(node[1], host)) / 100
        if kind == "bin":
            op, a, b = node[1], self.ev(node[2], host), self.ev(node[3], host)
            if isinstance(a, Area) or isinstance(b, Area):
                a, b = _scalar(a), _scalar(b)
            if op in ("=", "<>", "<", ">", "<=", ">="):
                return _compare(op, a, b)
            if op == "&":
                return _text(a) + _text(b)
            x, y = _num(a), _num(b)
            if op == "+":
                return x + y
            if op == "-":
                return x - y
            if op == "*":
                return x * y
            if op == "/":
                if y == 0:
                    raise XLError("#DIV/0!")
                return x / y
            if op == "^":
                return x ** y
        if kind == "fn":
            return self.call(node[1], node[2], host)
        raise Unsupported(str(kind))

    # ---- functions ------------------------------------------------------------------------------------------------

    def _args(self, args, host):
        return [self.ev(a, host) for a in args]

    def _numbers(self, vals):
        """Numbers among the arguments as SUM sees them: from ranges only numbers; typed-in arguments converted."""
        out = []
        for v in vals:
            if isinstance(v, Area):
                out += [float(x) for x in v.flat() if isinstance(x, (int, float)) and not isinstance(x, bool)]
            elif v is not None:
                out.append(_num(v))
        return out

    def call(self, name, args, host):
        lazy = {"IF", "IFERROR", "IFNA"}
        if name in lazy:
            if name == "IF":
                cond = _truthy(self.ev(args[0], host))
                if cond:
                    return self.ev(args[1], host) if len(args) > 1 else True
                return self.ev(args[2], host) if len(args) > 2 else False
            try:
                return self.ev(args[0], host)
            except XLError as e:
                if name == "IFNA" and str(e) != "#N/A":
                    raise
                return self.ev(args[1], host)
        a = self._args(args, host)
        if name == "SUM":
            return sum(self._numbers(a))
        if name == "PRODUCT":
            return math.prod(self._numbers(a))
        if name in ("AVERAGE", "MIN", "MAX"):
            nums = self._numbers(a)
            if not nums:
                if name == "AVERAGE":
                    raise XLError("#DIV/0!")
                return 0.0
            return {"AVERAGE": sum(nums) / len(nums), "MIN": min(nums), "MAX": max(nums)}[name]
        if name == "COUNT":
            return float(len(self._numbers([x for x in a if isinstance(x, Area)])) +
                         sum(1 for x in a if not isinstance(x, Area) and isinstance(x, (int, float))))
        if name == "COUNTA":
            return float(sum(1 for x in a for v in (x.flat() if isinstance(x, Area) else [x]) if v not in (None, "")))
        if name == "COUNTBLANK":
            return float(sum(1 for x in a for v in (x.flat() if isinstance(x, Area) else [x]) if v in (None, "")))
        if name in ("ROUND", "ROUNDUP", "ROUNDDOWN"):
            x, d = _num(a[0]), int(_num(a[1])) if len(a) > 1 else 0
            if name == "ROUND":
                return _round(x, d)
            from decimal import ROUND_DOWN, ROUND_UP
            return _round(x, d, ROUND_UP if name == "ROUNDUP" else ROUND_DOWN)
        if name == "INT":
            return float(math.floor(_num(a[0])))
        if name == "ABS":
            return abs(_num(a[0]))
        if name == "MOD":
            y = _num(a[1])
            if y == 0:
                raise XLError("#DIV/0!")
            return _num(a[0]) - y * math.floor(_num(a[0]) / y)
        if name == "POWER":
            return _num(a[0]) ** _num(a[1])
        if name == "SQRT":
            return math.sqrt(_num(a[0]))
        if name == "AND":
            return all(_truthy(v) for x in a for v in (x.flat() if isinstance(x, Area) else [x]) if v is not None)
        if name == "OR":
            return any(_truthy(v) for x in a for v in (x.flat() if isinstance(x, Area) else [x]) if v is not None)
        if name == "NOT":
            return not _truthy(a[0])
        if name in ("SUMIF", "AVERAGEIF"):
            rng, pred = a[0], _criteria(a[1])
            tgt = a[2] if len(a) > 2 else rng
            if not isinstance(rng, Area) or not isinstance(tgt, Area):
                raise Unsupported(f"{name} on non-ranges")
            vals = [t for r, t in zip(rng.flat(), tgt.flat()) if pred(r) and isinstance(t, (int, float))
                    and not isinstance(t, bool)]
            if name == "AVERAGEIF":
                if not vals:
                    raise XLError("#DIV/0!")
                return sum(vals) / len(vals)
            return float(sum(vals))
        if name == "COUNTIF":
            pred = _criteria(a[1])
            return float(sum(1 for v in a[0].flat() if pred(v)))
        if name in ("SUMIFS", "AVERAGEIFS", "COUNTIFS"):
            if name == "COUNTIFS":
                pairs, tgt = a, None
            else:
                tgt, pairs = a[0], a[1:]
            ranges = [pairs[i].flat() for i in range(0, len(pairs), 2)]
            preds = [_criteria(pairs[i]) for i in range(1, len(pairs), 2)]
            n = len(ranges[0])
            hits = [i for i in range(n) if all(p(r[i]) for r, p in zip(ranges, preds))]
            if name == "COUNTIFS":
                return float(len(hits))
            t = tgt.flat()
            vals = [t[i] for i in hits if isinstance(t[i], (int, float)) and not isinstance(t[i], bool)]
            if name == "AVERAGEIFS":
                if not vals:
                    raise XLError("#DIV/0!")
                return sum(vals) / len(vals)
            return float(sum(vals))
        if name == "SUMPRODUCT":
            arrays = [x.flat() if isinstance(x, Area) else [x] for x in a]
            if len({len(x) for x in arrays}) != 1:
                raise XLError("#VALUE!")
            total = 0.0
            for vals in zip(*arrays):
                p = 1.0
                for v in vals:
                    p *= float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
                total += p
            return total
        if name in ("VLOOKUP", "HLOOKUP"):
            key, table = _scalar(a[0]), a[1]
            idx = int(_num(a[2]))
            exact = len(a) > 3 and a[3] is not None and not _truthy(a[3])
            rows = table.rows if name == "VLOOKUP" else [list(c) for c in zip(*table.rows)]
            if idx < 1 or (rows and idx > len(rows[0])):
                raise XLError("#REF!")
            col = [r[0] for r in rows]
            i = self._find(key, col, 0 if exact else 1)
            return rows[i][idx - 1]
        if name == "MATCH":
            key, rng = _scalar(a[0]), a[1]
            mode = int(_num(a[2])) if len(a) > 2 and a[2] is not None else 1
            return float(self._find(key, rng.flat(), mode) + 1)
        if name == "INDEX":
            rng = a[0]
            r = int(_num(a[1])) if len(a) > 1 and a[1] is not None else 0
            c = int(_num(a[2])) if len(a) > 2 and a[2] is not None else 0
            rows = rng.rows if isinstance(rng, Area) else [[rng]]
            if len(rows) == 1 and c == 0 and r:
                r, c = 1, r
            if c == 0:
                c = 1
            if r == 0:
                r = 1
            if r > len(rows) or c > len(rows[0]):
                raise XLError("#REF!")
            return rows[r - 1][c - 1]
        if name == "XLOOKUP":
            key, look, ret = _scalar(a[0]), a[1].flat(), a[2].flat()
            try:
                return ret[self._find(key, look, 0)]
            except XLError:
                if len(a) > 3 and a[3] is not None:
                    return a[3]
                raise
        if name in ("CONCATENATE", "CONCAT"):
            return "".join(_text(v) for x in a for v in (x.flat() if isinstance(x, Area) else [x]))
        if name == "LEFT":
            return _text(a[0])[: int(_num(a[1])) if len(a) > 1 else 1]
        if name == "RIGHT":
            n = int(_num(a[1])) if len(a) > 1 else 1
            return _text(a[0])[-n:] if n else ""
        if name == "MID":
            s, start, n = _text(a[0]), int(_num(a[1])), int(_num(a[2]))
            return s[start - 1: start - 1 + n]
        if name == "LEN":
            return float(len(_text(a[0])))
        if name == "UPPER":
            return _text(a[0]).upper()
        if name == "LOWER":
            return _text(a[0]).lower()
        if name == "TRIM":
            return re.sub(r" +", " ", _text(a[0]).strip())
        if name == "VALUE":
            return _num(a[0])
        if name == "TEXT":
            return _text(a[0])
        if name == "TODAY":
            return float(to_excel(dt.date.today()))
        if name == "NOW":
            return float(to_excel(dt.datetime.now()))
        if name in ("YEAR", "MONTH", "DAY"):
            v = _scalar(a[0])
            d = v if isinstance(v, (dt.date, dt.datetime)) else from_excel(_num(v))
            return float(getattr(d, name.lower()))
        if name == "DATE":
            y, m, d = (int(_num(x)) for x in a[:3])
            # Excel carries month and day overflow over: DATE(2026, 13, 1) is 2027-01-01, DATE(2026, 3, 0) 2026-02-28
            return float(to_excel(_add_months(dt.date(y, 1, 1), m - 1) + dt.timedelta(days=d - 1)))
        raise Unsupported(f"function {name}")

    @staticmethod
    def _find(key, values: list, mode: int) -> int:
        """Position of `key` in `values`: mode 0 exact (wildcards in text), 1 the largest value <= key (sorted
        ascending), -1 the smallest value >= key (sorted descending). Raises #N/A."""
        if mode == 0:
            if isinstance(key, str) and any(ch in key for ch in "*?"):
                pred = _criteria(key)
                for i, v in enumerate(values):
                    if pred(v):
                        return i
            else:
                rk = _rank(key)
                for i, v in enumerate(values):
                    if v is not None and _rank(v) == rk:
                        return i
            raise XLError("#N/A")
        rk = _rank(key)
        best = None
        for i, v in enumerate(values):
            if v is None:
                continue
            rv = _rank(v)
            if rv[0] != rk[0]:
                continue
            if (mode == 1 and rv <= rk) or (mode == -1 and rv >= rk):
                best = i
            elif best is not None:
                break
        if best is None:
            raise XLError("#N/A")
        return best


def _add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    return dt.date(d.year + y, m + 1, 1)


def recalc_missing(con: sqlite3.Connection, limit: int = 200_000) -> dict[tuple[str, int, int], object]:
    """{(sheet, row, col): value} for every formula cell without a stored result that can be computed. Numbers come
    back as int when whole, dates stay Excel serials (the cell's number format says it is a date)."""
    ev = Evaluator(con)
    out = {}
    for key in list(ev.formulas)[:limit]:
        try:
            v = ev.cell(key)
        except (XLError, Unsupported, RecursionError, ValueError, TypeError, OverflowError, AttributeError,
                IndexError, ZeroDivisionError):
            continue
        if isinstance(v, float) and v.is_integer() and abs(v) < 2**53:
            v = int(v)
        if v is not None and not isinstance(v, Area):
            out[key] = v
    return out
