"""Per-user data (sheetir/web/users.py): behind a sign-in gateway, users never see each other's data."""

import json

import pytest
from fastapi.testclient import TestClient

import sheetir.web.app
from sheetir.web.app import app
from sheetir.web.users import user_key

ALICE = {"Oidc_claim_email": "Alice@Example.com"}
BOB = {"Oidc_claim_email": "bob@example.com"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    monkeypatch.setenv("SHEETIR_USER_HEADER", "Oidc_claim_email")
    return TestClient(app)


def _ids(client, headers=None):
    return [w["id"] for w in client.get("/api/workbooks", headers=headers).json()]


def test_users_do_not_see_each_others_workbooks(client, tmp_path):
    wid = client.post("/api/workbooks/sample", headers=ALICE).json()["id"]
    assert _ids(client, ALICE) == [wid]
    assert _ids(client, BOB) == []
    assert client.get(f"/api/workbooks/{wid}/summary", headers=BOB).status_code == 404
    assert (tmp_path / "users" / "alice@example.com" / wid / "ir.db").exists()


def test_projects_and_documents_are_per_user(client):
    pid = client.post("/api/projects", json={"name": "Q4 budget"}, headers=ALICE).json()["id"]
    client.put(f"/api/projects/{pid}/instructions", json={"instructions": "Amounts are in TWD."}, headers=ALICE)
    assert "Q4 budget" in [p["name"] for p in client.get("/api/projects", headers=ALICE).json()]
    assert "Q4 budget" not in [p["name"] for p in client.get("/api/projects", headers=BOB).json()]
    assert client.get(f"/api/projects/{pid}/knowledge", headers=BOB).status_code == 404


def test_without_the_header_the_shared_data_is_used(client, tmp_path):
    wid = client.post("/api/workbooks/sample").json()["id"]
    assert (tmp_path / wid / "ir.db").exists()
    assert _ids(client) == [wid] and _ids(client, ALICE) == []


def test_users_are_not_separated_unless_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    monkeypatch.delenv("SHEETIR_USER_HEADER", raising=False)
    c = TestClient(app)
    wid = c.post("/api/workbooks/sample", headers=ALICE).json()["id"]
    assert _ids(c, BOB) == [wid]
    assert c.get("/api/me", headers=ALICE).json() == {"user": None, "separated": False}


def test_me_names_the_user(client):
    assert client.get("/api/me", headers=ALICE).json() == {"user": "alice@example.com", "separated": True}
    assert client.get("/api/me").json() == {"user": None, "separated": True}


def test_a_streamed_answer_runs_in_the_users_data(client, monkeypatch):
    """The stream's worker thread must keep the request's user, or it would look in the shared data and 404."""
    from tests.test_web import _actions, _AgentLLM
    wid = client.post("/api/workbooks/sample", headers=ALICE).json()["id"]  # before the scripted model is in place
    monkeypatch.setattr(sheetir.web.app, "get_llm", lambda: _AgentLLM(*_actions()))
    with client.stream("POST", f"/api/workbooks/{wid}/chat/stream", headers=ALICE,
                       json={"message": "How many products use AL?", "lang": "en"}) as res:
        body = "".join(res.iter_text())
    final = json.loads(body.strip().split("\n\n")[-1].split("data: ", 1)[1])
    assert final["source"] == "agent" and final["reply"] == "4 products use AL."


@pytest.mark.parametrize("value, key", [
    ("Daniel.Pham@iir.csie.ncku.edu.tw", "daniel.pham@iir.csie.ncku.edu.tw"),
    ("../../etc", ".._.._etc"),
    ("a/b\\c", "a_b_c"),
    ("..", None),
    ("  ", None),
])
def test_user_keys_are_plain_directory_names(value, key):
    assert user_key(value) == key
