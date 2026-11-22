"""B0: the whole workbook as text in the prompt (cut to the context budget), and the question asked directly."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from sheetir.refs import num_to_col

MAX_CHARS = 120_000  # about 40k tokens


def dump(ir_db: str | Path, max_chars: int = MAX_CHARS) -> str:
    """Every sheet as 'row N: A=… | B=…' lines, values as Excel shows them (formula results where stored)."""
    con = sqlite3.connect(f"file:{Path(ir_db).as_posix()}?mode=ro", uri=True)
    try:
        sheets = json.loads(con.execute("SELECT value FROM meta WHERE key = 'sheets'").fetchone()[0])
        parts = []
        for s in sheets:
            rows: dict[int, list[str]] = {}
            for row, col, value, cached, formula in con.execute(
                    "SELECT row, col, value, cached, formula FROM cells WHERE sheet = ? ORDER BY row, col", (s,)):
                raw = cached if formula is not None else value
                v = "" if raw is None else json.loads(raw)
                rows.setdefault(row, []).append(f"{num_to_col(col)}={v}")
            parts.append(f"## Sheet {s}\n" + "\n".join(f"row {r}: " + " | ".join(c) for r, c in sorted(rows.items())))
        text = "\n\n".join(parts)
    finally:
        con.close()
    return text if len(text) <= max_chars else text[:max_chars] + "\n… (cut: the workbook is longer than the context)"


class _Reply(BaseModel):
    answer: str


SYSTEM = ("You answer questions about an Excel workbook given as text (each sheet's cells row by row). Read the "
          "values carefully, compute what the question needs, and answer briefly in the language of the question.")


def run(question: str, ir_db: str | Path, llm) -> dict:
    text = dump(ir_db)
    r = llm.chat_json([{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": f"{text}\n\n# Question\n{question}"}], _Reply, name="b0")
    return {"answer": r.answer, "steps": 1}
