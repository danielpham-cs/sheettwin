import pytest

from sheetir.refs import Ref, addr, num_to_col, col_to_num, parse_ref, quote_sheet


@pytest.mark.parametrize("n", [1, 26, 27, 52, 702, 703, 16384])
def test_column_roundtrip(n):
    assert col_to_num(num_to_col(n)) == n


def test_parse_cell_and_range():
    assert parse_ref("B3") == Ref(None, 3, 2, 3, 2)
    r = parse_ref("Inputs!$B$3")
    assert (r.sheet, r.r1, r.c1, r.ar1, r.ac1, r.is_cell) == ("Inputs", 3, 2, True, True, True)
    r = parse_ref("'My Sheet'!A2:C4")
    assert (r.sheet, r.r1, r.c1, r.r2, r.c2) == ("My Sheet", 2, 1, 4, 3)


def test_parse_reversed_range_keeps_anchors_with_coordinates():
    r = parse_ref("$C4:A$2")
    assert (r.r1, r.c1, r.r2, r.c2) == (2, 1, 4, 3)
    assert (r.ar1, r.ac1, r.ar2, r.ac2) == (True, False, False, True)


def test_parse_whole_column_and_row():
    col = parse_ref("A:B")
    assert (col.r1, col.c1, col.c2) == (None, 1, 2) and col.contains(500_000, 2)
    row = parse_ref("3:5")
    assert (row.r1, row.r2, row.c1) == (3, 5, None)


@pytest.mark.parametrize("text", ["LaborRate", "TRUE", "Table1[Col]", "XFE1", "A0"])
def test_non_references(text):
    assert parse_ref(text) is None


def test_r1c1_is_position_independent():
    a = parse_ref("C2").with_sheet("Q").relativize(2, 6)   # =C2 written in F2
    b = parse_ref("C9").with_sheet("Q").relativize(9, 6)   # =C9 written in F9
    assert a.r1c1("Q") == b.r1c1("Q") == "RC[-3]"
    assert parse_ref("Inputs!$B$3").relativize(2, 6).r1c1("Q") == "Inputs!R3C2"
    assert a.absolutize(9, 6).a1("Q") == "C9"


def test_sheet_quoting():
    assert quote_sheet("Quote") == "Quote"
    assert quote_sheet("My Sheet") == "'My Sheet'"
    assert quote_sheet("A1") == "'A1'"
    assert quote_sheet("O'Brien") == "'O''Brien'"
    assert addr("My Sheet", 1, 1) == "'My Sheet'!A1"
