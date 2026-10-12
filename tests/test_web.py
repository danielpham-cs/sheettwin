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


def test_sql_check_compiles_without_running(client, wid):
    check = lambda q: client.post(f"/api/workbooks/{wid}/sql/check", json={"query": q}).json()["error"]
    assert check("SELECT * FROM cells LIMIT 5") is None
    assert check("SELECT * FROM cells limt 10") == 'near "10": syntax error'
    assert check("SELECT semantik FROM cells") == "no such column: semantik"
    assert check("DELETE FROM cells") == "only SELECT / WITH queries are allowed"


# ---- provenance: which file produced which result ------------------------------------------------------

def _workbook(path, price):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Item", "Qty", "Price", "Amount"])
    ws.append(["Pen", 2, price, "=B2*C2"])
    ws.append(["Ink", 3, 5, "=B3*C3"])
    wb.save(path)
    return path.read_bytes()


def test_same_bytes_are_one_result_and_reupload_says_so(client, tmp_path):
    data = _workbook(tmp_path / "a.xlsx", 10)
    first = client.post("/api/workbooks", files={"file": ("orders.xlsx", data)}, data={"last_modified": "1767225600000"}).json()
    again = client.post("/api/workbooks", files={"file": ("orders-copy.xlsx", data)}).json()
    assert (first["duplicate"], again["duplicate"]) == (False, True)
    assert again["id"] == first["id"] and not again["reparsed"] and again["counts"] == first["counts"]
    (w,) = client.get("/api/workbooks").json()
    assert (w["name"], w["aliases"], w["uploads"], w["version"], w["versions"]) == ("orders.xlsx", ["orders-copy.xlsx"], 2, 1, 1)
    assert w["sha256"].startswith(w["id"]) and w["size"] == len(data) and w["file_modified"].startswith("2026-01-01")


def test_versions_by_file_name_and_diff(client, tmp_path):
    v1 = client.post("/api/workbooks", files={"file": ("orders.xlsx", _workbook(tmp_path / "1.xlsx", 10))}).json()["id"]
    v2 = client.post("/api/workbooks", files={"file": ("orders.xlsx", _workbook(tmp_path / "2.xlsx", 12))}).json()["id"]
    other = client.post("/api/workbooks", files={"file": ("prices.xlsx", _workbook(tmp_path / "3.xlsx", 99))}).json()["id"]
    cat = {w["id"]: w for w in client.get("/api/workbooks").json()}
    assert (cat[v1]["version"], cat[v2]["version"], cat[v2]["versions"], cat[v2]["previous"]) == (1, 2, 2, v1)
    assert (cat[other]["version"], cat[other]["versions"]) == (1, 1)
    d = client.get(f"/api/workbooks/{v2}/diff", params={"against": v1}).json()
    assert d["counts"] == {"added": 0, "removed": 0, "changed": 1} and not d["identical"]
    assert d["cells"] == [{"cell_id": "Sheet!C2", "change": "changed", "old": "10", "new": "12"}]
    assert client.get(f"/api/workbooks/{v2}/diff", params={"against": v2}).json()["identical"]


def test_downloads_name_the_result(client, wid):
    r = client.get(f"/api/workbooks/{wid}/exports/ir.db")
    assert f"quotation_sample.{wid}.sheetir.db" in r.headers["content-disposition"]
    assert client.get(f"/api/workbooks/{wid}/summary").json()["source"]["name"] == "quotation_sample.xlsx"


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


# ---- user corrections to table detection ------------------------------------------------------------

def _tables(client, wid, sheet):
    return client.get(f"/api/workbooks/{wid}/sheet", params={"name": sheet}).json()["tables"]


def test_user_defines_tables_on_a_sheet(client, wid):
    r = client.put(f"/api/workbooks/{wid}/user-tables/Inputs",
                   json={"tables": [{"range": "a3:b7", "header_rows": 0, "name": "Assumptions"}]})
    assert r.status_code == 200 and r.json()["status"] in ("verified", "partial")
    (t,) = _tables(client, wid, "Inputs")
    assert (t["source"], t["name"], t["header_row"], t["row_start"], t["row_end"]) == ("user", "assumptions", None, 3, 7)
    # the correction is stored with the result and applied again on every parse
    assert client.get(f"/api/workbooks/{wid}/user-tables").json()["sheets"]["Inputs"][0]["range"] == "A3:B7"
    client.post(f"/api/workbooks/{wid}/rebuild")
    assert _tables(client, wid, "Inputs")[0]["source"] == "user"
    assert client.get(f"/api/workbooks/{wid}/summary").json()["user_tables"]["Inputs"][0]["range"] == "A3:B7"
    # other sheets keep automatic detection
    assert [t["source"] for t in _tables(client, wid, "Quote")] == ["detected"]


def test_user_table_header_and_total_rows(client, wid):
    client.put(f"/api/workbooks/{wid}/user-tables/Quote", json={"tables": [{"range": "A1:J22", "header_rows": 1, "total": True}]})
    (t,) = _tables(client, wid, "Quote")
    assert (t["header_row"], t["row_start"], t["row_end"], t["total_row"], t["key_col"]) == (1, 2, 21, 22, 1)
    s = client.get(f"/api/workbooks/{wid}/cell", params={"id": "Quote!J13"}).json()["cell"]["semantic"]
    assert s == 'quote["Cover plate"].margin'


def test_resetting_a_sheet_restores_detection(client, wid):
    client.put(f"/api/workbooks/{wid}/user-tables/Quote", json={"tables": []})
    assert _tables(client, wid, "Quote") == []
    client.delete(f"/api/workbooks/{wid}/user-tables/Quote")
    assert [t["source"] for t in _tables(client, wid, "Quote")] == ["detected"]
    assert client.get(f"/api/workbooks/{wid}/user-tables").json() == {"sheets": {}}


@pytest.mark.parametrize("tables, kind", [
    ([{"range": "A1:C2", "header_rows": 1, "total": True}], "too_small"),
    ([{"range": "A1:C9"}, {"range": "C5:E9"}], "overlap"),
    ([{"range": "not a range"}], "bad_range"),
])
def test_bad_user_tables_are_refused(client, wid, tables, kind):
    r = client.put(f"/api/workbooks/{wid}/user-tables/Quote", json={"tables": tables})
    assert r.status_code == 400 and r.json()["detail"]["kind"] == kind
    assert [t["source"] for t in _tables(client, wid, "Quote")] == ["detected"]


def test_unknown_sheet(client, wid):
    r = client.put(f"/api/workbooks/{wid}/user-tables/Nope", json={"tables": []})
    assert r.status_code == 404
