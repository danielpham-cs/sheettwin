"""Header text and SQL identifiers."""

from __future__ import annotations

import re

from unidecode import unidecode

# Column names the pipeline adds to tables and views; a workbook column with one of these names gets a suffix.
RESERVED = {"_row", "period", "_source_sheet", "section"}

_PAREN = re.compile(r"\s*[\(（\[]([^\)）\]]*)[\)）\]]\s*")


def slug(text: str, fallback: str = "x") -> str:
    """'Doanh thu (triệu đồng)' -> 'doanh_thu_trieu_dong'; never empty."""
    s = unidecode(str(text)).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if s and s[0].isdigit():
        s = f"c_{s}"
    return s or fallback


def strip_unit(text: str) -> str:
    """The header without its parenthesized part: 'Doanh thu (triệu đồng)' -> 'Doanh thu'."""
    out = _PAREN.sub(" ", text).strip()
    return out or text


def paren_parts(text: str) -> list[str]:
    return [m.strip() for m in _PAREN.findall(text) if m.strip()]


def identifiers(headers: list[str | None], reserved: set[str] = RESERVED) -> list[str]:
    """One unique SQL identifier per column header (design §6.4 step 4): unaccented, lowercase, [a-z0-9_], a suffix
    _2, _3 … against duplicates, col_{i} for an empty header."""
    taken: set[str] = set(reserved)
    out = []
    for i, h in enumerate(headers, start=1):
        base = slug(strip_unit(h), "") if h else ""
        base = base or f"col_{i}"
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(name)
        out.append(name)
    return out
