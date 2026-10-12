"""L2: relations between data, read from lookup and conditional-aggregate calls.

A formula such as =VLOOKUP(B2, Materials!$A$2:$C$4, 3, FALSE) states a fact, not a guess:
the value in B2 is matched against Materials!A2:A4 and the answer comes from
Materials!C2:C4. These facts show which columns are keys and how tables join,
without any layout heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .formula import Arg, Call, ParsedFormula
from .loader import CellKey
from .refs import Ref

LOOKUPS = {"VLOOKUP", "HLOOKUP", "XLOOKUP", "LOOKUP", "MATCH", "XMATCH"}
# function -> (index of the aggregated range or None, index of the first criteria range)
AGGREGATES = {
    "SUMIF": (2, 0), "AVERAGEIF": (2, 0), "COUNTIF": (None, 0),
    "SUMIFS": (0, 1), "AVERAGEIFS": (0, 1), "MAXIFS": (0, 1), "MINIFS": (0, 1), "COUNTIFS": (None, 0),
}
_XMODES = {0: "exact", -1: "approximate", 1: "approximate", 2: "wildcard"}


@dataclass
class Relation:
    cell: CellKey
    function: str
    kind: str  # lookup | aggregate
    match: str  # exact | approximate | wildcard | unknown (lookups); equal | condition (aggregates)
    value: str  # lookup value or criterion, as written
    value_cell: CellKey | None  # the one cell the value comes from, if any
    key: Ref | None  # range searched; None when it is computed (e.g. by INDIRECT)
    ret: Ref | None  # range the result comes from; None for MATCH / COUNTIF
    key_axis: str = "col"  # col: key is a column (VLOOKUP); row: key is a row (HLOOKUP)


def _arg(call: Call, i: int) -> Arg | None:
    return call.args[i] if i < len(call.args) else None


def _range(arg: Arg | None) -> Ref | None:
    """The range an argument names, if it is a plain reference rather than an expression."""
    if arg is None or arg.tokens != 1 or len(arg.refs) != 1:
        return None
    return arg.refs[0][0]


def _single_cell(arg: Arg | None) -> CellKey | None:
    cells = {(r.sheet, r.r1, r.c1) for r, _ in arg.refs if r.is_cell} if arg else set()
    if len(cells) != 1 or any(not r.is_cell for r, _ in arg.refs):
        return None
    return cells.pop()


def _number(call: Call, i: int, default: float | None) -> float | None:
    """Numeric or logical argument; an omitted argument gets `default`, an empty one counts as 0."""
    arg = _arg(call, i)
    if arg is None:
        return default
    text = arg.text.strip().upper()
    if text in ("", "FALSE"):
        return 0.0
    if text == "TRUE":
        return 1.0
    try:
        return float(text)
    except ValueError:
        return None


def _column(ref: Ref | None, i: int) -> Ref | None:
    if ref is None or ref.c1 is None or not ref.c1 + i <= ref.c2:
        return None
    return Ref(ref.sheet, ref.r1, ref.c1 + i, ref.r2, ref.c1 + i)


def _row(ref: Ref | None, i: int) -> Ref | None:
    if ref is None or ref.r1 is None or not ref.r1 + i <= ref.r2:
        return None
    return Ref(ref.sheet, ref.r1 + i, ref.c1, ref.r1 + i, ref.c2)


def _descendants(arg: Arg | None):
    for call in arg.calls if arg else ():
        yield call
        for a in call.args:
            yield from _descendants(a)


def _match_mode(call: Call) -> str:
    if call.function == "XMATCH":
        return _XMODES.get(_number(call, 2, 0.0), "unknown")
    m = _number(call, 2, 1.0)
    return "unknown" if m is None else "exact" if m == 0 else "approximate"


def _lookup(ck: CellKey, call: Call) -> Relation | None:
    f = call.function
    value = _arg(call, 0)

    def rel(match, key, ret, axis="col", function=f):
        return Relation(ck, function, "lookup", match, value.text if value else "", _single_cell(value),
                        key, ret, axis)

    if f in ("VLOOKUP", "HLOOKUP"):
        table, idx = _range(_arg(call, 1)), _number(call, 2, None)
        m = _number(call, 3, 1.0)
        match = "unknown" if m is None else "exact" if m == 0 else "approximate"
        if f == "VLOOKUP":
            ret = _column(table, int(idx) - 1) if idx and idx >= 1 else table
            return rel(match, _column(table, 0), ret)
        ret = _row(table, int(idx) - 1) if idx and idx >= 1 else table
        return rel(match, _row(table, 0), ret, "row")
    if f == "XLOOKUP":
        return rel(_XMODES.get(_number(call, 4, 0.0), "unknown"), _range(_arg(call, 1)), _range(_arg(call, 2)))
    if f == "LOOKUP":
        return rel("approximate", _range(_arg(call, 1)), _range(_arg(call, 2)))
    if f in ("MATCH", "XMATCH"):
        return rel(_match_mode(call), _range(_arg(call, 1)), None)
    return None


def _index_match(ck: CellKey, call: Call, consumed: set[int]) -> list[Relation]:
    """INDEX(array, MATCH(v, keys, 0) [, col]): a lookup whose key and answer ranges are separate."""
    array = _range(_arg(call, 0))
    out = []
    for i, axis in ((1, "col"), (2, "row")):
        for m in _descendants(_arg(call, i)):
            if m.function not in ("MATCH", "XMATCH"):
                continue
            consumed.add(id(m))
            ret = array
            if axis == "col" and array is not None and array.c1 is not None and array.c1 != array.c2:
                n = _number(call, 2, None) if len(call.args) > 2 else None
                ret = _column(array, int(n) - 1) if n and n >= 1 else array
            value = _arg(m, 0)
            out.append(Relation(ck, f"INDEX/{m.function}", "lookup", _match_mode(m), value.text if value else "",
                                _single_cell(value), _range(_arg(m, 1)), ret, axis))
    return out


def _aggregate(ck: CellKey, call: Call) -> list[Relation]:
    target_idx, first = AGGREGATES[call.function]
    out = []
    for i in range(first, len(call.args), 2):
        key, crit = _range(_arg(call, i)), _arg(call, i + 1)
        if crit is None:
            break
        if target_idx is None:
            ret = None
        elif call.function in ("SUMIF", "AVERAGEIF"):
            ret = _range(_arg(call, target_idx)) or key
        else:
            ret = _range(_arg(call, target_idx))
        plain = crit.tokens == 1 and _single_cell(crit) is not None
        out.append(Relation(ck, call.function, "aggregate", "equal" if plain else "condition", crit.text,
                            _single_cell(crit), key, ret))
    return out


def extract_relations(parsed: dict[CellKey, ParsedFormula]) -> list[Relation]:
    out: list[Relation] = []
    for ck, pf in sorted(parsed.items()):
        consumed: set[int] = set()
        for call in pf.calls:
            if call.function == "INDEX":
                out.extend(_index_match(ck, call, consumed))
        for call in pf.calls:
            if call.function in AGGREGATES:
                out.extend(_aggregate(ck, call))
            elif call.function in LOOKUPS and id(call) not in consumed:
                r = _lookup(ck, call)
                if r is not None:
                    out.append(r)
    return out
