"""Grounded Q&A (sheetir.understand.ask) with a scripted stand-in for the language model: routing, the enforced query,
the issue list, the reply language and the number check are SheetIR's job, so they are tested without a model."""

import sqlite3

import pytest

from sheetir.build import build_ir
from sheetir.sample import make_quotation
from sheetir.understand import ISSUES_SQL, NO_QUERY, question_language, ungrounded_numbers
from sheetir.understand import ask as _ask


class FakeLLM:
    """Returns the scripted replies in order and records every call."""
    model = "fake"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def json(self, system, user, schema, name="answer"):
        self.calls.append({"system": system, "user": user, "name": name})
        assert self.replies, f"unexpected extra call:\n{user[-300:]}"
        return self.replies.pop(0)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    d = tmp_path_factory.mktemp("qa")
    build_ir(make_quotation(d / "q.xlsx"), d / "q.db", verify=False)
    return d / "q.db"


def ask(db, question, llm, lang="en"):
    return _ask(sqlite3.connect(f"file:{db}?mode=ro", uri=True), question, llm, lang)


def plan(route, answer="", sql=""):
    return {"route": route, "answer": answer, "sql": sql}


CU_SQL = "SELECT product FROM table_quote WHERE material = 'CU'"


def test_data_question_without_a_query_is_asked_again(db):
    llm = FakeLLM(plan("data", answer="Motor housing A, Motor housing B"), plan("data", sql=CU_SQL),
                  {"answer": "Motor housing A, Motor housing B, Busbar 100A, Busbar 250A, Terminal block, Cable lug kit, "
                             "Coil assembly, Contact spring, Ground strap"})
    r = ask(db, "Which products use material CU?", llm)
    assert r["route"] == "data" and r["sql"] == CU_SQL and r["grounded"]
    assert "Ground strap" in r["reply"] and len(llm.calls) == 3
    assert "Ground strap" in llm.calls[2]["user"]  # the answer was written from the query result


def test_data_question_is_never_answered_from_the_sketch(db):
    llm = FakeLLM(plan("data", answer="Motor housing A"), plan("data"))
    r = ask(db, "Which products use material CU?", llm)
    assert r["reply"] == NO_QUERY["en"] and r["sql"] is None


def test_issues_come_from_the_database_with_counts(db):
    llm = FakeLLM(plan("issues", sql="SELECT 1"), {"answer": "There are 5 medium issues."})
    r = ask(db, "How many medium severity issues are there?", llm)
    assert r["route"] == "issues" and r["sql"] == ISSUES_SQL and r["grounded"]
    data = llm.calls[1]["user"]
    assert "Quote!I13" in data and '"by_severity"' in data and '"medium": 5' in data


def test_numbers_not_in_the_result_are_rewritten_then_replaced_by_the_table(db):
    llm = FakeLLM(plan("data", sql="SELECT ROUND(SUM(steel), 2) AS total FROM table_quote"),
                  {"answer": "The total is 42 kg."}, {"answer": "The total is 41 kg."})
    r = ask(db, "What is the total steel use?", llm)
    assert "42" in llm.calls[2]["user"]  # the rewrite request names the number
    assert "| total |" in r["reply"] and "35.95" in r["reply"] and r["grounded"]


def test_structure_answer_keeps_numbers_from_the_sketch_and_flags_others(db):
    r = ask(db, "What is the copper price?", FakeLLM(plan("structure", answer="Copper costs 285 TWD/kg (Inputs!B3).")))
    assert r["grounded"] and r["cells"] == ["Inputs!B3"] and r["sql"] is None
    llm = FakeLLM(plan("structure", answer="Copper costs 999 TWD/kg."), {"answer": "Copper costs 998 TWD/kg."})
    r = ask(db, "What is the copper price?", llm)
    assert not r["grounded"] and r["ungrounded"] == ["998"] and "998" in r["reply"].split("\n\n> ")[1]


def test_reply_language_follows_the_question(db):
    llm = FakeLLM(plan("structure", answer="Quote!J13 is the margin of the Cover plate."),
                  {"answer": "Quote!J13 是 Cover plate 的毛利率。"})
    r = ask(db, "Quote!J13 是什麼意思？", llm, lang="en")
    assert r["lang"] == "zh" and r["reply"].startswith("Quote!J13 是")
    assert "Traditional Chinese" in llm.calls[0]["system"]
    assert "Rewrite this answer in Traditional Chinese" in llm.calls[1]["user"]


def test_question_language():
    assert question_language("哪些產品使用 CU？", "en") == "zh"
    assert question_language("Which products use AL?", "zh") == "en"
    assert question_language("Quote!J13", "zh") == "zh" and question_language("Quote!J13", "en") == "en"


def test_ungrounded_numbers():
    src = ['{"rows": [["Busbar 250A", 2.9], ["Heat sink 40", 0.15]]}']
    assert ungrounded_numbers("共 2 項：Busbar 250A 用 2.9 公斤", src, (2,)) == []
    assert ungrounded_numbers("Heat sink 40 has a 15% share", src) == []
    assert ungrounded_numbers("1. Quote!J13 reads RC[-1] and $H$1", src) == []
    assert ungrounded_numbers("價格乘以 1.15 再加 120", src) == ["1.15", "120"]
