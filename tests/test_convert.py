"""Workbooks that are not .xlsx (.csv, .xls, .ods) are converted, parsed and checked like any other."""

import datetime as dt
import json
import sqlite3
import zipfile

import pytest
import xlwt
from fastapi.testclient import TestClient

from sheetir import build_ir
from sheetir.convert import _odf_formula
from sheetir.web.app import app


def _cells(db):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT cell_id, vtype, value, cached, formula FROM cells").fetchall()
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    con.close()
    return {r[0]: (r[1], json.loads(r[2]) if r[2] else None, json.loads(r[3]) if r[3] else None, r[4]) for r in rows}, meta


def test_csv(tmp_path):
    p = tmp_path / "orders.csv"
    p.write_bytes("Item,Qty,Price,Date,Code,Paid\nPen,2,1.5,2026-01-05,007,TRUE\n筆,3,2,2026-01-06,042,false\n"
                  .encode("utf-8-sig"))
    res = build_ir(p, tmp_path / "o.db")
    cells, meta = _cells(tmp_path / "o.db")
    assert json.loads(meta["load_warnings"]) == ["from_csv"] and json.loads(meta["sheets"]) == ["orders"]
    assert meta["source"].endswith("orders.csv")
    assert cells["orders!B2"][:2] == ("number", 2) and cells["orders!C2"][:2] == ("number", 1.5)
    assert cells["orders!D2"][0] == "date" and cells["orders!E2"][:2] == ("text", "007")
    assert cells["orders!F3"][:2] == ("bool", False) and cells["orders!A3"][1] == "筆"
    assert res.verification["status"] == "verified"


@pytest.mark.parametrize("text, delimiter, encoding", [("a;b\n1;2,5\n", ";", "utf-8"), ("a\tb\n1\t2\n", "\t", "utf-16"),
                                                       ("名稱,數量\n筆,1\n", ",", "cp950")])
def test_csv_dialects_and_encodings(tmp_path, text, delimiter, encoding):
    p = tmp_path / "x.csv"
    p.write_bytes(text.encode(encoding))
    build_ir(p, tmp_path / "x.db")
    cells, _ = _cells(tmp_path / "x.db")
    assert len(cells) == 4 and cells["x!A1"][1] in ("a", "名稱")


def test_xls_keeps_values_formatting_and_merges(tmp_path):
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Quote")
    bold = xlwt.easyxf("font: bold on")
    for c, h in enumerate(["Item", "Qty", "Price", "Amount"]):
        ws.write(0, c, h, bold)
    ws.write(1, 0, "Pen"), ws.write(1, 1, 2), ws.write(1, 2, 10), ws.write(1, 3, xlwt.Formula("B2*C2"))
    ws.write(2, 0, dt.datetime(2026, 1, 5), xlwt.easyxf(num_format_str="yyyy-mm-dd"))
    ws.write_merge(4, 4, 0, 2, "Total")
    wb.save(str(tmp_path / "q.xls"))

    res = build_ir(tmp_path / "q.xls", tmp_path / "q.db")
    cells, meta = _cells(tmp_path / "q.db")
    assert json.loads(meta["load_warnings"]) == ["from_xls", "formulas_dropped"]
    assert cells["Quote!B2"][:2] == ("number", 2) and cells["Quote!A3"][0] == "date"
    assert "Quote!D2" not in cells or cells["Quote!D2"][3] is None  # xlwt writes no result; the formula is lost
    con = sqlite3.connect(tmp_path / "q.db")
    assert con.execute("SELECT COUNT(*) FROM cells WHERE vtype = 'formula'").fetchone()[0] == 0
    con.close()
    assert res.verification["status"] == "verified"


_ODS_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
  xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
  xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" office:version="1.3">
 <office:automatic-styles>
  <style:style style:name="head" style:family="table-cell"><style:text-properties fo:font-weight="bold"/></style:style>
 </office:automatic-styles>
 <office:body><office:spreadsheet>
  <table:table table:name="Quote">
   <table:table-column table:number-columns-repeated="1024"/>
   <table:table-row>
    <table:table-cell table:style-name="head" office:value-type="string"><text:p>Item</text:p></table:table-cell>
    <table:table-cell table:style-name="head" office:value-type="string"><text:p>Qty</text:p></table:table-cell>
    <table:table-cell table:style-name="head" office:value-type="string"><text:p>Price</text:p></table:table-cell>
    <table:table-cell table:style-name="head" office:value-type="string"><text:p>Amount</text:p></table:table-cell>
    <table:table-cell table:number-columns-repeated="1020"/>
   </table:table-row>
   <table:table-row table:number-rows-repeated="2">
    <table:table-cell office:value-type="string"><text:p>Blue<text:s text:c="2"/>pen</text:p></table:table-cell>
    <table:table-cell office:value-type="float" office:value="2"><text:p>2</text:p></table:table-cell>
    <table:table-cell office:value-type="float" office:value="10"><text:p>10</text:p></table:table-cell>
    <table:table-cell table:formula="of:=[.B2]*[.C2]" office:value-type="float" office:value="20"><text:p>20</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell table:number-columns-spanned="3" office:value-type="string"><text:p>Total</text:p></table:table-cell>
    <table:covered-table-cell table:number-columns-repeated="2"/>
    <table:table-cell table:formula="of:=SUM([.D2:.D3])" office:value-type="float" office:value="40"><text:p>40</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell office:value-type="percentage" office:value="0.05"><text:p>5%</text:p></table:table-cell>
    <table:table-cell office:value-type="date" office:date-value="2026-01-05"><text:p>2026-01-05</text:p></table:table-cell>
    <table:table-cell table:formula="of:=IF([.D4]&gt;30;&quot;big;&quot;;[$Rates.A1])" office:value-type="string"><text:p>big;</text:p></table:table-cell>
   </table:table-row>
   <table:table-row table:number-rows-repeated="1048570"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>
  </table:table>
  <table:table table:name="Rates">
   <table:table-row><table:table-cell office:value-type="float" office:value="0.1"><text:p>0.1</text:p></table:table-cell></table:table-row>
  </table:table>
  <table:named-expressions>
   <table:named-range table:name="TaxRate" table:base-cell-address="$Rates.$A$1" table:cell-range-address="$Rates.$A$1"/>
  </table:named-expressions>
 </office:spreadsheet></office:body>
</office:document-content>"""


def _ods(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", _ODS_CONTENT)
    return path


def test_ods_keeps_formulas_results_styles_and_names(tmp_path):
    res = build_ir(_ods(tmp_path / "q.ods"), tmp_path / "q.db")
    cells, meta = _cells(tmp_path / "q.db")
    assert json.loads(meta["load_warnings"]) == ["from_ods"] and json.loads(meta["sheets"]) == ["Quote", "Rates"]
    assert cells["Quote!A2"][1] == "Blue  pen" and cells["Quote!A3"][1] == "Blue  pen"
    assert cells["Quote!D2"] == ("formula", None, 20, "=B2*C2")
    assert cells["Quote!D4"] == ("formula", None, 40, "=SUM(D2:D3)")
    assert cells["Quote!C5"][3] == '=IF(D4>30,"big;",Rates!A1)' and cells["Quote!C5"][2] == "big;"
    assert cells["Quote!A5"][1] == 0.05 and cells["Quote!B5"][0] == "date"
    con = sqlite3.connect(tmp_path / "q.db")
    assert con.execute("SELECT name FROM names").fetchall() == [("TaxRate",)]
    con.close()
    assert res.counts["formula_groups"] >= 1
    assert res.verification["status"] == "verified"


@pytest.mark.parametrize("odf, excel", [
    ("of:=SUM([.A1:.B2];[Sheet2.C3])", "=SUM(A1:B2,Sheet2!C3)"),
    ("of:=[$'My Sheet'.$A$1:.$B$2]", "='My Sheet'!$A$1:$B$2"),
    ('of:=CONCATENATE("a;b";[.A1])', '=CONCATENATE("a;b",A1)'),
    ("msoxl:=SUM(A1:A3)", "=SUM(A1:A3)"),
    ("of:=[.#REF!]", "=#REF!"),
])
def test_openformula_translation(odf, excel):
    assert _odf_formula(odf) == excel


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path / "data"))
    return TestClient(app)


def test_upload_csv_and_download_the_original(client):
    r = client.post("/api/workbooks", files={"file": ("orders.csv", b"Item,Qty\nPen,2\nInk,3\n")}).json()
    assert not r["failed"] and r["name"] == "orders.csv" and r["counts"]["cells"] == 6
    s = client.get(f"/api/workbooks/{r['id']}/summary").json()
    assert s["load_warnings"] == ["from_csv"]
    (src,) = [e for e in client.get(f"/api/workbooks/{r['id']}/exports").json() if e["source"]]
    assert (src["name"], src["format"]) == ("orders.csv", "CSV")
    assert client.get(f"/api/workbooks/{r['id']}/exports/orders.csv").content == b"Item,Qty\nPen,2\nInk,3\n"
