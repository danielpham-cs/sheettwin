"""The clean stage (sheetir/clean) and the table specs it starts from (sheetir/detect)."""

import datetime as dt
import sqlite3

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.table import Table as XLTable

from sheetir.build import build_ir
from sheetir.clean.headers import identifiers, slug
from sheetir.clean.normalize import SheetGrid, refine_spec, to_clean_table
from sheetir.clean.types import candidate, column_type
from sheetir.clean.units import column_unit, normalize_unit
from sheetir.detect.models import TableSpec, specs_from_ir, validate_spec


def _ir(tmp_path, wb: Workbook, name="w.xlsx"):
    p = tmp_path / name
    wb.save(p)
    db = tmp_path / (name + ".db")
    build_ir(p, db, verify=False)
    return sqlite3.connect(db)


# ---- headers, units, types -----------------------------------------------------------------------------------------

def test_identifiers_are_unaccented_unique_and_never_empty():
    assert slug("Doanh thu (triệu đồng)") == "doanh_thu_trieu_dong"
    assert identifiers(["Mã CN", "Doanh thu (triệu đồng)", "Doanh thu", None, "2024", "period"]) == \
        ["ma_cn", "doanh_thu", "doanh_thu_2", "col_4", "c_2024", "period_2"]


@pytest.mark.parametrize("text,code", [
    ("triệu đồng", "trieu_vnd"), ("Tr.đ", "trieu_vnd"), ("nghìn đồng", "nghin_vnd"), ("1.000đ", "nghin_vnd"),
    ("VNĐ", "vnd"), ("tỷ đồng", "ty_vnd"), ("USD", "usd"), ("%", "percent"), ("kg", "kg"), ("TWD/kg", "twd_kg"),
    ("Q1 năm 2025 theo kế hoạch", None),
])
def test_units(text, code):
    assert normalize_unit(text) == code


def test_column_unit_prefers_the_header_then_the_format():
    assert column_unit("Doanh thu (triệu đồng)", ['#,##0 "đ"']) == "trieu_vnd"
    assert column_unit("Doanh thu", ['#,##0 "đ"', '#,##0 "đ"']) == "vnd"
    assert column_unit("Tỷ lệ", ["0.0%", "0.0%"]) == "percent"


def test_vietnamese_numbers_dates_and_percentages_are_typed():
    col = [candidate("text", s, None) for s in ["1.250,5", "2.000", "15,25", None, "(1.000)"]]
    dtype, values, dropped = column_type(col)
    assert dtype == "float" and values == [1250.5, 2000.0, 15.25, None, -1000.0] and dropped == 0
    dtype, values, _ = column_type([candidate("text", s, None) for s in ["1,250.5", "2,000", "3.5"]])
    assert values == [1250.5, 2000.0, 3.5]
    dtype, values, _ = column_type([candidate("text", s, None) for s in ["05/03/2026", "31/12/2025"]])
    assert dtype == "date" and values == [dt.date(2026, 3, 5), dt.date(2025, 12, 31)]
    dtype, values, _ = column_type([candidate("text", s, None) for s in ["12,5%", "40%"]])
    assert dtype == "float" and values == [0.125, 0.4]
    # a column mostly text stays text; numbers become their text
    dtype, values, _ = column_type([candidate("text", "a", None), candidate("number", 3, None),
                                    candidate("text", "b", None)])
    assert dtype == "text" and values == ["a", "3", "b"]


def test_formula_dates_from_number_format():
    assert candidate("formula", 46082.0, "dd/mm/yyyy") == ("date", dt.date(2026, 3, 1))


# ---- table specs ---------------------------------------------------------------------------------------------------

def _report(ws, top=1):
    ws.cell(top, 1, "BÁO CÁO DOANH THU THÁNG 3/2026")
    ws.cell(top + 2, 1, "Mã CN")
    ws.cell(top + 2, 2, "Kết quả")
    ws.merge_cells(start_row=top + 2, start_column=2, end_row=top + 2, end_column=3)
    ws.merge_cells(start_row=top + 2, start_column=1, end_row=top + 3, end_column=1)
    ws.cell(top + 3, 2, "Doanh thu (triệu đồng)")
    ws.cell(top + 3, 3, "Số đơn")
    rows = [("Miền Bắc", None, None), ("HN01", 120.5, 30), ("HP01", 80.0, 20), ("Cộng miền Bắc", "=SUM(B7:B8)", "=SUM(C7:C8)"),
            ("Miền Nam", None, None), ("SG01", 200.0, 50), ("Tổng cộng", "=B9+B12", "=C9+C12")]
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            if v is not None:
                ws.cell(top + 4 + i, 1 + j, v)
    ws.cell(top + 11, 1, "Ghi chú: số liệu chưa bao gồm thuế VAT, được tổng hợp tự động từ hệ thống bán hàng.")


def test_specs_from_ir_refined_carry_header_levels_total_notes_and_title(tmp_path):
    wb = Workbook()
    _report(wb.active)
    wb.active.title = "DT_T3"
    con = _ir(tmp_path, wb)
    (spec,) = specs_from_ir(con)
    assert spec.table_id == "dt_t3__t1" and spec.sheet == "DT_T3"
    assert spec.title == "BÁO CÁO DOANH THU THÁNG 3/2026" and spec.source == "rule:heuristic"
    # detection reads one header row and ends at the note; refine_spec finds the second header row and the total
    spec = refine_spec(spec, SheetGrid.load(con, spec.sheet))
    assert spec.header_rows == [3, 4] and spec.total_rows == [11] and spec.note_rows == [12]


def test_clean_table_flattens_headers_drops_subtotals_and_keeps_sections(tmp_path):
    wb = Workbook()
    _report(wb.active)
    con = _ir(tmp_path, wb)
    (spec,) = specs_from_ir(con)
    grid = SheetGrid.load(con, spec.sheet)
    spec = refine_spec(spec, grid)
    names, rows, t = to_clean_table(spec, grid, "t_x")
    assert names == ["_row", "section", "ma_cn", "ket_qua_doanh_thu", "ket_qua_so_don"]
    assert [r[2] for r in rows] == ["HN01", "HP01", "SG01"]
    assert [r[1] for r in rows] == ["Miền Bắc", "Miền Bắc", "Miền Nam"]
    assert t.subtotal_rows == [8] and t.section_rows == [5, 9]
    revenue = next(c for c in t.columns if c.name == "ket_qua_doanh_thu")
    assert revenue.dtype == "float" and revenue.unit == "trieu_vnd"
    assert revenue.header_path == ["Kết quả", "Doanh thu (triệu đồng)"]


def test_totals_are_checked_against_the_total_row(tmp_path):
    wb = Workbook()
    ws = wb.active
    for r, row in enumerate([("Mã", "Doanh thu"), ("A", 10), ("B", 20), ("C", 5), ("Tổng cộng", 36)], start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(r, c, v)
    con = _ir(tmp_path, wb)
    (spec,) = specs_from_ir(con)
    _, rows, t = to_clean_table(spec, SheetGrid.load(con, spec.sheet), "t")
    assert len(rows) == 3 and t.totals_check == {"doanh_thu": False}
    assert any("total row says 36" in w for w in t.warnings)


def test_vertical_merges_fill_down(tmp_path):
    wb = Workbook()
    ws = wb.active
    data = [("Miền", "Chi nhánh", "Doanh thu"), ("Bắc", "HN", 1), (None, "HP", 2), ("Nam", "SG", 3), (None, "CT", 4)]
    for r, row in enumerate(data, start=1):
        for c, v in enumerate(row, start=1):
            if v is not None:
                ws.cell(r, c, v)
    ws.merge_cells("A2:A3")
    ws.merge_cells("A4:A5")
    con = _ir(tmp_path, wb)
    (spec,) = specs_from_ir(con)
    _, rows, _ = to_clean_table(spec, SheetGrid.load(con, spec.sheet), "t")
    assert [r[1] for r in rows] == ["Bắc", "Bắc", "Nam", "Nam"]


def test_validate_spec_names_each_problem():
    other = TableSpec(table_id="s__t1", sheet="S", range="A1:C5", header_rows=[1], confidence=1, source="llm")
    bad = TableSpec(table_id="s__t2", sheet="S", range="B4:D9", header_rows=[5], total_rows=[12], confidence=1,
                    source="llm")
    errs = validate_spec(bad, (1, 1, 4, 10), [other])
    assert any("start at the range's first row" in e for e in errs)
    assert any("inside the range" in e for e in errs)
    assert any("overlaps" in e for e in errs)
    assert validate_spec(other, (1, 1, 4, 10), []) == []


# ---- detection additions (§6.3.1) ----------------------------------------------------------------------------------

def test_autofilter_and_named_ranges_declare_tables(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    # values only, so the layout heuristic would not take it: numbers in the header row
    for r, row in enumerate([("Code", "Q1", "Q2"), ("a", 1, 2), ("b", 3, 4), ("c", 5, 6)], start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(r, c, v)
    ws.auto_filter.ref = "A1:C4"
    from openpyxl.workbook.defined_name import DefinedName
    for r, row in enumerate([("Name", "City"), ("x", "Hà Nội"), ("y", "Huế"), ("z", "Vinh")], start=8):
        for c, v in enumerate(row, start=5):
            ws.cell(r, c, v)
    wb.defined_names["Branches"] = DefinedName("Branches", attr_text="Data!$E$8:$F$11")
    con = _ir(tmp_path, wb)
    sources = {r[0]: r[1] for r in con.execute("SELECT source, name FROM tables")}
    assert "autofilter" in sources and sources.get("named_range") == "branches"  # the name, as the author gave it
    specs = specs_from_ir(con)
    assert {s.source for s in specs} >= {"rule:auto_filter", "rule:named_range"}


def test_vietnamese_total_words_mark_the_total_row(tmp_path):
    wb = Workbook()
    ws = wb.active
    rows = [("Sản phẩm", "Số lượng"), ("Bút", 2), ("Vở", 3), ("Tổng cộng", 5)]
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(r, c, v)
    con = _ir(tmp_path, wb)
    assert con.execute("SELECT total_row FROM tables").fetchone()[0] == 4


def test_excel_tables_still_come_first(tmp_path):
    wb = Workbook()
    ws = wb.active
    for r, row in enumerate([("A", "B"), (1, 2), (3, 4)], start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(r, c, v)
    ws.add_table(XLTable(displayName="T1", ref="A1:B3"))
    ws.auto_filter.ref = "A1:B3"
    con = _ir(tmp_path, wb)
    assert [r[0] for r in con.execute("SELECT source FROM tables")] == ["listobject"]
