import pytest
from fastapi.testclient import TestClient

from sheetir.web.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    return TestClient(app)


@pytest.fixture
def wid(client):
    return client.post("/api/workbooks/sample").json()["id"]


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


def test_download_rebuild_delete(client, wid, tmp_path):
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
