import io
import json

import openpyxl
import pytest
from fastapi.testclient import TestClient

import sheetir.web.app
from sheetir.catalog.project import alias_for
from sheetir.web.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    return TestClient(app)


def _sales_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Product", "Qty", "Price", "Amount"])
    for i, (p, q, pr) in enumerate([("Heat sink 40", 5, 12), ("Shield can", 3, 8), ("Gear housing", 7, 20)], start=2):
        ws.append([p, q, pr, f"=B{i}*C{i}"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, name, data, project=None):
    form = {"project": project} if project else {}
    r = client.post("/api/workbooks", files={"file": (name, data)}, data=form)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _projects(client):
    return {p["id"]: p for p in client.get("/api/projects").json()}


def test_default_project_holds_every_unclaimed_workbook(client):
    wid = client.post("/api/workbooks/sample").json()["id"]
    ps = client.get("/api/projects").json()
    assert [p["id"] for p in ps] == ["default"] and ps[0]["workbooks"] == [wid]


def test_upload_into_a_project_and_move_between_projects(client):
    p = client.post("/api/projects", json={"name": "  Q1   report "}).json()
    assert p["name"] == "Q1 report"
    sales = _upload(client, "sales.xlsx", _sales_xlsx(), p["id"])
    quote = client.post("/api/workbooks/sample").json()["id"]
    ps = _projects(client)
    assert ps[p["id"]]["workbooks"] == [sales] and ps["default"]["workbooks"] == [quote]

    client.post(f"/api/projects/{p['id']}/workbooks", json={"workbook": quote, "move_from": "default"})
    ps = _projects(client)
    assert set(ps[p["id"]]["workbooks"]) == {sales, quote} and ps["default"]["workbooks"] == []

    client.delete(f"/api/projects/{p['id']}/workbooks/{quote}")
    assert _projects(client)["default"]["workbooks"] == [quote]  # in no project: back in default


def test_deleting_a_project_keeps_its_files(client):
    p = client.post("/api/projects", json={"name": "tmp"}).json()
    sales = _upload(client, "sales.xlsx", _sales_xlsx(), p["id"])
    assert client.delete("/api/projects/default").status_code == 400
    assert client.delete(f"/api/projects/{p['id']}").json() == {"deleted": p["id"]}
    assert list(_projects(client)) == ["default"] and _projects(client)["default"]["workbooks"] == [sales]
    assert client.get(f"/api/workbooks/{sales}/summary").status_code == 200


def test_rename_and_bad_ids(client):
    p = client.post("/api/projects", json={"name": "a"}).json()
    assert client.patch(f"/api/projects/{p['id']}", json={"name": "b"}).json()["name"] == "b"
    assert client.post("/api/projects", json={"name": "   "}).status_code == 400
    assert client.patch("/api/projects/NOT-AN-ID", json={"name": "b"}).status_code == 400
    assert client.patch("/api/projects/ffffffffffff", json={"name": "b"}).status_code == 404
    r = client.post("/api/workbooks", files={"file": ("s.xlsx", _sales_xlsx())}, data={"project": "ffffffffffff"})
    assert r.status_code == 404


def test_an_edit_keeps_the_new_version_in_the_project(client):
    p = client.post("/api/projects", json={"name": "q"}).json()
    quote = client.post(f"/api/workbooks/sample?project={p['id']}").json()["id"]
    new = client.post(f"/api/workbooks/{quote}/edits", json={"edits": {"Inputs!B3": "9"}}).json()["id"]
    assert new != quote and set(_projects(client)[p["id"]]["workbooks"]) == {quote, new}
    client.delete(f"/api/workbooks/{new}")
    assert _projects(client)[p["id"]]["workbooks"] == [quote]


def test_alias_is_sql_safe_and_unique():
    taken = set()
    assert alias_for("Doanh thu Q1 (đã chốt).xlsx", taken) == "doanh_thu_q1_da_chot"
    assert alias_for("doanh thu q1 đã chốt.xlsx", taken) == "doanh_thu_q1_da_chot_2"
    assert alias_for("2024.xlsx", taken) == "f_2024"
    assert alias_for("報表.xlsx", taken) == "file"


class _AgentLLM:
    model = "scripted-agent"

    def __init__(self, *actions, route="data"):
        self.actions, self.route, self.systems = list(actions), route, []

    def json(self, system, user, schema, name="answer"):
        if name == "route":
            return {"route": self.route}
        raise AssertionError(name)

    def chat_json(self, messages, schema, **kw):
        self.systems.append(messages[0]["content"])
        return schema(**self.actions.pop(0))


def _stream(client, pid, body):
    with client.stream("POST", f"/api/projects/{pid}/chat/stream", json=body) as res:
        text = "".join(res.iter_text())
    blocks = text.strip().split("\n\n")
    last = blocks[-1]
    assert last.startswith("event: final"), text
    return json.loads(last.split("data: ", 1)[1])


def test_project_agent_queries_across_files(client, monkeypatch):
    p = client.post("/api/projects", json={"name": "both"}).json()
    quote = client.post(f"/api/workbooks/sample?project={p['id']}").json()["id"]
    sales = _upload(client, "sales.xlsx", _sales_xlsx(), p["id"])
    sales_table = client.get(f"/api/workbooks/{sales}/catalog").json()["cards"][0]["sql_name"]
    sql = (f'SELECT (SELECT COUNT(*) FROM quotation_sample__t_quote__t1) AS quotes, '
           f'(SELECT SUM(qty) FROM "sales__{sales_table}") AS sold')
    llm = _AgentLLM({"thought": "both", "tool": "run_sql", "args": {"sql": sql}},
                    {"thought": "done", "tool": "final_answer",
                     "args": {"answer": "20 quote lines; 15 units sold.", "result_ids": ["r1"]}})
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: llm)
    r = _stream(client, p["id"], {"message": "How many quote lines and units sold?", "lang": "en", "workbook": quote})
    assert r["source"] == "agent" and r["grounded"], r
    assert r["results"][0]["rows"] == [[20, 15]]
    assert {x["workbook_id"] for x in r["provenance"]} == {quote, sales}
    assert "quotation_sample: quotation_sample.xlsx" in llm.systems[0] and "sales: sales.xlsx" in llm.systems[0]


def test_project_agent_cannot_reach_the_attached_databases(client, monkeypatch):
    p = client.post("/api/projects", json={"name": "both"}).json()
    client.post(f"/api/workbooks/sample?project={p['id']}")
    _upload(client, "sales.xlsx", _sales_xlsx(), p["id"])
    llm = _AgentLLM({"thought": "x", "tool": "run_sql", "args": {"sql": "SELECT * FROM w0.main.t_quote__t1"}},
                    {"thought": "x", "tool": "run_sql", "args": {"sql": "SELECT * FROM t_quote__t1"}},
                    {"thought": "x", "tool": "final_answer", "args": {"answer": "cannot", "result_ids": []}})
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: llm)
    r = _stream(client, p["id"], {"message": "?", "lang": "en"})
    assert [("error" in s["result"]) for s in r["steps"][:2]] == [True, True]


def test_project_chat_answers_structure_and_cells_about_the_file_in_view(client, monkeypatch):
    p = client.post("/api/projects", json={"name": "both"}).json()
    quote = client.post(f"/api/workbooks/sample?project={p['id']}").json()["id"]
    _upload(client, "sales.xlsx", _sales_xlsx(), p["id"])
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: _AgentLLM())
    r = _stream(client, p["id"], {"message": "quote!j13", "lang": "en", "workbook": quote})
    assert r["source"] == "rules" and r["cells"] == ["Quote!J13"] and r["workbook"] == quote


def test_project_chat_without_a_model_uses_the_rules_on_the_file_in_view(client):
    p = client.post("/api/projects", json={"name": "q"}).json()
    quote = client.post(f"/api/workbooks/sample?project={p['id']}").json()["id"]
    r = _stream(client, p["id"], {"message": "What issues are there?", "lang": "en"})
    assert r["source"] == "rules" and r["workbook"] == quote and "Quote!I13" in r["reply"]


def test_a_workbook_can_be_deleted_after_it_was_viewed(client):
    """Viewing leaves sqlite connections for the garbage collector; on Windows they lock ir.db until collected."""
    wid = client.post("/api/workbooks/sample").json()["id"]
    for url in ("summary", "sheet?name=Quote", "cell?id=Quote!J13", "graph", "charts", "issues", "layout"):
        assert client.get(f"/api/workbooks/{wid}/{url}").status_code == 200
    assert client.delete(f"/api/workbooks/{wid}").json() == {"deleted": wid}
    assert client.get("/api/workbooks").json() == []
