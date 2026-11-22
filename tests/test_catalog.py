"""The catalog (sheetir/catalog): families, relations, cards, search; built from generated messy workbooks."""

import pytest

from eval.generator.base_data import make_world
from eval.generator.perturb import write_workbook
from sheetir.build import build_ir
from sheetir.catalog.families import parse_period
from sheetir.catalog.relations import name_sim
from sheetir.catalog.store import CatalogHandle, build_catalog


def _catalog(tmp_path, perturb, seed=3):
    world = make_world(seed)
    gold = write_workbook(world, tmp_path / "w.xlsx", set(perturb), seed)
    build_ir(tmp_path / "w.xlsx", tmp_path / "ir.db", verify=False)
    return world, gold, build_catalog(tmp_path / "ir.db", tmp_path)


@pytest.mark.parametrize("token,context,period", [
    ("t3", "", "03"), ("thang 3", "", "03"), ("Tháng 12", "", "12"), ("m03", "", "03"), ("march", "", "03"),
    ("t3", "BÁO CÁO THÁNG 3/2026", "2026-03"), ("t1_2025", "", "2025-01"), ("q2", "", "Q2"), ("quy 4", "2026", "2026-Q4"),
    ("2025", "", "2025"), ("ha noi", "", None), ("t13", "", None),
])
def test_periods_from_sheet_names(token, context, period):
    assert parse_period(token, context) == period


def test_name_similarity():
    assert name_sim("ma_cn", "ma_cn") == 1.0
    assert name_sim("ma_cn", "ma_chi_nhanh") == 0.9          # abbreviation
    assert name_sim("branch_id", "ma_chi_nhanh") >= 0.9      # synonyms, in another order
    assert name_sim("doanh_thu", "ma_cn") == 0.0


def test_monthly_sheets_become_one_view_in_one_unit(tmp_path):
    world, gold, h = _catalog(tmp_path, {"split_period", "unit_shift", "rename_cols", "total_rows", "title_rows"})
    (fam,) = h.families
    assert fam.discriminator == "period" and len(fam.member_table_ids) == 12
    assert sorted(fam.member_values.values())[0] in ("01", "2026-01")
    assert fam.units["doanh_thu"] == "trieu_vnd" and any("nghin_vnd" in n for n in fam.notes)
    con = h.connect()
    try:
        # months 1-3 were written in thousands: the view gives millions, as the other months
        got = con.execute(f'SELECT SUM(doanh_thu) FROM "{fam.duckdb_view}" WHERE period LIKE \'%01\'').fetchone()[0]
        assert got == pytest.approx(sum(r["doanh_thu"] for r in world.revenue[1]), rel=1e-6)
        # months 4-6 called the column 'Doanh số': still the same column of the view
        got = con.execute(f'SELECT SUM(doanh_thu) FROM "{fam.duckdb_view}" WHERE period LIKE \'%05\'').fetchone()[0]
        assert got == pytest.approx(sum(r["doanh_thu"] for r in world.revenue[5]), rel=1e-6)
        # subtotal and total rows are not data
        n = con.execute(f'SELECT COUNT(*) FROM "{fam.duckdb_view}"').fetchone()[0]
        assert n == 12 * len(world.branches)
    finally:
        con.close()
    card = h.card(fam.family_id)
    assert card.sql_name == fam.duckdb_view and card.time_coverage and len(card.sources) == 12
    unit_of = {c.name: c.unit for c in card.columns}
    assert unit_of["doanh_thu"] == "trieu_vnd"


def test_join_keys_between_tables(tmp_path):
    _, _, h = _catalog(tmp_path, {"split_period", "multi_table"})
    rels = {(r.left, r.right) for r in h.relations}
    assert any(left.startswith("v_fam_") and left.endswith(".ma_cn") and right.endswith(".ma_cn")
               for left, right in rels)
    staff = next(c for c in h.cards if "ma_nv" in [x.name for x in c.columns])
    assert any("ma_cn" in r for r in staff.relations)


def test_search_finds_cards_without_diacritics(tmp_path):
    _, _, h = _catalog(tmp_path, {"split_period"})
    best, _ = h.search("doanh thu chi nhanh theo thang", 3)[0]
    assert best.card_id.startswith("fam_")
    best, _ = h.search("nhân sự lương", 3)[0]
    assert "ma_nv" in [c.name for c in best.columns]


def test_catalog_reopens_and_ignores_other_versions(tmp_path):
    _, _, h = _catalog(tmp_path, set())
    again = CatalogHandle.open(tmp_path)
    assert again is not None and [c.card_id for c in again.cards] == [c.card_id for c in h.cards]
    import json
    p = tmp_path / "catalog.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["version"] = -1
    p.write_text(json.dumps(data), encoding="utf-8")
    assert CatalogHandle.open(tmp_path) is None


def test_formulas_without_stored_results_are_computed(tmp_path):
    from sheetir.sample import make_quotation

    make_quotation(tmp_path / "q.xlsx")  # written by openpyxl: no formula results
    build_ir(tmp_path / "q.xlsx", tmp_path / "ir.db", verify=False)
    h = build_catalog(tmp_path / "ir.db", tmp_path)
    assert h.stats["formulas_computed"] >= 100
    con = h.connect()
    try:
        price = con.execute("SELECT price FROM t_quote__t1 WHERE product = 'Cover plate'").fetchone()[0]
    finally:
        con.close()
    assert price == pytest.approx(552.096)  # =H13*1.12+Inputs!$B$6, the cell with the planted inconsistency
    quote = h.card("quote__t1")
    assert any("computed by SheetTwin" in w for w in quote.warnings)


def test_periods_take_one_form_across_a_family():
    from sheetir.catalog.families import _same_form

    assert _same_form(["2026-01", "02", "03", None]) == ["2026-01", "2026-02", "2026-03", None]
    assert _same_form(["2025-01", "2026-01", "03"]) == ["2025-01", "2026-01", "03"]  # two years: left as they are
    assert _same_form(["01", "02"]) == ["01", "02"]
