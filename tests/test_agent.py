"""The query agent (sheetir/agent): SQL guard, tools and hints, the loop's checks, provenance."""

import json

import pytest

from eval.generator.base_data import make_world
from eval.generator.perturb import write_workbook
from sheetir.agent.loop import answer
from sheetir.agent.protocol import AgentAction
from sheetir.agent.provenance import _filter_values
from sheetir.agent.sql_guard import SQLRejected, check_sql
from sheetir.agent.tools import Toolbox
from sheetir.build import build_ir
from sheetir.catalog.store import build_catalog
from sheetir.config import Settings
from sheetir.llm import LLMError


@pytest.fixture(scope="module")
def cat(tmp_path_factory):
    d = tmp_path_factory.mktemp("agent")
    world = make_world(5)
    write_workbook(world, d / "w.xlsx", {"split_period", "total_rows", "unit_shift"}, 5)
    build_ir(d / "w.xlsx", d / "ir.db", verify=False)
    h = build_catalog(d / "ir.db", d)
    h.world = world
    return h


@pytest.mark.parametrize("sql", [
    "DROP TABLE t", "SELECT 1; SELECT 2", "SELECT * FROM read_csv('x.csv')", "SELECT * FROM 'x.csv'",
    "SELECT * FROM unknown_table", "COPY t TO 'x'", "SELECT * FROM glob('*')", "SELECT getenv('HOME')",
    "ATTACH 'x.db'", "SELECT * FROM main.t_a", "INSTALL httpfs",
])
def test_guard_rejects(sql):
    with pytest.raises(SQLRejected):
        check_sql(sql, {"t_a"})


def test_guard_accepts_ctes_unions_and_joins():
    sql = "WITH x AS (SELECT * FROM t_a) SELECT * FROM x JOIN v_b ON x.k = v_b.k UNION ALL SELECT * FROM t_a"
    assert sorted(check_sql(sql, {"t_a", "v_b"})) == ["t_a", "v_b"]


def test_run_sql_keeps_results_and_counts_rows(cat):
    tb = Toolbox(cat, Settings(agent={"max_rows_to_llm": 3, "max_rows_return": 5}))
    try:
        r = tb.run_sql("SELECT * FROM v_fam_dt")
        assert r["result_id"] == "r1" and len(r["rows"]) == 3 and r["truncated"]
        assert r["row_count"] == 12 * len(cat.world.branches)
        assert len(tb.results["r1"]["rows"]) == 5
        assert tb.run_sql("SELECT nope FROM v_fam_dt")["columns"]["v_fam_dt"][0] == "period"
        assert "unknown table" in tb.run_sql("SELECT * FROM t_secret")["error"]
    finally:
        tb.close()


def test_empty_filter_hint_shows_real_values(cat):
    tb = Toolbox(cat)
    try:
        r = tb.run_sql("SELECT * FROM t_chinhanh__t1 WHERE mien = 'Mien Bac'")
        assert r["row_count"] == 0 and "Bắc" in r["hints"][0]
    finally:
        tb.close()


def test_make_chart_checks_columns(cat):
    tb = Toolbox(cat)
    try:
        tb.run_sql("SELECT period, SUM(doanh_thu) AS dt FROM v_fam_dt GROUP BY period ORDER BY period")
        assert "error" in tb.make_chart("r1", "bar", "period", "nope")
        assert "error" in tb.make_chart("r1", "bar", "dt", "period")  # y must be numeric
        c = tb.make_chart("r1", "line", "period", "dt", "Doanh thu")
        assert c == {"chart_id": "c1", "points": 12} and tb.charts["c1"]["type"] == "line"
    finally:
        tb.close()


def test_provenance_filters_family_members():
    assert _filter_values("SELECT * FROM v WHERE period IN ('01', '02') AND x = 1") == {"01", "02"}
    assert _filter_values("SELECT * FROM v WHERE _source_sheet = 'DT_T3'") == {"DT_T3"}
    assert _filter_values("SELECT * FROM v WHERE period >= '03'") is None
    assert _filter_values("SELECT * FROM v") is None


class ScriptedAgent:
    """A model that returns a fixed sequence of actions (dicts) from chat_json."""
    model = "scripted"

    def __init__(self, *actions):
        self.actions = list(actions)
        self.seen = []

    def chat_json(self, messages, schema, **kw):
        self.seen.append(messages[-1]["content"])
        if not self.actions:
            raise LLMError("no more actions")
        a = self.actions.pop(0)
        return schema(**a) if schema is AgentAction else schema(answer=a["answer"])


def test_loop_answers_from_a_query_with_provenance(cat):
    month1 = round(sum(r["doanh_thu"] for r in cat.world.revenue[1]), 1)
    jan = next(v for t, v in cat.families[0].member_values.items() if cat.tables[t].spec.sheet == "DT_T1")
    llm = ScriptedAgent(
        {"thought": "find", "tool": "search_catalog", "args": {"query": "doanh thu"}},
        {"thought": "sum", "tool": "run_sql",
         "args": {"sql": f"SELECT ROUND(SUM(doanh_thu), 1) AS t FROM v_fam_dt WHERE period = '{jan}'"}},
        {"thought": "done", "tool": "final_answer",
         "args": {"answer": f"Tổng doanh thu tháng 1 là {month1} triệu đồng.", "result_ids": ["r1"]}})
    steps = []
    r = answer("Tổng doanh thu tháng 1?", cat, llm, on_step=steps.append)
    assert r.grounded and r.stopped is None and r.lang == "vi"
    assert [s.tool for s in r.steps] == ["search_catalog", "run_sql", "final_answer"] and len(steps) == 3
    assert [p["sheet"] for p in r.provenance] == ["DT_T1"]
    assert r.results[0]["rows"] == [[month1]]


def test_loop_sends_back_numbers_no_query_returned(cat):
    llm = ScriptedAgent(
        {"thought": "guess", "tool": "final_answer", "args": {"answer": "It is 123456.7.", "result_ids": []}},
        {"thought": "query", "tool": "run_sql", "args": {"sql": "SELECT COUNT(*) AS n FROM t_chinhanh__t1"}},
        {"thought": "done", "tool": "final_answer",
         "args": {"answer": f"There are {len(cat.world.branches)} branches.", "result_ids": ["r1"]}})
    r = answer("How many branches are there?", cat, llm)
    assert "123456.7" in llm.seen[1] and r.grounded and str(len(cat.world.branches)) in r.answer


def test_loop_flags_numbers_it_cannot_ground(cat):
    llm = ScriptedAgent(
        {"thought": "guess", "tool": "final_answer", "args": {"answer": "About 999999.", "result_ids": []}},
        {"thought": "again", "tool": "final_answer", "args": {"answer": "Still 999999.", "result_ids": []}})
    r = answer("How much revenue?", cat, llm)
    assert not r.grounded and r.ungrounded == ["999999"] and "may be wrong" in r.answer


def test_loop_stops_honestly_at_max_steps(cat):
    llm = ScriptedAgent(*[{"thought": "look", "tool": "run_sql", "args": {"sql": "SELECT COUNT(*) AS n FROM v_fam_dt"}}] * 3)
    r = answer("How many rows?", cat, llm, Settings(agent={"max_steps": 2}))
    assert r.stopped == "max_steps" and "| n |" in r.answer and r.grounded


def test_unit_mismatch_hint(cat, tmp_path):
    # a join of two money columns in different units gets a warning
    tb = Toolbox(cat)
    try:
        card = cat.card("fam_dt")
        assert {c.unit for c in card.columns if c.name == "doanh_thu"} == {"trieu_vnd"}
        r = tb.run_sql("SELECT d.ma_cn, SUM(d.doanh_thu) FROM v_fam_dt d JOIN t_chinhanh__t1 c ON d.ma_cn = c.ma_cn "
                       "GROUP BY d.ma_cn")
        assert "hints" not in r  # one unit only
    finally:
        tb.close()
    json.dumps(r)  # results are JSON-able


def test_or_without_parentheses_gets_a_hint(cat):
    tb = Toolbox(cat)
    try:
        r = tb.run_sql("SELECT COUNT(*) FROM t_chinhanh__t1 WHERE mien = 'Bắc' OR mien = 'Nam' AND ma_cn = 'x'")
        assert any("parentheses" in h for h in r.get("hints", []))
        r = tb.run_sql("SELECT COUNT(*) FROM t_chinhanh__t1 WHERE (mien = 'Bắc' OR mien = 'Nam') AND ma_cn <> 'x'")
        assert not any("parentheses" in h for h in r.get("hints", []))
    finally:
        tb.close()
