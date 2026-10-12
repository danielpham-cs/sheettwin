"""Score the grounded Q&A (sheetir.understand.ask) on the sample workbook with the configured language model.

Each question has an expected answer read from the IR with plain SQL (or a fixed list of cells), so the score does
not depend on what the model says about the workbook. A reply passes when it names every expected item, names none
of the excluded items, and is written in the language of the question.

    uv run python bench/eval_qa.py [--repeat N] [--only 1,5] [-o report.json]

The model comes from the same settings as the web app (OPENAI_API_KEY, OPENAI_BASE_URL, SHEETIR_LLM_MODEL; .env).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from sheetir.build import build_ir
from sheetir.llm import get_llm
from sheetir.sample import make_quotation
from sheetir.understand import ask

QUOTE = "SELECT p.value FROM cells p JOIN cells v ON v.sheet = p.sheet AND v.row = p.row WHERE p.sheet = 'Quote' AND p.col = 1 AND p.row BETWEEN 2 AND 21 AND v.col = {col} AND {cond}"

# (language, question, expected: SQL returning the items or a list, excluded: SQL or list, kind of question)
QUESTIONS = [
    ("zh", "哪些產品使用材料代碼 CU？請列出產品名稱。", QUOTE.format(col=2, cond="v.value = '\"CU\"'"), QUOTE.format(col=2, cond="v.value <> '\"CU\"'"), "rows"),
    ("en", "Which products use material AL?", QUOTE.format(col=2, cond="v.value = '\"AL\"'"), QUOTE.format(col=2, cond="v.value <> '\"AL\"'"), "rows"),
    ("zh", "銅用量超過 2 公斤的產品有哪些？", QUOTE.format(col=3, cond="CAST(v.value AS REAL) > 2"), QUOTE.format(col=3, cond="CAST(v.value AS REAL) BETWEEN 0.01 AND 2"), "rows"),
    ("zh", "哪個產品的人工工時最多？", QUOTE.format(col=5, cond="CAST(v.value AS REAL) = (SELECT MAX(CAST(value AS REAL)) FROM cells WHERE sheet = 'Quote' AND col = 5 AND row BETWEEN 2 AND 21)"), [], "rows"),
    ("en", "How many products are in the quote table?", ["20"], [], "count"),
    ("zh", "所有產品的鋼用量總和是多少公斤？", "SELECT ROUND(SUM(CAST(value AS REAL)), 2) FROM cells WHERE sheet = 'Quote' AND col = 4 AND row BETWEEN 2 AND 21", [], "sum"),
    ("en", "What is the copper price?", "SELECT value FROM cells WHERE cell_id = 'Inputs!B3'", [], "value"),
    ("en", "What is the price factor of aluminium?", "SELECT value FROM cells WHERE cell_id = 'Materials!C4'", [], "value"),
    ("zh", "有哪些高嚴重度的問題？分別在哪個儲存格？", "SELECT DISTINCT cell_id FROM issues WHERE severity = 'high'", [], "issues"),
    ("en", "Which cells hold values that no formula uses?", "SELECT cell_id FROM issues WHERE kind = 'orphan'", [], "issues"),
    ("zh", "總共有幾個中等嚴重度的問題？", "SELECT COUNT(*) FROM issues WHERE severity = 'medium'", [], "issues"),
    ("zh", "這個活頁簿是做什麼用的？", [], [], "structure"),
    ("zh", "Quote!J13 的計算公式是什麼意思？", ["J13"], [], "structure"),
    ("en", "Which parameters are measured in TWD/kg?", ["copper", "steel"], ["labor"], "structure"),
]

_CJK = re.compile(r"[㐀-鿿]")
_EN_WORDS = re.compile(r"\b(the|is|are|of|and|which|in|uses?|has|have|this|that|with|for|to|by|was|from|it)\b", re.I)


def language(text: str) -> str:
    """zh when the text has CJK characters, en when it has English function words, else neutral (a bare number or a
    list of names from the workbook reads the same in any language). Cell ids and code are ignored."""
    text = re.sub(r"`[^`]*`|(?:'[^']+'|[A-Za-z_][\w.]*)![A-Z]{1,3}\d+", "", text)
    if _CJK.search(text):
        return "zh"
    return "en" if _EN_WORDS.search(text) else "neutral"


def _items(con: sqlite3.Connection, spec) -> list[str]:
    if isinstance(spec, list):
        return spec
    out = []
    for (v,) in con.execute(spec).fetchall():
        try:
            v = json.loads(v) if isinstance(v, str) else v
        except json.JSONDecodeError:
            pass
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        out.append(str(v))
    return out


def _mentions(reply: str, item: str) -> bool:
    if re.fullmatch(r"-?\d+(\.\d+)?", item):  # a number: as a whole token, also with thousands separators
        nums = {n.replace(",", "") for n in re.findall(r"-?\d[\d,]*(?:\.\d+)?", reply)}
        return any(float(n) == float(item) for n in nums if n not in ("-", ""))
    return item.lower() in reply.lower()


def score(con: sqlite3.Connection, q, reply: str) -> dict:
    lang, _, want, exclude, _ = q
    want, exclude = _items(con, want), _items(con, exclude)
    missing = [w for w in want if not _mentions(reply, w)]
    wrong = [x for x in exclude if _mentions(reply, x)]
    got_lang = language(reply)
    return {"pass": not missing and not wrong and got_lang in (lang, "neutral"), "missing": missing, "wrong": wrong,
            "language": got_lang, "expected_language": lang}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeat", type=int, default=1, help="ask each question N times")
    ap.add_argument("--only", help="comma-separated question numbers (1-based)")
    ap.add_argument("-o", "--out", help="write the full report as JSON")
    args = ap.parse_args(argv)

    llm = get_llm()
    if llm is None:
        print("no language model configured (OPENAI_API_KEY)", file=sys.stderr)
        return 2
    picked = [int(i) for i in args.only.split(",")] if args.only else range(1, len(QUESTIONS) + 1)
    tmp = Path(tempfile.mkdtemp(prefix="sheetir-qa-"))
    db = tmp / "q.db"
    build_ir(make_quotation(tmp / "q.xlsx"), db, verify=False)
    truth = sqlite3.connect(db)

    results = []
    for i in picked:
        q = QUESTIONS[i - 1]
        for r in range(args.repeat):
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            t = time.time()
            try:
                res = ask(con, q[1], llm, q[0])
                s = score(truth, q, res["reply"])
            except Exception as e:  # a failed call counts as a failed answer
                res, s = {"reply": f"{type(e).__name__}: {e}"}, {"pass": False, "error": str(e)}
            secs = time.time() - t
            results.append({"n": i, "run": r + 1, "question": q[1], "kind": q[4], "seconds": round(secs, 1), **s,
                            **{k: res.get(k) for k in ("reply", "sql", "route", "grounded", "ungrounded")}})
            mark = "PASS" if s["pass"] else "FAIL"
            why = "; ".join(f"{k}: {s[k]}" for k in ("missing", "wrong", "error") if s.get(k))
            if s.get("language") not in (None, q[0], "neutral"):
                why = (why + "; " if why else "") + f"replied in {s['language']}"
            print(f"{mark} {i:2}.{r + 1} [{q[4]:9}] {secs:5.1f}s  {q[1]}" + (f"\n      {why}" if why else ""))

    n_pass = sum(r["pass"] for r in results)
    print(f"\n{n_pass}/{len(results)} passed  (model {llm.model}, {llm.usage['calls']} calls, "
          f"{llm.usage['prompt_tokens']} prompt + {llm.usage['completion_tokens']} completion tokens)")
    if args.out:
        Path(args.out).write_text(json.dumps({"model": llm.model, "passed": n_pass, "total": len(results),
                                              "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
