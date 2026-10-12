"""The audit report (sheetir.report) and its downloads: every finding is in it, in both formats and both languages."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from sheetir import build_ir
from sheetir.report import audit_report, to_html, to_markdown
from sheetir.sample import make_quotation
from sheetir.web.app import app

PLANTED = ["Quote!I13", "Quote!G17", "Inputs!B9", "Quote!E17", "Report!B1", "Report!B6"]


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("report")
    res = build_ir(make_quotation(tmp / "quotation.xlsx"), tmp / "q.db")
    return sqlite3.connect(res.db_path)


def test_report_lists_every_issue_by_severity(con):
    r = audit_report(con, "en")
    n = con.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    assert sum(r["counts"].values()) == n == sum(len(k["items"]) for f in r["findings"] for k in f["kinds"])
    assert [f["severity"] for f in r["findings"]] == ["high", "medium", "low"]
    assert r["status"] == "partial" and r["stats"]["formulas"] == 108 and not r["has_cached"]


@pytest.mark.parametrize("lang, words", [("zh", ["稽核報告", "高嚴重度 2 個", "同一欄的公式不一致", "部分理解"]),
                                         ("en", ["Audit report", "2 high severity", "Inconsistent formula in a column",
                                                 "Partly understood"])])
def test_markdown_and_html_say_the_same_thing(con, lang, words):
    r = audit_report(con, lang)
    md, page = to_markdown(r), to_html(r)
    for text in PLANTED + words:
        assert text in md and text in page, text
    assert "`=H13*1.12+Inputs!$B$6`" in md  # formulas are code, so Markdown does not mangle them
    assert page.startswith("<!doctype html>") and "http" not in page  # self-contained: nothing fetched


def test_html_escapes_workbook_content(con):
    r = audit_report(con, "en")
    r["findings"][0]["kinds"][0]["items"][0]["detail"] = "<script>alert(1)</script>"
    assert "<script>alert" not in to_html(r) and "&lt;script&gt;" in to_html(r)


def test_reports_are_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    client = TestClient(app)
    wid = client.post("/api/workbooks/sample").json()["id"]
    names = {f["name"]: f["format"] for f in client.get(f"/api/workbooks/{wid}/exports").json()}
    assert names["audit_report.html"] == "HTML" and names["audit_report.md"] == "Markdown"

    md = client.get(f"/api/workbooks/{wid}/exports/audit_report.md?lang=en")
    assert md.headers["content-disposition"].startswith("attachment") and "audit_report.en.md" in md.headers["content-disposition"]
    assert md.text.startswith("# Audit report")
    page = client.get(f"/api/workbooks/{wid}/exports/audit_report.html?inline=true")
    assert page.headers["content-disposition"].startswith("inline") and "稽核報告" in page.text
    assert page.headers["content-type"].startswith("text/html")
