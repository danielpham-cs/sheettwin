"""Units of a column, from its header and number format (design §6.4 step 7), as a short code."""

from __future__ import annotations

import re

from .headers import paren_parts, slug

# Most specific first. Matched against a header's parenthesized text and the literal parts of a number format.
_UNITS: list[tuple[str, re.Pattern]] = [(code, re.compile(p, re.I)) for code, p in [
    ("ty_vnd", r"^(tỷ|ty)(\s*(đồng|dong|vnđ|vnd|đ))?$|billion\s*vnd|^bn\s*vnd$"),
    ("trieu_vnd", r"^(triệu|trieu)(\s*(đồng|dong|vnđ|vnd|đ))?$|^tr\.?\s*(đ|đồng|dong|vnd)?$|^trđ$|million\s*vnd|"
                  r"^mil\.?\s*vnd$|^1\.?000\.?000\s*(đ|vnd|vnđ|đồng)$"),
    ("nghin_vnd", r"^(nghìn|ngàn|nghin|ngan)(\s*(đồng|dong|vnđ|vnd|đ))?$|^1[.,]?000\s*(đ|vnd|vnđ|đồng|dong)$|"
                  r"^k\s*vnd$|thousand\s*vnd|^000\s*vnd$"),
    ("vnd", r"^(đồng|dong|vnđ|vnd|đ|₫)$"),
    ("usd", r"^(usd|us\$|\$)$"),
    ("eur", r"^(eur|€)$"),
    ("percent", r"^(%|phần trăm|phan tram|percent|pct)$"),
]]

# Money units that can be converted into one another (design §7.3.2: warn when a query mixes them).
MONEY_FACTOR = {"vnd": 1.0, "nghin_vnd": 1e3, "trieu_vnd": 1e6, "ty_vnd": 1e9}

_FMT_LITERAL = re.compile(r'"([^"]+)"|\[\$([^\]-]+)')


def normalize_unit(text: str | None) -> str | None:
    """A unit text as a code: 'triệu đồng' -> 'trieu_vnd', '%' -> 'percent'. A short text that is no known unit becomes
    its slug ('kg', 'twd_kg'); anything else (a long phrase, text with digits other than a known scale) None."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip().strip(".:")).strip()
    for code, rx in _UNITS:
        if rx.search(t):
            return code
    if len(t) <= 12 and not re.search(r"\d", t) and re.search(r"\w", t):
        return slug(t)
    return None


def unit_from_format(fmt: str | None) -> str | None:
    if not fmt or fmt == "General":
        return None
    if "%" in re.sub(r'"[^"]*"', "", fmt):
        return "percent"
    for a, b in _FMT_LITERAL.findall(fmt):
        u = normalize_unit((a or b).strip())
        if u:
            return u
    return None


def column_unit(header: str | None, formats: list[str | None]) -> str | None:
    """The column's unit: from the header's parentheses first (the author named it), else from the most common number
    format of its cells."""
    for part in reversed(paren_parts(header or "")):
        u = normalize_unit(part)
        if u:
            return u
    counted: dict[str, int] = {}
    for f in formats:
        u = unit_from_format(f)
        if u:
            counted[u] = counted.get(u, 0) + 1
    if counted and sum(counted.values()) * 2 >= len([f for f in formats if f]):
        return max(counted, key=counted.get)
    return None
