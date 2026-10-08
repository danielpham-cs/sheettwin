"""Cell and range references: A1 parsing, R1C1 relativization and rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

MAX_ROW = 1_048_576
MAX_COL = 16_384


def col_to_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def num_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_SIMPLE_SHEET = re.compile(r"^[^\W\d][\w.]*$")
_LOOKS_LIKE_REF = re.compile(r"^(?:[A-Za-z]{1,3}\d+|[Rr]\d*[Cc]\d*)$")


def quote_sheet(name: str) -> str:
    if _SIMPLE_SHEET.match(name) and not _LOOKS_LIKE_REF.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def addr(sheet: str, row: int, col: int) -> str:
    """Canonical cell id, e.g. Quote!F2 or 'My Sheet'!A1."""
    return f"{quote_sheet(sheet)}!{num_to_col(col)}{row}"


_REF_RE = re.compile(
    r"""^
    (?:(?:'(?P<qsheet>(?:[^']|'')+)'|(?P<sheet>[^'!:\[\]\s]+))!)?
    (?:
        (?P<a>\$?[A-Za-z]{1,3}\$?\d+)(?::(?P<b>\$?[A-Za-z]{1,3}\$?\d+))?
      | (?P<ca>\$?[A-Za-z]{1,3}):(?P<cb>\$?[A-Za-z]{1,3})
      | (?P<ra>\$?\d+):(?P<rb>\$?\d+)
    )$""",
    re.X,
)
_PT_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$")


@dataclass(frozen=True)
class Ref:
    """Absolute position of a cell or rectangular range.

    A None row (or col) bound means a whole-column (or whole-row) reference.
    The a* flags record `$` anchors, which matter for R1C1 relativization.
    """

    sheet: str | None
    r1: int | None
    c1: int | None
    r2: int | None
    c2: int | None
    ar1: bool = False
    ac1: bool = False
    ar2: bool = False
    ac2: bool = False

    @property
    def is_cell(self) -> bool:
        return self.r1 is not None and self.c1 is not None and self.r1 == self.r2 and self.c1 == self.c2

    def with_sheet(self, sheet: str) -> Ref:
        return replace(self, sheet=sheet)

    def contains(self, row: int, col: int) -> bool:
        return (self.r1 is None or self.r1 <= row <= self.r2) and (self.c1 is None or self.c1 <= col <= self.c2)

    @property
    def area(self) -> int:
        rows = MAX_ROW if self.r1 is None else self.r2 - self.r1 + 1
        cols = MAX_COL if self.c1 is None else self.c2 - self.c1 + 1
        return rows * cols

    def a1(self, host: str | None = None) -> str:
        """A1 text; the sheet prefix is omitted when it equals `host`."""

        def pt(r, c, ar, ac):
            s = ""
            if c is not None:
                s += ("$" if ac else "") + num_to_col(c)
            if r is not None:
                s += ("$" if ar else "") + str(r)
            return s

        body = pt(self.r1, self.c1, self.ar1, self.ac1)
        if not self.is_cell:
            body += ":" + pt(self.r2, self.c2, self.ar2, self.ac2)
        if self.sheet is None or self.sheet == host:
            return body
        return f"{quote_sheet(self.sheet)}!{body}"

    def relativize(self, row: int, col: int) -> RelRef:
        def rel(v, a, base):
            return v if (v is None or a) else v - base

        return RelRef(
            self.sheet,
            rel(self.r1, self.ar1, row),
            rel(self.c1, self.ac1, col),
            rel(self.r2, self.ar2, row),
            rel(self.c2, self.ac2, col),
            self.ar1, self.ac1, self.ar2, self.ac2,
            self.is_cell,
        )


@dataclass(frozen=True)
class RelRef:
    """A reference as seen from its host cell: relative parts are offsets (R1C1 semantics)."""

    sheet: str | None
    r1: int | None
    c1: int | None
    r2: int | None
    c2: int | None
    ar1: bool
    ac1: bool
    ar2: bool
    ac2: bool
    single: bool

    def r1c1(self, host: str | None = None) -> str:
        def part(letter, v, a):
            if v is None:
                return ""
            if a:
                return f"{letter}{v}"
            return letter if v == 0 else f"{letter}[{v}]"

        body = part("R", self.r1, self.ar1) + part("C", self.c1, self.ac1)
        if not self.single:
            body += ":" + part("R", self.r2, self.ar2) + part("C", self.c2, self.ac2)
        if self.sheet is None or self.sheet == host:
            return body
        return f"{quote_sheet(self.sheet)}!{body}"

    def absolutize(self, row: int, col: int) -> Ref:
        def ab(v, a, base):
            return v if (v is None or a) else v + base

        return Ref(
            self.sheet,
            ab(self.r1, self.ar1, row),
            ab(self.c1, self.ac1, col),
            ab(self.r2, self.ar2, row),
            ab(self.c2, self.ac2, col),
            self.ar1, self.ac1, self.ar2, self.ac2,
        )

    def to_json(self) -> list:
        return [self.sheet, self.r1, self.c1, self.r2, self.c2,
                self.ar1, self.ac1, self.ar2, self.ac2, self.single]

    @classmethod
    def from_json(cls, data: list) -> RelRef:
        return cls(*data)


def _parse_point(text: str) -> tuple[int, int, bool, bool] | None:
    m = _PT_RE.match(text)
    if not m:
        return None
    col, row = col_to_num(m.group(2)), int(m.group(4))
    if not (1 <= col <= MAX_COL and 1 <= row <= MAX_ROW):
        return None
    return row, col, bool(m.group(3)), bool(m.group(1))


def parse_ref(text: str) -> Ref | None:
    """Parse an A1 reference (optionally sheet-qualified). Returns None for names etc."""
    m = _REF_RE.match(text.strip())
    if not m:
        return None
    sheet = m.group("qsheet")
    sheet = sheet.replace("''", "'") if sheet is not None else m.group("sheet")

    if m.group("a"):
        p1 = _parse_point(m.group("a"))
        p2 = _parse_point(m.group("b")) if m.group("b") else p1
        if p1 is None or p2 is None:
            return None
        # Normalize B2:A1 -> A1:B2, keeping each `$` with its coordinate.
        (lr, lar), (hr, har) = sorted([(p1[0], p1[2]), (p2[0], p2[2])])
        (lc, lac), (hc, hac) = sorted([(p1[1], p1[3]), (p2[1], p2[3])])
        return Ref(sheet, lr, lc, hr, hc, lar, lac, har, hac)

    if m.group("ca"):
        a, b = m.group("ca"), m.group("cb")
        c1, c2 = col_to_num(a.lstrip("$")), col_to_num(b.lstrip("$"))
        if not (1 <= c1 <= MAX_COL and 1 <= c2 <= MAX_COL):
            return None
        return Ref(sheet, None, min(c1, c2), None, max(c1, c2),
                   False, a.startswith("$"), False, b.startswith("$"))

    a, b = m.group("ra"), m.group("rb")
    r1, r2 = int(a.lstrip("$")), int(b.lstrip("$"))
    if not (1 <= r1 <= MAX_ROW and 1 <= r2 <= MAX_ROW):
        return None
    return Ref(sheet, min(r1, r2), None, max(r1, r2), None,
               a.startswith("$"), False, b.startswith("$"), False)
