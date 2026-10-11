"""L1: tokenize formulas, resolve references, and build position-independent R1C1 templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from openpyxl.formula.tokenizer import Token, Tokenizer

from .refs import Ref, RelRef, parse_ref

VOLATILE = {"NOW", "TODAY", "RAND", "RANDBETWEEN", "RANDARRAY", "CELL", "INFO"}
DYNAMIC_REF = {"INDIRECT", "OFFSET"}
# Numeric arguments of these functions are positions/flags/precision, not business constants.
STRUCTURAL_ARGS = {
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "INDEX", "MATCH", "XMATCH", "CHOOSE", "OFFSET",
    "ROUND", "ROUNDUP", "ROUNDDOWN", "MROUND", "LEFT", "RIGHT", "MID", "TEXT",
    "SUBTOTAL", "AGGREGATE", "WEEKDAY", "WEEKNUM", "LARGE", "SMALL", "RANK", "DATE", "EDATE", "EOMONTH",
}


class Resolver(Protocol):
    def sheet(self, name: str) -> str | None: ...
    def name(self, name: str, host_sheet: str) -> list[Ref] | None: ...
    def structured(self, text: str, host_sheet: str, row: int, col: int) -> list[Ref] | None: ...


@dataclass
class Arg:
    """One argument of a function call, including everything nested inside it."""

    text: str = ""
    tokens: int = 0
    refs: list[tuple[Ref, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)  # calls directly inside this argument


@dataclass
class Call:
    function: str
    args: list[Arg] = field(default_factory=lambda: [Arg()])


def _emit(stack: list[Call | None], text: str, refs=()) -> None:
    """Add a token to the current argument of every enclosing call."""
    for call in stack:
        if call is not None:
            arg = call.args[-1]
            arg.text += text
            arg.tokens += 1
            arg.refs.extend(refs)


@dataclass
class ParsedFormula:
    # Template items: ["t", text] for literal text, ["r", RelRef json] for a relative reference.
    template: list[list]
    key: str  # R1C1 form; identical keys == identical computation
    refs: list[tuple[Ref, str]] = field(default_factory=list)  # (resolved ref, via)
    literals: list[tuple[float, str | None]] = field(default_factory=list)  # (value, enclosing function)
    functions: list[str] = field(default_factory=list)
    issues: list[tuple[str, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)  # every call, innermost first


def render(template: list[list], host: str, row: int, col: int, mode: str = "a1") -> str:
    parts = ["="]
    for kind, value in template:
        if kind == "t":
            parts.append(value)
        else:
            rel = RelRef.from_json(value)
            parts.append(rel.r1c1(host) if mode == "r1c1" else rel.absolutize(row, col).a1(host))
    return "".join(parts)


def _function_name(token_value: str) -> str:
    name = token_value[:-1].upper()
    for prefix in ("_XLFN._XLWS.", "_XLFN.", "_XLWS."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def parse_formula(text: str, sheet: str, row: int, col: int, resolver: Resolver) -> ParsedFormula:
    try:
        tokens = Tokenizer(text).items
    except Exception as exc:  # malformed formula: keep it, flag it
        return ParsedFormula([["t", text.lstrip("=")]], text, issues=[("parse_error", str(exc))])

    pf = ParsedFormula(template=[], key="")
    stack: list[Call | None] = []  # enclosing calls (None for a plain parenthesis)
    in_array = 0
    for tok in tokens:
        if tok.type == Token.WSPACE:
            continue
        value = tok.value

        if tok.type in (Token.FUNC, Token.PAREN) and tok.subtype == Token.CLOSE:
            call = stack.pop() if stack else None
            if call is not None:
                pf.calls.append(call)
                parent = next((c for c in reversed(stack) if c is not None), None)
                if parent is not None:
                    parent.args[-1].calls.append(call)
            _emit(stack, value)
            pf.template.append(["t", value])
            continue

        if tok.type == Token.PAREN and tok.subtype == Token.OPEN:
            _emit(stack, value)
            stack.append(None)
            pf.template.append(["t", value])
            continue

        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            fname = _function_name(value)
            _emit(stack, value)
            stack.append(Call(fname))
            pf.functions.append(fname)
            if fname in VOLATILE:
                pf.issues.append(("volatile", fname))
            if fname in DYNAMIC_REF:
                pf.issues.append(("dynamic_ref", fname))
            pf.template.append(["t", value.upper()])
            continue

        if tok.type == Token.ARRAY:
            in_array += 1 if tok.subtype == Token.OPEN else -1
        elif tok.type == Token.SEP and tok.subtype == Token.ARG and not in_array and stack and stack[-1] is not None:
            _emit(stack[:-1], value)
            stack[-1].args.append(Arg())
            pf.template.append(["t", value])
            continue

        if tok.type == Token.OPERAND and tok.subtype == Token.NUMBER:
            try:
                pf.literals.append((float(value), stack[-1].function if stack and stack[-1] else None))
            except ValueError:
                pass
            _emit(stack, value)
            pf.template.append(["t", value])
            continue

        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
            n = len(pf.refs)
            pf.template.append(_operand(value, sheet, row, col, resolver, pf))
            _emit(stack, value, pf.refs[n:])
            continue

        _emit(stack, value)
        pf.template.append(["t", value])

    pf.key = render(pf.template, sheet, row, col, mode="r1c1")
    return pf


def _operand(value: str, sheet: str, row: int, col: int, resolver: Resolver, pf: ParsedFormula) -> list:
    if value.startswith("[") and "]" in value and "!" in value:
        pf.issues.append(("external_link", value))
        return ["t", value]
    if "#REF!" in value.upper():
        pf.issues.append(("broken_ref", value))
        return ["t", value]

    ref = parse_ref(value)
    if ref is not None:
        target = sheet if ref.sheet is None else resolver.sheet(ref.sheet)
        if target is None:
            pf.issues.append(("unknown_sheet", value))
            return ["t", value]
        ref = ref.with_sheet(target)
        pf.refs.append((ref, "direct"))
        return ["r", ref.relativize(row, col).to_json()]

    if "[" in value:
        refs = resolver.structured(value, sheet, row, col)
        if refs is None:
            pf.issues.append(("unresolved_ref", value))
        else:
            pf.refs.extend((r, "structured") for r in refs)
        return ["t", value]

    refs = resolver.name(value, sheet)
    if refs is None:
        pf.issues.append(("unresolved_name", value))
    else:
        pf.refs.extend((r, f"name:{value.upper()}") for r in refs)
    return ["t", value.upper()]
