"""Typing a column's values (design §6.4 step 6).

A cell gives a candidate: a number, a date, a boolean, or text. Text is read further: numbers written with separators
('1.250,5' or '1,250.5', the locale decided by the column's majority), percentages ('12,5%'), negative numbers in
parentheses, and day-first dates ('05/03/2026'). The column's type is the one at least 90% of its non-empty cells share;
values that do not fit become NULL (and are counted), and a column with no majority type is text.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import from_excel

MAJORITY = 0.9

_DATE_TEXT = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?)?$")
_COMMA_GROUPS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")   # 1,250,000.5
_DOT_GROUPS = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")     # 1.250.000,5
_PLAIN = re.compile(r"^\d+([.,]\d+)?$")
_STRIP = re.compile(r"[\s ₫$€]|(?i:vnđ|vnd|đ)$")


def _iso(text: str):
    m = _ISO.match(text)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    try:
        if m[4] and (m[4], m[5], m[6]) != ("00", "00", "00"):
            return dt.datetime(y, mo, d, int(m[4]), int(m[5]), int(m[6]))
        return dt.date(y, mo, d)
    except ValueError:
        return None


def candidate(vtype: str, raw, number_format: str | None):
    """(kind, value) of one cell: kind is int | float | bool | date | text | numtext (a number written as text, read
    once the column's locale is known), or (None, None) for an empty or error cell."""
    if raw is None or vtype == "error":
        return None, None
    if isinstance(raw, bool):
        return "bool", raw
    if isinstance(raw, (int, float)):
        if vtype == "formula" and number_format and is_date_format(number_format):
            try:
                v = from_excel(raw)
                return "date", v.date() if isinstance(v, dt.datetime) and v.time() == dt.time() else v
            except (ValueError, OverflowError, TypeError):
                pass
        if isinstance(raw, float) and raw.is_integer() and abs(raw) < 2**53:
            return "int", int(raw)
        return ("int" if isinstance(raw, int) else "float"), raw
    text = str(raw).strip()
    if not text:
        return None, None
    if vtype == "date" or (vtype == "formula" and number_format and is_date_format(number_format)):
        d = _iso(text)
        if d is not None:
            return "date", d
    if text.startswith("#") and text.endswith(("!", "?", "A")) and len(text) <= 8:  # #REF!, #N/A, #NAME?
        return None, None
    m = _DATE_TEXT.match(text)
    if m:
        try:
            return "date", dt.date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            pass
    d = _iso(text)
    if d is not None:
        return "date", d
    if text.lower() in ("true", "false"):
        return "bool", text.lower() == "true"
    core = _STRIP.sub("", text)
    if core.startswith("(") and core.endswith(")"):
        core = "-" + core[1:-1]
    body = core.lstrip("+-").rstrip("%")
    if body and (_PLAIN.match(body) or _COMMA_GROUPS.match(body) or _DOT_GROUPS.match(body)):
        return "numtext", core
    return "text", text


def _locale(texts: list[str]) -> str:
    """'dot' when the column writes 1.250,5 (Vietnamese, European), else 'comma' (1,250.5). Decided by the texts that
    can be read only one way; ties and no evidence fall back to the comma style, except a lone comma decimal (2,5)."""
    dot = comma = 0
    for t in texts:
        b = t.lstrip("+-").rstrip("%")
        if _DOT_GROUPS.match(b) and ("," in b or b.count(".") > 1):
            dot += 1
        elif _COMMA_GROUPS.match(b) and ("." in b or b.count(",") > 1):
            comma += 1
        elif re.match(r"^\d+,\d{1,2}$", b) or re.match(r"^\d+,\d{4,}$", b):
            dot += 1  # 2,5 or 3,14159: a decimal comma
        elif re.match(r"^\d+\.\d{1,2}$", b) or re.match(r"^\d+\.\d{4,}$", b):
            comma += 1
    return "dot" if dot > comma else "comma"


def parse_numtext(text: str, locale: str) -> float | None:
    pct = text.endswith("%")
    t = text.rstrip("%")
    if locale == "dot":
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v / 100 if pct else v


def column_type(cands: list[tuple[str | None, object]]) -> tuple[str, list, int]:
    """(dtype, values, n_dropped) for a column's cell candidates: dtype is int | float | date | bool | text; values are
    typed (None where empty or not fitting); n_dropped counts non-empty cells that did not fit the type."""
    filled = [(k, v) for k, v in cands if k is not None]
    if not filled:
        return "text", [None] * len(cands), 0
    loc = _locale([v for k, v in filled if k == "numtext"])
    nums = [(k, v if k != "numtext" else parse_numtext(v, loc)) for k, v in filled]
    kinds = Counter("num" if k in ("int", "float", "numtext") and v is not None else k for k, v in nums)
    kind, n = kinds.most_common(1)[0]
    if n < MAJORITY * len(filled):
        kind = "text"

    def text_of(k, v):
        if v is None:
            return None
        if k == "date":
            return v.isoformat()
        if k in ("int", "float") and isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    values, dropped = [], 0
    for k, v in cands:
        if k is None:
            values.append(None)
            continue
        if kind == "num":
            x = parse_numtext(v, loc) if k == "numtext" else (v if k in ("int", "float") else None)
        elif kind == "text":
            x = text_of(k, v)
        else:
            x = v if k == kind else None
        dropped += x is None
        values.append(x)
    if kind == "num":
        ints = all(isinstance(x, int) or (isinstance(x, float) and x.is_integer() and abs(x) < 2**53)
                   for x in values if x is not None)
        kind = "int" if ints else "float"
        values = [None if x is None else (int(x) if kind == "int" else float(x)) for x in values]
    elif kind == "date":
        # datetimes at midnight are dates; a column with real times stays a timestamp but is reported as date
        values = [x.date() if isinstance(x, dt.datetime) and x.time() == dt.time() else x for x in values]
    return kind, values, dropped
