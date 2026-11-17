"""Generate a dataset of synthetic workbooks with gold tables and questions (design §11).

    uv run python -m eval.generator.make --out eval/datasets/synthetic_v1 --workbooks 30 --questions 6
    uv run python -m eval.generator.make --out eval/datasets/m1 --workbooks 4 --questions 5 --clean

--clean writes M1's workbooks: every perturbation off (one table per sheet, header in row 1, revenue in one sheet).
Otherwise each perturbation is on with probability 0.5 (split_period 0.8), so the categories mix.

    <out>/workbooks/wb_001.xlsx …
    <out>/tables.jsonl       {"workbook", "sheet", "range", "header_rows", "total_rows", "name"}
    <out>/questions.jsonl    {"id", "workbook", "question", "answer", "answer_type", "category", "gold_sources", …}
    <out>/workbooks.jsonl    {"workbook", "seed", "perturbations"}
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .base_data import make_world
from .perturb import PERTURBATIONS, write_workbook
from .questions import make_questions


def make_dataset(out: str | Path, workbooks: int, questions: int, clean: bool = False, seed: int = 7) -> dict:
    out = Path(out)
    (out / "workbooks").mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    tables, qs, books = [], [], []
    for i in range(1, workbooks + 1):
        s = rng.randint(0, 10**9)
        world = make_world(s)
        if clean:
            perturb = set()
        else:
            perturb = {p for p in PERTURBATIONS if rng.random() < (0.8 if p == "split_period" else 0.5)}
        name = f"wb_{i:03d}.xlsx"
        gold = write_workbook(world, out / "workbooks" / name, perturb, s)
        tables += [{"workbook": name, **t} for t in gold["tables"]]
        qs += make_questions(world, name, gold, random.Random(s), questions)
        books.append({"workbook": name, "seed": s, "perturbations": gold["perturbations"]})
    for fname, rows in (("tables.jsonl", tables), ("questions.jsonl", qs), ("workbooks.jsonl", books)):
        (out / fname).write_text("\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows) + "\n",
                                 encoding="utf-8")
    return {"workbooks": len(books), "tables": len(tables), "questions": len(qs)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workbooks", type=int, default=30)
    ap.add_argument("--questions", type=int, default=6)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    print(make_dataset(a.out, a.workbooks, a.questions, a.clean, a.seed))


if __name__ == "__main__":
    main()
