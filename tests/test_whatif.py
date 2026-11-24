"""Twin compute engine (sheetir.whatif) and project history (sheetir.history, sheetir.web.history_api)."""

import io
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from sheetir import build_ir
from sheetir.history import History, HistoryError
from sheetir.twin import check
from sheetir.web.app import app
from sheetir.whatif import Engine, WhatIfError, as_typed, typed

from test_twin import _workbook


@pytest.fixture
def db(tmp_path):
    book = _workbook(tmp_path / "calc.xlsx")
    build_ir(book, tmp_path / "calc.db", verify=False)
    check(tmp_path / "calc.db", book, scenarios=3)
    return tmp_path / "calc.db"


# ---- engine -----------------------------------------------------------------------------------------------------

def test_values_are_read_as_typed_in_excel():
    assert typed("12%") == 0.12 and typed("1,234.5") == 1234.5 and typed(" 7 ") == 7.0
    assert typed("true") is True and typed("") is None and typed("AL") == "AL" and typed(3) == 3.0
    with pytest.raises(WhatIfError):
        typed("=A1+1")
    assert (as_typed(3.0), as_typed(2.5), as_typed(True), as_typed(None)) == ("3", "2.5", "TRUE", "")


def test_scenario_lists_what_changed_and_never_touches_the_workbook(db):
    eng = Engine(db)
    r = eng.run({"Inputs!B1": 3})
    assert r["changes"] == [{"cell": "Inputs!B1", "label": "Rate", "old": 2.0, "new": 3.0}]
    got = {c["cell"]: (c["base"], c["value"], c["delta"], c["status"]) for c in r["changed"]}
    assert got["Calc!D5"] == (36.5, 54.75, 18.25, "verified")
    assert got["Calc!D8"][1] == 18.25 and "Calc!D7" not in got  # COUNTIF of "small" stays 2
    assert r["n_changed"] == len(r["changed"]) and not r["warnings"]
    assert eng.run({"inputs!b1": "3"})["fingerprint"] == r["fingerprint"]  # same scenario, same digest
    assert eng.run({})["n_changed"] == 0
    assert Engine(db).value(eng.key("Calc!D5")) == 36.5  # the stored result is unchanged


def test_scenario_refuses_formulas_and_unknown_cells(db):
    eng = Engine(db)
    for changes in ({"Calc!D2": 1}, {"Nope!A1": 1}, {"D2": 1}, {"Calc!A1:B2": 1}):
        with pytest.raises(WhatIfError):
            eng.run(changes)


def test_cells_that_cannot_follow_a_change_say_so(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"], ws["A2"] = 2, 5
    ws["B1"] = '=INDIRECT("S!A1")*2'   # not translated: keeps Excel's value
    ws["B2"] = "=B1+A2"                # reads it
    ws["B3"] = "=A1*10"                # translated and independent of B1
    wb.save(tmp_path / "w.xlsx")
    build_ir(tmp_path / "w.xlsx", tmp_path / "w.db", verify=False)
    eng = Engine(tmp_path / "w.db")
    assert eng.status[eng.key("S!B2")] == "not_recomputed" and eng.status[eng.key("S!B3")] == "unchecked"
    r = eng.run({"S!A1": 3})  # INDIRECT reads A1 without SheetIR seeing it: a general warning
    assert any("not_recomputed" in w for w in r["warnings"])
    r = eng.run({"S!A2": 6})  # B2 reads A2 and the untranslated B1
    assert any("read the changed cells" in w for w in r["warnings"])
    assert {c["cell"]: c["status"] for c in r["changed"]} == {"S!B2": "not_recomputed"}
    assert any("not checked" in w for w in r["warnings"])
    with pytest.raises(WhatIfError, match="could not translate"):
        eng.goal_seek("S!B2", 100, "S!A2")


def test_sensitivity_ranks_inputs_by_how_far_the_target_moves(db):
    r = Engine(db).sensitivity("Calc!D5", step=0.1)
    assert r["target"]["value"] == 36.5
    top = r["inputs"][0]
    assert (top["cell"], top["label"], top["target_low"], top["target_high"]) == ("Inputs!B1", "Rate", 32.85, 40.15)
    assert top["elasticity"] == 1.0
    assert any(s["cell"] == "Calc!B4" and "0" in s["reason"] for s in r["skipped"])  # qty 0 cannot move by 10 %
    with pytest.raises(WhatIfError):
        Engine(db).sensitivity("Inputs!B1")  # not a formula


def test_goal_seek_finds_the_input_value(db):
    eng = Engine(db)
    r = eng.goal_seek("Calc!D5", 100, "Inputs!B1")
    assert r["found"] and abs(r["target"]["achieved"] - 100) < 1e-6
    assert abs(r["changing"]["value"] - 100 / 18.25) < 1e-9
    changed = {c["cell"]: c["value"] for c in eng.run({"Inputs!B1": r["changing"]["value"]})["changed"]}
    assert changed["Calc!D5"] == pytest.approx(100)
    # a step function: secant stalls, bisection finds the edge
    r = eng.goal_seek("Calc!D7", 3, "Inputs!B1")  # COUNTIF(E,"small"): 3 once D2 <= 20
    assert r["found"] and r["changing"]["value"] < 20 / 13
    with pytest.raises(WhatIfError, match="does not read"):
        eng.goal_seek("Calc!D5", 1, "Materials!B2")


def test_compare_two_scenarios(db):
    c = Engine(db).compare({"Inputs!B1": 3}, {"Inputs!B1": 3, "Calc!B3": 4})
    assert c["inputs"] == [{"cell": "Calc!B3", "label": c["inputs"][0]["label"], "a": 3.0, "b": 4.0}]
    assert {x["cell"] for x in c["cells"]} >= {"Calc!D3", "Calc!D5"}


# ---- history store ----------------------------------------------------------------------------------------------

def test_history_is_append_only(tmp_path):
    h = History(tmp_path)
    wb = {"id": "a" * 12, "name": "x.xlsx", "sha256": "a" * 64}
    s = h.add("scenario", wb, "ann", "user", {"S!A1": 1})
    c = h.add("correction", wb, "ann", "user", {"S!A1": "2"}, params={"reason": "typo"})
    assert (s["ref"], s["status"], c["ref"], c["status"]) == ("S1", "recorded", "C2", "pending")
    con = sqlite3.connect(tmp_path / "history.db")
    for sql in ("UPDATE records SET note = 'x'", "DELETE FROM records", "DELETE FROM events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(sql)
    con.close()
    with pytest.raises(HistoryError, match="only ann"):
        h.review(2, "withdrawn", "bob")
    assert h.review(2, "rejected", "bob", "wrong cell")["status"] == "rejected"
    with pytest.raises(HistoryError, match="already rejected"):
        h.review(2, "approved", "bob")
    assert [r["ref"] for r in h.list()] == ["C2", "S1"]


# ---- API --------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path / "data"))
    client = TestClient(app)
    data = _workbook(tmp_path / "calc.xlsx").read_bytes()
    wid = client.post("/api/workbooks", files={"file": ("calc.xlsx", data)}).json()["id"]
    assert client.post(f"/api/workbooks/{wid}/twin", json={"scenarios": 2}).status_code == 200
    return client, wid


def test_api_scenarios_continue_compare_replay_and_export(api):
    client, wid = api
    base = "/api/projects/default"
    s1 = client.post(f"{base}/scenarios", json={"workbook": wid, "changes": {"Inputs!B1": 3}, "note": "rate 3",
                                                 "by": "ann"}).json()
    assert s1["ref"] == "S1" and s1["author"] == "ann" and s1["twin_status"] == "verified"
    assert s1["result"]["changed"][0]["status"] == "verified"
    s2 = client.post(f"{base}/scenarios", json={"workbook": wid, "parent": "S1", "changes": {"Calc!B3": 4}}).json()
    assert s2["parent"] == 1 and s2["changes"] == {"Inputs!B1": 3, "Calc!B3": 4}
    s3 = client.post(f"{base}/scenarios", json={"workbook": wid, "parent": "S2", "reset": ["Inputs!B1"]}).json()
    assert s3["changes"] == {"Calc!B3": 4}
    preview = client.post(f"{base}/scenarios", json={"workbook": wid, "changes": {"Inputs!B1": 1}, "save": False})
    assert preview.json()["saved"] is False
    assert client.post(f"{base}/scenarios", json={"workbook": wid, "changes": {"Calc!D2": 1}}).status_code == 400

    listed = client.get(f"{base}/history").json()["records"]
    assert [r["ref"] for r in listed] == ["S3", "S2", "S1"] and listed[0]["summary"] == "1 cell changed"
    cmp = client.get(f"{base}/history/compare", params={"a": "S1", "b": "S2"}).json()
    assert cmp["same_workbook"] and cmp["inputs"][0]["cell"] == "Calc!B3"

    again = client.post(f"{base}/history/S1/replay").json()
    assert again["same"] is True
    assert client.get(f"{base}/history/S1").json()["events"][0]["action"] == "replayed"

    x = client.get(f"{base}/history/S2/export")
    assert x.status_code == 200 and "calc.S2.xlsx" in x.headers["content-disposition"]
    book = load_workbook(io.BytesIO(x.content))
    assert book["Inputs"]["B1"].value == 3 and book["Calc"]["B3"].value == 4
    assert book["Calc"]["D2"].value == "=B2*C2*Rate"  # formulas are kept; Excel recalculates on open


def test_api_sensitivity_and_goal_seek_are_recorded(api):
    client, wid = api
    base = "/api/projects/default"
    s = client.post(f"{base}/sensitivity", json={"workbook": wid, "target": "Calc!D5", "step": 0.2}).json()
    assert s["kind"] == "sensitivity" and s["result"]["inputs"][0]["cell"] == "Inputs!B1"
    g = client.post(f"{base}/goal-seek", json={"workbook": wid, "target": "Calc!D5", "goal": 73,
                                                "changing": "Inputs!B1"}).json()
    assert g["result"]["found"] and g["changes"]["Inputs!B1"] == pytest.approx(4)
    nxt = client.post(f"{base}/scenarios", json={"workbook": wid, "parent": g["ref"], "changes": {}}).json()
    assert nxt["result"]["outputs"] or nxt["result"]["changed"]
    assert client.post(f"{base}/history/{g['ref']}/replay").json()["same"] is True
    assert "goal seek" in client.get(f"{base}/history/{g['ref']}").json()["summary"]


def test_api_corrections_are_confirmed_and_become_the_next_version(api):
    client, wid = api
    base = "/api/projects/default"
    c = client.post(f"{base}/corrections", json={"workbook": wid, "edits": {"Inputs!B1": "3"},
                                                  "reason": "rate was updated", "by": "ann"}).json()
    assert c["ref"] == "C1" and c["status"] == "pending" and c["params"]["old"] == {"Inputs!B1": "2"}
    assert c["result"]["impact"]["n_changed"] > 0
    assert client.post(f"{base}/corrections", json={"workbook": wid, "edits": {"Inputs!B1": "3"},
                                                     "reason": " "}).status_code == 400
    assert client.post(f"{base}/history/C1/reject", json={"by": "ann"}).status_code == 400  # needs a note
    ok = client.post(f"{base}/history/C1/approve", json={"by": "Ann", "note": "checked with finance"}).json()
    assert ok["status"] == "approved" and ok["applied_as"] and ok["applied_as"] != wid
    v2 = ok["applied_as"]
    cat = {w["id"]: w for w in client.get("/api/workbooks").json()}
    assert (cat[v2]["version"], cat[v2]["previous"]) == (2, wid)
    eng = Engine(_db(v2))
    assert eng.value(eng.key("Inputs!B1")) == 3.0
    assert client.post(f"{base}/history/C1/approve", json={"by": "cat"}).status_code == 409  # already approved

    # a proposal on the version before is refused at approval: the data moved on
    old = client.post(f"{base}/corrections", json={"workbook": wid, "edits": {"Calc!B2": "5"}, "reason": "qty",
                                                    "by": "ann"}).json()
    r = client.post(f"{base}/history/{old['ref']}/approve", json={"by": "bob"})
    assert r.status_code == 409 and v2 in r.json()["detail"]
    assert client.post(f"{base}/history/{old['ref']}/withdraw", json={"by": "bob"}).status_code == 403
    assert client.post(f"{base}/history/{old['ref']}/withdraw", json={"by": "ann"}).json()["status"] == "withdrawn"

    # bring version 1 back: a revert proposal, approved by someone else, becomes version 3
    rv = client.post(f"{base}/corrections/revert", json={"workbook": v2, "to": wid, "reason": "wrong rate",
                                                         "by": "bob"}).json()
    assert rv["kind"] == "revert" and rv["changes"] == {"Inputs!B1": "2"} and rv["params"]["to_name"] == "v1"
    back = client.post(f"{base}/history/{rv['ref']}/approve", json={"by": "ann"}).json()
    v3 = back["applied_as"]
    eng = Engine(_db(v3))
    assert eng.value(eng.key("Inputs!B1")) == 2.0
    statuses = {r["ref"]: r["status"] for r in client.get(f"{base}/history").json()["records"]}
    assert statuses == {"C1": "approved", old["ref"]: "withdrawn", rv["ref"]: "approved"}


def _db(wid):
    return Path(os.environ["SHEETIR_DATA"]) / wid / "ir.db"


def test_api_scenario_reapplied_on_a_new_version(api):
    client, wid = api
    base = "/api/projects/default"
    s = client.post(f"{base}/scenarios", json={"workbook": wid, "changes": {"Inputs!B1": 3, "Calc!B2": 9}}).json()
    c = client.post(f"{base}/corrections", json={"workbook": wid, "edits": {"Calc!B2": "=1+1"},
                                                  "reason": "qty is computed", "by": "ann"}).json()
    v2 = client.post(f"{base}/history/{c['ref']}/approve", json={"by": "bob"}).json()["applied_as"]
    r = client.post(f"{base}/history/{s['ref']}/reapply", json={"to": v2}).json()
    assert r["reapplied"] is False and [x["cell"] for x in r["checks"] if not x["ok"]] == ["Calc!B2"]
    r = client.post(f"{base}/history/{s['ref']}/reapply", json={"to": v2, "force": True}).json()
    assert r["reapplied"] and r["record"]["workbook"] == v2 and r["record"]["changes"] == {"Inputs!B1": 3}
    assert r["record"]["parent"] == s["id"]


def test_api_history_belongs_to_the_project(api):
    client, wid = api
    pid = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/scenarios", json={"workbook": wid, "changes": {"Inputs!B1": 3}})
    assert r.status_code == 404  # the workbook is not in that project
    assert client.get(f"/api/projects/{pid}/history").json() == {"records": []}
    assert client.get("/api/projects/nope/history").status_code == 404


# ---- agent ------------------------------------------------------------------------------------------------------

def test_agent_answers_what_if_from_the_twin_and_records_it(db, tmp_path):
    from sheetir.agent.loop import answer
    from sheetir.agent.whatif_tools import WhatIfTools
    from sheetir.catalog.store import build_catalog
    from test_agent import ScriptedAgent

    cat = build_catalog(db, tmp_path)
    h = History(tmp_path / "project")
    tools = WhatIfTools([("abc123abc123", "calc.xlsx", db)], h, "ann", "What if the rate were 3?")
    llm = ScriptedAgent(
        {"thought": "find the rate", "tool": "model_cells", "args": {"query": "rate"}},
        {"thought": "change it", "tool": "what_if", "args": {"changes": {"Inputs!B1": 3}, "outputs": ["Calc!D5"]}},
        {"thought": "formula", "tool": "what_if", "args": {"changes": {"Calc!D5": 3}}},
        {"thought": "done", "tool": "final_answer",
         "args": {"answer": "With a rate of 3 the total Calc!D5 goes from 36.5 to 54.75 (scenario S1).",
                  "result_ids": ["S1"]}})
    r = answer("What if the rate were 3?", cat, llm, whatif=tools)
    assert r.grounded and r.stopped is None
    assert [s.tool for s in r.steps] == ["model_cells", "what_if", "what_if", "final_answer"]
    assert r.steps[0].result["rows"][0][:3] == ["input", "Inputs!B1", "Rate"]
    assert "formula" in r.steps[2].result["error"]
    assert r.results[0]["result_id"] == "S1" and ["Calc!D5", r.results[0]["rows"][0][1], 36.5, 54.75, 18.25,
                                                     "verified"] in r.results[0]["rows"]
    (rec,) = h.list()
    assert (rec["ref"], rec["source"], rec["author"], rec["note"]) == ("S1", "agent", "ann", "What if the rate were 3?")
    assert tools.records == ["S1"] and r.provenance == []


def test_agent_without_a_twin_says_the_tool_is_missing(db, tmp_path):
    from sheetir.agent.tools import Toolbox
    from sheetir.catalog.store import build_catalog

    t = Toolbox(build_catalog(db, tmp_path))
    assert "not available" in t.dispatch("what_if", {"changes": {"Inputs!B1": 3}})["error"]


def test_agent_proposes_a_correction_that_only_the_user_applies(api):
    from sheetir.agent.loop import answer
    from sheetir.agent.whatif_tools import WhatIfTools
    from sheetir.catalog.store import build_catalog
    from test_agent import ScriptedAgent

    client, wid = api
    db = _db(wid)
    h = History(db.parent.parent / "projects" / "default")
    tools = WhatIfTools([(wid, "calc.xlsx", db)], h, "ann", "The rate should be 3, please fix it")
    llm = ScriptedAgent(
        {"thought": "propose", "tool": "propose_correction",
         "args": {"edits": {"Inputs!B1": "3"}, "reason": "the rate should be 3"}},
        {"thought": "done", "tool": "final_answer",
         "args": {"answer": "I proposed C1: Inputs!B1 from 2 to 3, so Calc!D5 would go from 36.5 to 54.75. "
                            "It is applied once you approve it.", "result_ids": ["C1"]}})
    r = answer("The rate should be 3, please fix it", build_catalog(db, db.parent), llm, whatif=tools)
    assert r.grounded and tools.records == ["C1"]
    assert r.results[0]["rows"][0] == ["edit", "Inputs!B1", "Rate", 2.0, 3.0]
    effects = {row[1]: row[3:] for row in r.results[0]["rows"] if row[0] == "effect"}
    assert effects["Calc!D5"] == [36.5, 54.75]
    base = "/api/projects/default"
    rec = client.get(f"{base}/history/C1").json()
    assert (rec["status"], rec["source"], rec["author"]) == ("pending", "agent", "ann")
    assert Engine(db).value(Engine(db).key("Inputs!B1")) == 2.0  # nothing changed yet
    from sheetir.agent.protocol import AgentAction
    assert not any("approve" in t for t in AgentAction.model_fields["tool"].annotation.__args__)  # no tool to approve
    ok = client.post(f"{base}/history/C1/approve", json={"by": "ann"}).json()
    new = Engine(_db(ok["applied_as"]))
    assert ok["status"] == "approved" and new.value(new.key("Inputs!B1")) == 3.0
