"""Families: tables of the same shape split across tabs (DT_T1 … DT_T12), joined into one view (design §6.5.1).

Two tables belong together when their column names overlap by Jaccard >= relations.family_jaccard with matching
types, or when they have the same number of columns with the same types in the same order, at least half of the names
equal position by position, and their sheet names differ only in one part (a month renamed 'Doanh thu' -> 'DT' still
joins its family). The part that differs between the members' sheet names (or titles, for tables on one sheet) is read
as a period where it can be: T3, Tháng 3, M03, March -> 03 (2026-03 when a year is in the name or the title), Q1,
Quý 2 -> Q1/Q2, 2025 -> 2025. The view adds that value (`period`, or `part` when none reads as a period) and the source
sheet, and converts money columns to the unit most members use when some members count in another (nghìn vs triệu).
"""

from __future__ import annotations

import os
import re
from collections import Counter

from unidecode import unidecode

from ..clean.headers import slug
from ..clean.models import CleanTable
from ..clean.units import MONEY_FACTOR
from .models import TableFamily

_MONTHS = {m: i for i, ms in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")], start=1) for m in ms}
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def _cols(t: CleanTable):
    return [(c.name, "num" if c.dtype in ("int", "float") else c.dtype) for c in t.columns]


def _common_affixes(names: list[str]) -> tuple[str, str]:
    """Common prefix and suffix of the names, cut back to a separator so a token is not split (T1 / T12 -> 'dt_')."""
    pre = os.path.commonprefix(names)
    suf = os.path.commonprefix([n[::-1] for n in names])[::-1]
    shortest = min(len(n) for n in names)
    if len(pre) + len(suf) > shortest:
        suf = suf[len(pre) + len(suf) - shortest:]
    m = re.match(r"^(.*[_\W])", pre)
    pre = m.group(1) if m else ("" if any(len(n) > len(pre) and n[len(pre)].isalnum() and pre[-1:].isalnum()
                                          for n in names) else pre)
    m = re.match(r"^([_\W].*)$", suf) if suf else None
    suf = suf if m else ""
    return pre, suf


def _differing(names: list[str]) -> list[str]:
    keys = [unidecode(n).lower().strip() for n in names]
    pre, suf = _common_affixes(keys)
    return [k[len(pre):len(k) - len(suf) if suf else None].strip(" _-.") for k in keys]


def parse_period(token: str, context: str = "") -> str | None:
    """A sheet-name part as a period, or None: 't3' -> '03', 'thang 3' -> '03', 'q2' -> 'Q2', '2025' -> '2025', with the
    year (from the token or `context`, e.g. the table's title) in front when known: '2026-03'."""
    t = unidecode(token).lower().strip()
    year = None
    m = _YEAR.search(t)
    if m:
        year, t = m.group(1), (t[:m.start()] + t[m.end():]).strip(" _-./")
    if not year and context:
        m = _YEAR.search(unidecode(context))
        year = m.group(1) if m else None
    if not t:
        return year
    t = re.sub(r"[\s_.\-/]+", "", t)
    m = re.fullmatch(r"(?:t|thang|m|month|th)?0?(\d{1,2})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        mm = f"{int(m.group(1)):02d}"
        return f"{year}-{mm}" if year else mm
    if t in _MONTHS:
        mm = f"{_MONTHS[t]:02d}"
        return f"{year}-{mm}" if year else mm
    m = re.fullmatch(r"(?:q|quy|quarter)([1-4])", t)
    if m:
        return f"{year}-Q{m.group(1)}" if year else f"Q{m.group(1)}"
    m = re.fullmatch(r"(?:h|nua)([12])", t)
    if m:
        return f"{year}-H{m.group(1)}" if year else f"H{m.group(1)}"
    return None


def _same_form(periods: list[str | None]) -> list[str | None]:
    """One form for a family's periods: when some members name the year (a title saying 'THÁNG 3/2026') and the others
    do not, and every year named is the same, the others get it too ('03' -> '2026-03'). Mixed forms would make a
    filter on one of them miss the rest."""
    years = {p[:4] for p in periods if p and re.match(r"^\d{4}-", p)}
    if len(years) != 1:
        return periods
    (year,) = years
    return [f"{year}-{p}" if p and re.fullmatch(r"\d{2}|Q[1-4]|H[12]", p) else p for p in periods]


def _compatible(a: CleanTable, b: CleanTable, jaccard: float) -> bool:
    ca, cb = _cols(a), _cols(b)
    na, nb = {n for n, _ in ca}, {n for n, _ in cb}
    if not na or not nb:
        return False
    da, db = dict(ca), dict(cb)
    common = na & nb
    if len(common) / len(na | nb) >= jaccard and all(da[n] == db[n] for n in common):
        return True
    if len(ca) == len(cb) and len(ca) >= 2 and all(x[1] == y[1] for x, y in zip(ca, cb)):
        same = sum(x[0] == y[0] for x, y in zip(ca, cb))
        if same * 2 >= len(ca) and a.spec.sheet != b.spec.sheet:
            parts = _differing([a.spec.sheet, b.spec.sheet])
            return all(parts) and all(parse_period(p) for p in parts)
    return False


def find_families(tables: list[CleanTable], jaccard: float = 0.9) -> list[TableFamily]:
    parent = list(range(len(tables)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(tables)):
        for j in range(i + 1, len(tables)):
            if tables[i].n_rows and tables[j].n_rows and root(i) != root(j) and _compatible(tables[i], tables[j], jaccard):
                parent[root(j)] = root(i)
    groups: dict[int, list[CleanTable]] = {}
    for i, t in enumerate(tables):
        groups.setdefault(root(i), []).append(t)

    families, taken = [], set()
    for members in groups.values():
        if len(members) < 2:
            continue
        sheets = [m.spec.sheet for m in members]
        if len(set(sheets)) == len(sheets):
            parts = _differing(sheets)
            labels = [s for s in sheets]
        else:
            titles = [m.spec.title or m.table_id for m in members]
            parts = _differing(titles)
            labels = titles
        periods = _same_form([parse_period(p, f"{m.spec.title or ''} {m.spec.sheet}") for p, m in zip(parts, members)])
        if sum(p is not None for p in periods) * 2 >= len(periods) and len(set(periods)) == len(periods):
            disc = "period"
            values = [p or lab for p, lab in zip(periods, labels)]
        else:
            disc = "part"
            values = [p or lab for p, lab in zip(parts, labels)]
        base = slug(_common_affixes([slug(s) + "_" for s in sheets])[0].strip("_") or
                    os.path.commonprefix([m.table_id for m in members]).strip("_"), "family")
        fid, n = f"fam_{base}", 2
        while fid in taken:
            fid, n = f"fam_{base}_{n}", n + 1
        taken.add(fid)
        order = sorted(range(len(members)), key=lambda i: (values[i], sheets[i]))
        families.append(TableFamily(
            family_id=fid, member_table_ids=[members[i].table_id for i in order], discriminator=disc,
            member_values={members[i].table_id: values[i] for i in order}, duckdb_view=f"v_{fid}"))
    return families


def view_sql(family: TableFamily, tables: dict[str, CleanTable]) -> tuple[str, list[str], dict, list[str]]:
    """(CREATE VIEW statement, the view's data columns, the unit of each, notes). Columns come from the first member;
    another member's column is matched by name, else by position; money columns are scaled to the unit most members
    use."""
    members = [tables[t] for t in family.member_table_ids]
    first = members[0]
    canon = [c for c in first.columns]
    target_unit, view_units = {}, {}
    notes = []
    for i, c in enumerate(canon):
        units = Counter()
        for m in members:
            mc = _match(m, c.name, i, len(canon))
            if mc is not None:
                units[mc.unit] += 1
        view_units[c.name] = units.most_common(1)[0][0] if units else c.unit
        money = Counter({u: n for u, n in units.items() if u in MONEY_FACTOR})
        if len(money) > 1:
            target_unit[c.name] = money.most_common(1)[0][0]
            view_units[c.name] = target_unit[c.name]
    selects = []
    for m in members:
        val = family.member_values[m.table_id].replace("'", "''")
        sheet = m.spec.sheet.replace("'", "''")
        parts = [f"'{val}' AS \"{family.discriminator}\"", f"'{sheet}' AS \"_source_sheet\"", '"_row"']
        for i, c in enumerate(canon):
            mc = _match(m, c.name, i, len(canon))
            if mc is None:
                parts.append(f'NULL AS "{c.name}"')
                continue
            expr = f'"{mc.name}"'
            if c.name in target_unit and mc.unit in MONEY_FACTOR and mc.unit != target_unit[c.name]:
                factor = MONEY_FACTOR[mc.unit] / MONEY_FACTOR[target_unit[c.name]]
                expr = f"{expr} * {factor!r}"
                notes.append(f"{m.spec.sheet}: {mc.name} converted from {mc.unit} to {target_unit[c.name]}")
            parts.append(f'{expr} AS "{c.name}"')
        selects.append(f"SELECT {', '.join(parts)} FROM \"{m.duckdb_name}\"")
    sql = f'CREATE OR REPLACE VIEW "{family.duckdb_view}" AS\n' + "\nUNION ALL\n".join(selects)
    return sql, [c.name for c in canon], view_units, notes


def _match(t: CleanTable, name: str, i: int, n: int):
    by_name = next((c for c in t.columns if c.name == name), None)
    if by_name is not None:
        return by_name
    return t.columns[i] if len(t.columns) == n else None
