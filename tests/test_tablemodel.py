import json
import math
import sqlite3

import pytest

from sheetir import build_ir
from sheetir.sample import make_quotation
from sheetir.tablemodel import FEATURES, HandScorer, ModelScorer, TreeModel, default_scorer, get_scorer


def test_hand_scorer_is_the_default():
    assert isinstance(default_scorer(), HandScorer) and isinstance(get_scorer(), HandScorer)
    with pytest.raises(ValueError):
        get_scorer("llm")


@pytest.mark.skipif(ModelScorer.load() is None, reason="no packaged table model")
def test_packaged_model_finds_the_sample_tables(tmp_path):
    res = build_ir(make_quotation(tmp_path / "q.xlsx"), tmp_path / "q.db", table_scorer="model")
    con = sqlite3.connect(res.db_path)
    rows = con.execute("SELECT sheet, header_row, row_end, total_row, col_start, col_end FROM tables ORDER BY sheet").fetchall()
    assert rows == [("Materials", 1, 4, None, 1, 3), ("Quote", 1, 21, 22, 1, 10)]
    assert con.execute("SELECT value FROM meta WHERE key='table_scorer'").fetchone() == ("model",)
    con.close()


def test_tree_model_sums_leaves_and_applies_sigmoid():
    i = FEATURES.index("label_share")
    spec = {"features": FEATURES, "threshold": 0.5,
            "trees": [[i, 0.5, -1.0, [i, 0.9, 0.5, 2.0]], 0.25]}
    m = TreeModel(spec)
    f = dict.fromkeys(FEATURES, 0.0)
    assert math.isclose(m.predict({**f, "label_share": 0.5}), 1 / (1 + math.exp(-(-1.0 + 0.25))))  # <= goes left
    assert math.isclose(m.predict({**f, "label_share": 0.7}), 1 / (1 + math.exp(-(0.5 + 0.25))))
    assert math.isclose(m.predict({**f, "label_share": 1.0}), 1 / (1 + math.exp(-(2.0 + 0.25))))


def test_model_scorer_needs_both_files(tmp_path):
    assert ModelScorer.load(tmp_path) is None
    spec = {"features": FEATURES, "threshold": 0.4, "trees": [1.0]}
    for name in ("header_row.json", "header_block.json"):
        (tmp_path / name).write_text(json.dumps(spec))
    scorer = ModelScorer.load(tmp_path)
    f = dict.fromkeys(FEATURES, 0.0)
    assert scorer.threshold == 0.4 and scorer.header(f) > 0.7 and scorer.block(f)


def test_hand_scorer_passes_text_labels_over_numbers():
    f = dict.fromkeys(FEATURES, 0.0)
    f.update(label_share=1.0, contrast_any=1.0, contrast=0.5, unique=1.0)
    assert HandScorer().header(f) >= HandScorer.threshold
    assert HandScorer().header({**f, "contrast_any": 0.0, "contrast": 0.0}) < HandScorer.threshold
