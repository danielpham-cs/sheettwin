import sqlite3

import pytest
from fastapi.testclient import TestClient

from sheetir import build_ir
from sheetir.sample import make_quotation
from sheetir.web import graph
from sheetir.web.app import app
from sheetir.web.charts import charts
from sheetir.web.chat import answer, find_cells


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web")
    res = build_ir(make_quotation(tmp / "quotation.xlsx"), tmp / "q.db")
    c = sqlite3.connect(res.db_path)
    yield c
    c.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    return TestClient(app)


@pytest.fixture
def wid(client):
    return client.post("/api/workbooks/sample").json()["id"]


# ---- HTTP API -------------------------------------------------------------------------------------

def test_sample_is_one_workbook_however_often_it_is_loaded(client, wid):
    assert client.post("/api/workbooks/sample").json()["id"] == wid
    assert [w["id"] for w in client.get("/api/workbooks").json()] == [wid]


def test_summary_has_per_sheet_stats(client, wid):
    s = client.get(f"/api/workbooks/{wid}/summary").json()
    assert not s["stale"] and s["table_scorer"] == "hand"
    stats = {x["sheet"]: x for x in s["sheet_stats"]}
    assert stats["Quote"]["tables"] == 1 and stats["Quote"]["issues_high"] == 2
    assert s["counts"]["issues"] == 9 and s["counts"]["edges"] > 0


def test_sheet_carries_tables_and_params(client, wid):
    d = client.get(f"/api/workbooks/{wid}/sheet", params={"name": "Quote"}).json()
    (t,) = d["tables"]
    assert (t["name"], t["header_row"], t["total_row"]) == ("quote", 1, 22)
    assert [f["name"] for f in t["fields"]][-1] == "margin"
    inputs = client.get(f"/api/workbooks/{wid}/sheet", params={"name": "Inputs"}).json()
    assert {p["name"] for p in inputs["params"]} >= {"copper_price", "labor_rate"}


def test_layout_for_minimaps(client, wid):
    lay = client.get(f"/api/workbooks/{wid}/layout").json()
    assert set(lay) == {"Inputs", "Materials", "Quote", "Report"}
    assert any(c[4] == "high" for c in lay["Quote"]["cells"])
    assert lay["Quote"]["tables"][0]["name"] == "quote"


def test_download_rebuild_delete(client, wid):
    r = client.get(f"/api/workbooks/{wid}/db")
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
    assert client.post(f"/api/workbooks/{wid}/rebuild").json()["counts"]["tables"] == 2
    assert client.delete(f"/api/workbooks/{wid}").json() == {"deleted": wid}
    assert client.get("/api/workbooks").json() == []
    assert client.get(f"/api/workbooks/{wid}/summary").status_code == 404


def test_rejects_bad_ids_and_writes(client, wid):
    assert client.get("/api/workbooks/../../etc/summary").status_code in (400, 404)
    assert client.delete("/api/workbooks/not-an-id").status_code == 400
    r = client.post(f"/api/workbooks/{wid}/sql", json={"query": "DELETE FROM cells"})
    assert r.status_code == 400


# ---- chat, graphs, charts ---------------------------------------------------------------------------

def test_find_cells_normalizes_sheet_case(con):
    assert find_cells(con, "quote!j13 和 Inputs!B3, quote!J13") == ["Quote!J13", "Inputs!B3"]


def test_chat_cell_reply_traces_precedents(con):
    res = answer(con, "Quote!J13 怎麼算的")
    assert res["cells"] == ["Quote!J13"]
    assert 'quote["Cover plate"].margin' in res["reply"]
    assert "Quote!I13" in res["reply"]


def test_chat_issues_lists_planted_problems(con):
    reply = answer(con, "有哪些問題")["reply"]
    assert "Quote!I13" in reply and "inconsistent_formula" in reply


def test_overview_collapses_blocks(con):
    g = graph.overview(con)
    by_label = {n["label"]: n for n in g["nodes"]}
    margin = by_label["quote[*].margin"]  # 20 rows + total cell, one group
    assert margin["kind"] == "formula" and margin["cells"] == 21
    assert by_label["quote[*].copper"]["kind"] == "input"
    assert by_label["params.copper_price"]["kind"] == "param"
    # the typed-over cell and the off-pattern formula stand alone, with their issues
    assert by_label['quote["Contact spring"].labor_cost']["issues"]["severity"] == "high"
    assert "inconsistent_formula" in by_label['quote["Cover plate"].price']["issues"]["kinds"]
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids and e["source"] != e["target"] for e in g["edges"])
    assert len(g["nodes"]) < 40


def test_focus_has_precedents_and_dependents(con):
    g = graph.focus(con, "Quote!I13")
    depth = {n["id"]: n["depth"] for n in g["nodes"]}
    assert depth["Quote!I13"] == 0
    assert depth["Quote!H13"] == 1 and depth["Inputs!B6"] == 1
    assert depth["Quote!J13"] == -1
    assert {"source": "Quote!H13", "target": "Quote!I13", "weight": 1} in g["edges"]


def test_chart_data_has_stats_and_table_rows(con):
    c = charts(con)
    assert sum(r["n"] for r in c["roles"]) == con.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    assert {r["severity"] for r in c["severity"]} == {"high", "medium", "low"}
    quote = next(t for t in c["tables"] if t["name"] == "quote")
    assert len(quote["rows"]) == 20  # total row excluded
    fields = {f["name"]: f for f in quote["fields"]}
    assert fields["copper"]["numeric"] and fields["copper"]["unit"] == "kg"
    assert fields["material"]["distinct"] == 3 and not fields["material"]["numeric"]
    # written by openpyxl, never recalculated: formula columns have no cached values
    assert fields["price"]["numeric"] and fields["price"]["missing"] == 20
    assert quote["rows"][0]["values"]["copper"] == 1.8


def test_chat_replies_in_english(con):
    assert "Found **9** issues" in answer(con, "What issues are there?", "en")["reply"]
    reply = answer(con, "Quote!I13", "en")["reply"]
    assert "**Formula**:" in reply and "Calculated from" in reply
    assert "rule-based assistant" in answer(con, "hello", "en")["reply"]
    assert "**quotation.xlsx** has 4 sheets" in answer(con, "Give me a summary", "en")["reply"]
