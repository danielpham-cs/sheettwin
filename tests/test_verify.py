"""The independent parse check (bench/verify_parse.py): it passes on the sample and catches a damaged IR."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bench"))

from verify_parse import check  # noqa: E402

from sheetir.sample import make_quotation  # noqa: E402

PLANTED = [["Quote!I13", "inconsistent_formula"], ["Quote!G17", "hardcoded_in_formula_column"], ["Inputs!B9", "orphan"],
           ["Report!B1", "volatile"], ["Report!B6", "dynamic_ref"]]


def test_sample_passes_every_check(tmp_path):
    r = check({"file": str(make_quotation(tmp_path / "q.xlsx")), "expected_issues": PLANTED}, tmp_path)
    assert {k: c["pass"] for k, c in r["checks"].items()} == dict.fromkeys(["content", "rebuild", "references", "groups", "issues"], True)
    assert r["checks"]["content"]["cells"] == 251 and r["checks"]["references"]["checked"] == 108


def test_damaged_ir_is_caught(tmp_path):
    r = check({"file": str(make_quotation(tmp_path / "q.xlsx")), "tamper": True}, tmp_path)
    assert len(r["tampered"]) == 4
    assert not any(r["checks"][k]["pass"] for k in ("content", "rebuild", "references", "groups"))
