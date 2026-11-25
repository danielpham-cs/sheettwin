"""Provenance of an answer (design §7.4): the sheets and ranges its query results were read from.

The tables and views each result's SQL reads (from the guard); a family view expands to its member sheets, and only to
those its WHERE clause selects when it filters on the discriminator or _source_sheet with = or IN.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

_FILTER_COLUMNS = {"period", "part", "_source_sheet"}


def _filter_values(sql: str) -> set[str] | None:
    """Values a query keeps for period/part/_source_sheet, or None when it does not filter on them (or in a way this
    cannot read, such as a range)."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return None
    where = tree.find(exp.Where)
    if where is None:
        return None
    values: set[str] = set()
    found = False
    for node in where.walk():
        if isinstance(node, exp.EQ):
            col, lit = node.find(exp.Column), node.find(exp.Literal)
            if col is not None and lit is not None and col.name in _FILTER_COLUMNS:
                values.add(lit.this)
                found = True
        elif isinstance(node, exp.In):
            col = node.this if isinstance(node.this, exp.Column) else None
            if col is not None and col.name in _FILTER_COLUMNS:
                lits = [e for e in node.expressions if isinstance(e, exp.Literal)]
                if len(lits) == len(node.expressions):
                    values |= {lit.this for lit in lits}
                    found = True
        elif isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.Like)):
            col = node.find(exp.Column)
            if col is not None and col.name in _FILTER_COLUMNS:
                return None
    return values if found else None


def provenance(catalog, results: list[dict]) -> list[dict]:
    """[{sheet, range, table_title, card_id}] for the given run_sql results, without duplicates."""
    out, seen = [], set()
    for res in results:
        if not res.get("sql"):  # a what-if result (whatif_tools.py): read from cells, not tables
            continue
        values = _filter_values(res["sql"])
        for t in res.get("tables", []):
            for src in catalog.sources(t, values):
                key = (src["sheet"], src["range"])
                if key not in seen:
                    seen.add(key)
                    out.append(src)
    return out
