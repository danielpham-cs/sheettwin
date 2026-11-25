"""The agent's tools (design §7.3). Every tool returns a JSON-able dict and never raises: a problem comes back as
{"error": …, "hint": …} for the model to act on.

    search_catalog(query, k)                     cards that match, best first
    describe_table(card_id)                      a card in full: columns with types and units, sample rows, relations
    run_sql(sql)                                 a guarded read-only query; the result is kept under a result_id
    make_chart(result_id, type, x, y, title)     a chart of a kept result: bar | line | pie | scatter
    model_cells / what_if / sensitivity / goal_seek / propose_correction   the twin's tools (whatif_tools.py), when given
    final_answer(answer, result_ids, chart_id)   handled by the loop

Hints after a query (§7.3.2): the right column names when one is unknown; the values a column really holds when a
filter on it finds nothing; a warning when a query combines money columns in different units, or when a join returns
far more rows than its tables have.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re

import duckdb
import sqlglot
from sqlglot import exp

from ..clean.units import MONEY_FACTOR
from ..config import Settings, get_settings
from .sql_guard import SQLRejected, check_sql, run_sql

CHART_TYPES = ("bar", "line", "pie", "scatter")


def jsonable(v):
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v


class Toolbox:
    def __init__(self, catalog, settings: Settings | None = None, whatif=None):
        self.cat = catalog
        self.whatif = whatif  # WhatIfTools, or None: the what-if tools are not offered
        self.s = settings or get_settings()
        self._con = None
        self.results: dict[str, dict] = {}
        self.charts: dict[str, dict] = {}
        self.searches: list[list[str]] = []   # card ids each search_catalog call returned (for recall@k)
        self.sql_runs: list[dict] = []        # every run_sql: {sql, ok, error}

    @property
    def con(self):
        if self._con is None:
            self._con = self.cat.connect()
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def dispatch(self, tool: str, args: dict) -> dict:
        try:
            if tool == "search_catalog":
                return self.search_catalog(str(args.get("query", "")), args.get("k"))
            if tool == "describe_table":
                return self.describe_table(str(args.get("card_id") or args.get("table") or args.get("sql_name") or ""))
            if tool == "run_sql":
                return self.run_sql(str(args.get("sql", "")))
            if tool == "make_chart":
                return self.make_chart(**{k: args.get(k) for k in ("result_id", "type", "x", "y", "title")})
            if tool in ("model_cells", "what_if", "sensitivity", "goal_seek", "propose_correction"):
                return self._whatif(tool, args)
            return {"error": f"unknown tool {tool!r}"}
        except Exception as e:  # noqa: BLE001 - the model gets the error, the loop goes on
            return {"error": f"{type(e).__name__}: {e}"[:400]}

    def _whatif(self, tool: str, args: dict) -> dict:
        if self.whatif is None:
            return {"error": f"{tool} is not available here: no workbook of this question has a Python twin"}
        res = self.whatif.dispatch(tool, args)
        if "result_id" not in res:
            return res
        self.results[res["result_id"]] = jsonable(res)
        return jsonable({k: v for k, v in res.items() if k not in ("sql", "tables")})

    # ---- search_catalog / describe_table -----------------------------------------------------------------------------

    def search_catalog(self, query: str, k=None) -> dict:
        try:
            k = max(1, min(30, int(k or self.s.agent.search_k)))
        except (TypeError, ValueError):
            k = self.s.agent.search_k
        hits = self.cat.search(query, k)
        self.searches.append([c.card_id for c, _ in hits])
        return {"tables": [{"card_id": c.card_id, "sql_name": c.sql_name, "title": c.title, "description": c.description,
                            "columns": [x.name for x in c.columns], "time_coverage": c.time_coverage,
                            "row_count": c.row_count, "score": score,
                            **({"low_confidence": True} if c.low_confidence else {})} for c, score in hits]}

    def describe_table(self, card_id: str) -> dict:
        c = self.cat.card(card_id)
        if c is None:
            return {"error": f"no table {card_id!r}", "hint": "use a card_id or sql_name returned by search_catalog",
                    "known": sorted(x.card_id for x in self.cat.cards)[:40]}
        sources = c.sources[:12]
        return jsonable({
            "card_id": c.card_id, "sql_name": c.sql_name, "title": c.title, "description": c.description,
            "row_count": c.row_count, "time_coverage": c.time_coverage,
            "columns": [{"name": x.name, "type": x.dtype, "unit": x.unit, "description": x.description}
                        for x in c.columns],
            "sample_rows": c.sample_rows[:5], "relations": c.relations,
            "sources": [{"sheet": s["sheet"], "range": s["range"], **({"value": s["value"]} if "value" in s else {})}
                        for s in sources] + ([{"more_sheets": len(c.sources) - 12}] if len(c.sources) > 12 else []),
            "warnings": c.warnings[:6], **({"low_confidence": True} if c.low_confidence else {})})

    # ---- run_sql ----------------------------------------------------------------------------------------------------

    def _columns_of(self, names: list[str]) -> dict[str, list[str]]:
        out = {}
        for n in names:
            c = self.cat.card(n)
            if c is not None:
                out[c.sql_name] = [x.name for x in c.columns]
        return out

    def run_sql(self, sql: str) -> dict:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            return {"error": "empty query"}
        try:
            used = check_sql(sql, self.cat.sql_names)
        except SQLRejected as e:
            self.sql_runs.append({"sql": sql, "ok": False, "error": str(e)})
            return {"error": str(e), "hint": "tables you can query: " + ", ".join(sorted(self.cat.sql_names)[:30])}
        try:
            res = run_sql(self.con, sql, self.s.agent.sql_timeout_s, self.s.agent.max_rows_return)
        except TimeoutError as e:
            self.sql_runs.append({"sql": sql, "ok": False, "error": str(e)})
            return {"error": str(e), "hint": "aggregate in SQL, filter earlier, or check the join condition"}
        except duckdb.Error as e:
            msg = str(e).strip().splitlines()[0][:400]
            self.sql_runs.append({"sql": sql, "ok": False, "error": msg})
            out = {"error": msg}
            if re.search(r"not found|does not have a column|Referenced column|Binder Error", msg, re.I):
                out["hint"] = "the columns of the tables you used"
                out["columns"] = self._columns_of(used)
            return out
        rid = f"r{len(self.results) + 1}"
        res = {"result_id": rid, "sql": sql, "tables": used, **jsonable(res)}
        self.results[rid] = res
        self.sql_runs.append({"sql": sql, "ok": True, "error": None})
        shown = res["rows"][: self.s.agent.max_rows_to_llm]
        out = {"result_id": rid, "columns": res["columns"], "rows": shown, "row_count": res["row_count"],
               "truncated": res["row_count"] > len(shown)}
        hints = self._hints(sql, used, res)
        if hints:
            out["hints"] = hints
        return out

    def _hints(self, sql: str, used: list[str], res: dict) -> list[str]:
        hints = []
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except sqlglot.errors.ParseError:
            return hints
        # a filter that finds nothing: show the values the column really holds
        if res["row_count"] == 0 or (res["row_count"] == 1 and all(v in (None, 0) for v in res["rows"][0])):
            for cond in [*tree.find_all(exp.EQ), *tree.find_all(exp.Like), *tree.find_all(exp.ILike)]:
                col = cond.find(exp.Column)
                lit = cond.find(exp.Literal)
                if col is None or lit is None or not lit.is_string:
                    continue
                for t in used:
                    card = self.cat.card(t)
                    if card is None or col.name not in [x.name for x in card.columns]:
                        continue
                    vals = [r[0] for r in self.con.execute(
                        f'SELECT DISTINCT "{col.name}" FROM "{card.sql_name}" WHERE "{col.name}" IS NOT NULL '
                        f'ORDER BY 1 LIMIT 25').fetchall()]
                    hints.append(f"no rows matched {cond.sql()}; values in {card.sql_name}.{col.name}: "
                                 f"{json.dumps(jsonable(vals), ensure_ascii=False)[:600]}")
                    break
        # `a OR b AND c` reads as `a OR (b AND c)`: usually a filter meant to apply to both sides
        where = tree.find(exp.Where)
        if where is not None and isinstance(where.this, exp.Or) and any(
                isinstance(x, exp.And) for x in (where.this.left, where.this.right)):
            hints.append("the WHERE clause mixes OR and AND without parentheses: AND binds tighter, so the other "
                         "conditions apply to one side of the OR only. Add parentheses around the OR.")
        # money columns in different units combined in one query
        if len(used) >= 2:
            named = {c.name for c in tree.find_all(exp.Column)}
            units = {}
            for t in used:
                card = self.cat.card(t)
                for x in (card.columns if card else []):
                    if x.name in named and x.unit in MONEY_FACTOR:
                        units.setdefault(x.unit, []).append(f"{card.sql_name}.{x.name}")
            if len(units) > 1:
                hints.append("these columns are in different units, convert before combining them: " +
                             "; ".join(f"{u}: {', '.join(cols)}" for u, cols in units.items()) +
                             " (nghin_vnd = 1e3 vnd, trieu_vnd = 1e6 vnd, ty_vnd = 1e9 vnd)")
        # a join that multiplies rows
        if tree.find(exp.Join) is not None and used:
            biggest = max((self.cat.card(t).row_count for t in used if self.cat.card(t)), default=0)
            if biggest and res["row_count"] > 10 * biggest:
                hints.append(f"the join returned {res['row_count']} rows, more than 10x the largest table "
                             f"({biggest}): the join key is probably wrong or not unique")
        return hints

    # ---- make_chart -------------------------------------------------------------------------------------------------

    def make_chart(self, result_id=None, type=None, x=None, y=None, title=None) -> dict:  # noqa: A002
        res = self.results.get(str(result_id))
        if res is None:
            return {"error": f"no result {result_id!r}", "hint": f"results so far: {sorted(self.results)}"}
        kind = str(type or "bar").lower()
        if kind not in CHART_TYPES:
            return {"error": f"type must be one of {', '.join(CHART_TYPES)}"}
        cols = res["columns"]
        ys = [y] if isinstance(y, str) else list(y or [])
        if x not in cols:
            return {"error": f"x {x!r} is not a column of {result_id}", "columns": cols}
        bad = [c for c in ys if c not in cols]
        if not ys or bad:
            return {"error": f"y must be one or more columns of {result_id}", "columns": cols}
        idx = {c: cols.index(c) for c in [x, *ys]}
        for c in ys:
            vals = [r[idx[c]] for r in res["rows"] if r[idx[c]] is not None]
            if not vals or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                return {"error": f"y column {c!r} is not numeric"}
        if kind == "pie" and len(ys) != 1:
            return {"error": "a pie chart takes exactly one y column"}
        cid = f"c{len(self.charts) + 1}"
        data = [{x: r[idx[x]], **{c: r[idx[c]] for c in ys}} for r in res["rows"]]
        self.charts[cid] = {"chart_id": cid, "result_id": res["result_id"], "type": kind, "x": x, "y": ys,
                            "title": str(title or ""), "data": data, "truncated": res["truncated"]}
        return {"chart_id": cid, "points": len(data)}
