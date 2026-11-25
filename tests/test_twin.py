"""Python twin: translation (sheetir.transpile), runtime (sheetir.xl), V1 / V2 verification (sheetir.twin)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.workbook.defined_name import DefinedName

from sheetir import build_ir, xl
from sheetir.cli import main
from sheetir.convert import _write_cached
from sheetir.sample import make_quotation
from sheetir.transpile import _Names, load_twin, module_source, translate_formula
from sheetir.twin import check, export_module, stored
from sheetir.web.app import app

# Hand-computed values Excel would save for the workbook below.
EXPECTED = {
    "Calc": {
        (2, 4): 26.0, (3, 4): 10.5, (4, 4): 0.0,         # D = B*C*Rate (Rate is a defined name = Inputs!B1 = 2)
        (2, 5): "big", (3, 5): "small", (4, 5): "small",  # E = IF(D>20,"big","small")
        (2, 6): 1.35, (3, 6): 1.0, (4, 6): 1.35,          # F = VLOOKUP(A, Materials, 2, FALSE)
        (5, 4): 36.5,                                     # D5 = SUM(D2:D4)
        (6, 4): 26.0,                                     # D6 = SUMIF(A2:A4,"AL",D2:D4)
        (7, 4): 2.0,                                      # D7 = COUNTIF(E2:E4,"small")
        (8, 4): 12.17,                                    # D8 = ROUND(D5/3,2)
        (9, 4): "AL-26",                                  # D9 = A2&"-"&D2
        (10, 4): xl.XLError("#DIV/0!"),                   # D10 = D4/D4
        (11, 4): 0.0,                                     # D11 = IFERROR(D10,0)
    },
}


def _workbook(path, cached=True, wrong=None):
    wb = Workbook()
    inp = wb.active
    inp.title = "Inputs"
    inp["A1"], inp["B1"] = "Rate", 2
    wb.defined_names["Rate"] = DefinedName("Rate", attr_text="Inputs!$B$1")
    mat = wb.create_sheet("Materials")
    for r, (code, factor) in enumerate([("AL", 1.35), ("ST", 1.0)], start=1):
        mat.cell(r, 1, code)
        mat.cell(r, 2, factor)
    calc = wb.create_sheet("Calc")
    calc.append(["code", "qty", "kg", "cost", "size", "factor"])
    for code, qty, kg in [("AL", 2, 6.5), ("ST", 3, 1.75), ("AL", 0, 4)]:
        calc.append([code, qty, kg])
    for r in range(2, 5):
        calc[f"D{r}"] = f"=B{r}*C{r}*Rate"
        calc[f"E{r}"] = f'=IF(D{r}>20,"big","small")'
        calc[f"F{r}"] = f"=VLOOKUP(A{r},Materials!$A$1:$B$2,2,FALSE)"
    calc["D5"] = "=SUM(D2:D4)"
    calc["D6"] = '=SUMIF(A2:A4,"AL",D2:D4)'
    calc["D7"] = '=COUNTIF(E2:E4,"small")'
    calc["D8"] = "=ROUND(D5/3,2)"
    calc["D9"] = '=A2&"-"&D2'
    calc["D10"] = "=D4/D4"
    calc["D11"] = "=IFERROR(D10,0)"
    wb.save(path)
    if cached:
        values = dict(EXPECTED["Calc"])
        values.update(wrong or {})
        _write_cached(path, {2: values})
    return path


@pytest.fixture
def db(tmp_path):
    book = _workbook(tmp_path / "calc.xlsx")
    build_ir(book, tmp_path / "calc.db", verify=False)
    return tmp_path / "calc.db"


# ---- runtime -------------------------------------------------------------------------------------------------------

def test_rounding_follows_excel():
    assert xl.ROUND(2.675, 2) == 2.68  # half away from zero on the decimal value Excel shows
    assert xl.ROUND(1.635, 2) == 1.64
    assert xl.ROUND(-2.5, 0) == -3.0
    assert xl.ROUND(1234.5, -2) == 1200.0
    assert xl.ROUNDUP(1.201, 1) == 1.3 and xl.ROUNDDOWN(-1.29, 1) == -1.2


def test_cancellation_snaps_to_zero_like_excel():
    assert xl.sub(xl.sub(0.3, 0.2), 0.1) == 0.0
    assert xl.sub(1.0, 0.9999) != 0.0  # a real difference is kept


def test_values_coerce_and_compare_like_excel():
    assert xl.add("3", True) == 4.0
    assert xl.add("x", 1) == "#VALUE!"
    assert xl.div(1, 0) == "#DIV/0!" and isinstance(xl.div(1, 0), xl.XLError)
    assert xl.eq("abc", "ABC") is True  # text compares case-insensitively
    assert xl.lt(100, "a") is True and xl.lt("z", False) is True  # numbers < text < logicals
    assert xl.eq(None, 0) is True and xl.eq(None, "") is True


def test_criteria_and_lookups():
    codes = xl.Range([["AL"], ["ST"], ["AL"]])
    nums = xl.Range([[1.0], [2.0], [3.0]])
    assert xl.SUMIF(codes, "al", nums) == 4.0
    assert xl.COUNTIF(nums, ">=2") == 2.0
    assert xl.COUNTIF(codes, "A*") == 2.0
    assert xl.MATCH(2.5, nums, 1) == 2.0
    assert xl.MATCH("ST", codes, 0) == 2.0
    assert xl.VLOOKUP("XX", xl.Range([["AL", 1.0]]), 2, False) == "#N/A"


def test_dates():
    assert xl.DATE(2001, 1, 1) == 36892.0
    assert xl.WEEKDAY(36892) == 2.0  # a Monday
    assert xl.YEAR(36892) == 2001.0 and xl.MONTH(xl.DATE(2001, 14, 1)) == 2.0


def test_implicit_intersection():
    names = _Names([], ["S"])
    expr, _ = translate_formula("=VLOOKUP($A$1:$A$9,$C$1:$D$3,2,FALSE)", "S", 5, 5, names)
    assert expr.startswith("xl.VLOOKUP(xl.scalar(m.r('S', 1, 1, 9, 1), row, col)")


# ---- translation and V1 --------------------------------------------------------------------------------------------

def test_every_group_is_translated_once(db):
    twin = load_twin(db)
    cov = twin.coverage()
    assert cov["groups_translated"] == cov["groups"]
    d = next(g for g in twin.groups.values() if g.anchor == "Calc!D2")
    assert d.n_cells == 3 and "m.c('Inputs', 1, 2)" in d.expr  # the defined name became its cell


def test_v1_reproduces_excel(db):
    r = check(db, v2=False)
    assert r["v1"]["status"] == "passed"
    assert r["v1"]["isolated"] == r["v1"]["end_to_end"] == {"checked": 16, "passed": 16}
    assert r["groups"] == {"v1_passed": len(load_twin(db).groups)}


def test_v1_points_at_the_cell_that_differs(tmp_path):
    book = _workbook(tmp_path / "w.xlsx", wrong={(8, 4): 99.0})
    build_ir(book, tmp_path / "w.db", verify=False)
    r = check(tmp_path / "w.db", v2=False)
    assert r["v1"]["status"] == "failed" and r["status"] == "failed"
    iso = [m for m in r["v1"]["mismatches"] if m["mode"] == "isolated"]
    assert [m["cell"] for m in iso] == ["Calc!D8"]
    assert iso[0]["expected"] == "99.0" and iso[0]["got"] == "12.17"


def test_without_saved_values_v1_has_nothing_to_compare(tmp_path):
    book = _workbook(tmp_path / "n.xlsx", cached=False)
    build_ir(book, tmp_path / "n.db", verify=False)
    assert check(tmp_path / "n.db", v2=False)["v1"]["status"] == "no_baseline"


# ---- V2 ------------------------------------------------------------------------------------------------------------

def test_v2_agrees_with_pycel_on_random_scenarios(db, tmp_path):
    r = check(db, xlsx=tmp_path / "calc.xlsx", scenarios=8, seed=1)
    assert r["v2"]["status"] == "passed", r["v2"]["mismatches"][:3]
    assert r["v2"]["checked"] > 16 * 5
    assert r["status"] == "verified"
    assert set(r["groups"]) == {"verified"}


def test_v2_catches_a_wrong_translation(db, tmp_path, monkeypatch):
    monkeypatch.setattr(xl, "IF", lambda c, a, b: xl._thunk(b))  # a translation bug: IF always takes "else"
    r = check(db, xlsx=tmp_path / "calc.xlsx", scenarios=5, seed=2)
    assert r["v2"]["status"] == "failed"
    assert any(m["cell"].startswith("Calc!E") for m in r["v2"]["mismatches"])


def test_sample_without_saved_values_is_checked_by_v2(tmp_path):
    book = make_quotation(tmp_path / "q.xlsx")
    build_ir(book, tmp_path / "q.db", verify=False)
    r = check(tmp_path / "q.db", xlsx=book, scenarios=4)
    assert r["v1"]["status"] == "no_baseline"
    assert r["v2"]["status"] == "passed"
    reasons = " ".join(u["reason"] for u in r["coverage"]["untranslated"])
    assert "TODAY" in reasons and "INDIRECT" in reasons  # kept as Excel saved them, never guessed
    assert r["groups"]["untranslated"] == 2


def test_results_are_kept_in_the_database(db):
    check(db, v2=False)
    s = stored(db)
    assert s["v1"]["status"] == "passed" and len(s["group_results"]) == len(load_twin(db).groups)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM twin_groups WHERE status = 'v1_passed'").fetchone()[0] > 0


# ---- the exported module, CLI and API ------------------------------------------------------------------------------

def test_exported_module_computes_and_answers_what_if(db):
    ns = {}
    exec(compile(module_source(load_twin(db)), "twin.py", "exec"), ns)
    base = ns["compute"]()
    assert base[("Calc", 5, 4)] == 36.5
    what_if = ns["compute"]({("Inputs", 1, 2): 4.0})  # double the rate
    assert what_if[("Calc", 5, 4)] == 73.0 and what_if[("Calc", 2, 5)] == "big"


def test_cli_twin(db, tmp_path, capsys):
    out = tmp_path / "model.py"
    assert main(["twin", str(db), "--xlsx", str(tmp_path / "calc.xlsx"), "--scenarios", "3", "-o", str(out)]) == 0
    text = capsys.readouterr().out
    assert "V1          PASSED" in text and "V2          PASSED" in text
    assert "def g1(m, row, col):" in out.read_text(encoding="utf-8")


def test_api_twin(tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETIR_DATA", str(tmp_path))
    client = TestClient(app)
    wid = client.post("/api/workbooks/sample").json()["id"]
    assert client.get(f"/api/workbooks/{wid}/twin").json() == {"status": "not_run"}
    r = client.post(f"/api/workbooks/{wid}/twin", json={"scenarios": 3}).json()
    assert r["v2"]["status"] == "passed" and r["group_results"]
    assert client.get(f"/api/workbooks/{wid}/twin").json()["v2"]["status"] == "passed"
    names = [e["name"] for e in client.get(f"/api/workbooks/{wid}/exports").json()]
    assert "twin.py" in names
    py = client.get(f"/api/workbooks/{wid}/exports/twin.py")
    assert py.status_code == 200 and "def compute(" in py.text


def test_export_module_includes_the_check(db):
    check(db, v2=False)
    assert "V1: passed" in export_module(db)
