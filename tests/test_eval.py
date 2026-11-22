"""Evaluation tooling (eval/): generator, answer scoring, detection metrics."""

import json

from eval.generator.make import make_dataset
from eval.metrics import correct, detection, summarize


def test_dataset_has_gold_tables_and_questions_per_category(tmp_path):
    stats = make_dataset(tmp_path, workbooks=4, questions=6, seed=3)
    assert stats["workbooks"] == 4 and stats["questions"] == 24
    qs = [json.loads(x) for x in (tmp_path / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {q["category"] for q in qs} >= {"single_lookup", "aggregate", "multi_tab_join", "fragmented_period"}
    assert all((tmp_path / "workbooks" / q["workbook"]).exists() for q in qs)
    tables = [json.loads(x) for x in (tmp_path / "tables.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all({"sheet", "range", "header_rows", "total_rows"} <= set(t) for t in tables)


def test_answers_are_scored_by_type():
    num = {"answer_type": "number", "answer": 4397.7, "tolerance": 1e-3}
    assert correct(num, "Tổng là **4.397,7 triệu đồng**.")
    assert correct(num, "Total: 4,397.7")
    assert not correct(num, "Tổng là 4.3977 triệu đồng")
    text = {"answer_type": "text", "answer": "tháng 12", "accept": ["tháng 12", "t12", "2026-12"]}
    assert correct(text, "Tháng **12** có doanh thu cao nhất")
    assert not correct(text, "Tháng 1 có doanh thu cao nhất")
    lst = {"answer_type": "list", "answer": ["CN Hà Nội", "CN Huế"]}
    assert correct(lst, "Đó là CN Huế và CN Ha Noi.") and not correct(lst, "Chỉ có CN Huế.")


def test_detection_matching():
    gold = [{"sheet": "S", "range": "A1:D10", "header_rows": [1], "total_rows": [10]},
            {"sheet": "S", "range": "A14:B20", "header_rows": [14], "total_rows": []}]
    pred = [{"sheet": "S", "range": "A1:D10", "header_rows": [1], "total_rows": [10]},
            {"sheet": "S", "range": "F1:G3", "header_rows": [1], "total_rows": []}]
    d = detection(gold, pred)
    assert d == {"tp": 1, "gold": 2, "predicted": 2, "header_ok": 1, "gold_totals": 1, "found_totals": 1}
    s = summarize([{"category": "x", "correct": True, "seconds": 1}], [d])
    assert s["detect"]["precision"] == 50.0 and s["detect"]["recall"] == 50.0 and s["accuracy"] == 100.0
