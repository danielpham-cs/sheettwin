"""sheetir.recalc: values for formulas Excel stored no result for."""

import sqlite3

import pytest
from openpyxl import Workbook

from eval.generator.base_data import make_world
from eval.generator.perturb import write_workbook
from sheetir.build import build_ir
from sheetir.recalc import Evaluator, recalc_missing


def _con(tmp_path, wb):
    wb.save(tmp_path / "w.xlsx")
    build_ir(tmp_path / "w.xlsx", tmp_path / "ir.db", verify=False)
    return sqlite3.connect(tmp_path / "ir.db")


def test_matches_excel_on_stored_results(tmp_path):
    """The generator stores results next to its SUM and ratio formulas, as Excel would: recomputing them must agree."""
    world = make_world(9)
    write_workbook(world, tmp_path / "w.xlsx", {"split_period", "total_rows", "formats"}, 9)
    build_ir(tmp_path / "w.xlsx", tmp_path / "ir.db", verify=False)
    con = sqlite3.connect(tmp_path / "ir.db")
    ev = Evaluator(con, ignore_cached=True)
    import json
    rows = con.execute("SELECT sheet, row, col, cached FROM cells WHERE formula IS NOT NULL AND cached IS NOT NULL")
    n = 0
    for s, r, c, cached in rows:
        # the generator stores results rounded to 6 decimals
        assert ev.cell((s, r, c)) == pytest.approx(json.loads(cached), rel=1e-5, abs=1e-6)
        n += 1
    assert n > 100


def test_functions(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    data = [("Mã", "Giá", "Nhóm"), ("A", 10, "x"), ("B", 20, "y"), ("C", 30, "x")]
    for r, row in enumerate(data, start=1):
        for c, v in enumerate(row, start=1):
            ws.cell(r, c, v)
    formulas = {
        "E1": "=SUM(B2:B4)", "E2": "=AVERAGE(B2:B4)", "E3": '=SUMIF(C2:C4,"x",B2:B4)', "E4": '=COUNTIF(B2:B4,">15")',
        "E5": '=VLOOKUP("B",A2:B4,2,FALSE)', "E6": '=INDEX(B2:B4,MATCH("C",A2:A4,0))', "E7": '=IF(E1>50,"big","small")',
        "E8": "=IFERROR(1/0,-1)", "E9": "=ROUND(2.345,2)", "E10": '=A2&"-"&B2', "E11": "=SUMPRODUCT(B2:B4,B2:B4)",
        "E12": '=SUMIFS(B2:B4,C2:C4,"x",B2:B4,">10")', "E13": "=MAX(B2:B4)-MIN(B2:B4)", "E14": "=10%*B4",
        "E15": '=XLOOKUP("Z",A2:A4,B2:B4,"none")', "E16": "=LEN(TRIM(\"  a  b \"))", "E17": "=B2/0",
    }
    for a, f in formulas.items():
        ws[a] = f
    vals = recalc_missing(_con(tmp_path, wb))
    got = {f"E{r}": vals.get(("S", r, 5)) for r in range(1, 18)}
    assert got == {"E1": 60, "E2": 20, "E3": 40, "E4": 2, "E5": 20, "E6": 30, "E7": "big", "E8": -1, "E9": 2.35,
                   "E10": "A-10", "E11": 1400, "E12": 30, "E13": 20, "E14": 3, "E15": "none", "E16": 3, "E17": None}


def test_unsupported_formulas_stay_without_a_value(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = 1
    ws["A2"] = '=INDIRECT("A1")'
    ws["A3"] = "=A2+1"
    assert recalc_missing(_con(tmp_path, wb)) == {}
