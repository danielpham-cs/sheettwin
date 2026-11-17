"""The agent loop (design §7.1, §7.5).

    result = answer(question, catalog, llm, settings, lang="en", on_step=callback)

Each step the model returns one AgentAction; SheetIR runs the tool and sends the result back. At final_answer SheetIR
checks the answer before returning it (§7.5): every number in it must appear in a query result (or the question), and
an answer with figures must cite a result. A failed check goes back to the model once; if it fails again the answer is
returned with the unmatched numbers listed. The reply language is the question's (checked, one rewrite). When the steps
run out, the answer says so honestly and shows the last query's result instead of a guess.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..llm import LLMError
from ..understand import LANGUAGE_NAME, question_language, text_language, ungrounded_numbers
from .prompts import (
    LAST_RESULT, MODEL_FAILED, NEEDS_RESULT, NUMBERS_NOT_FOUND, RESULT_PREFIX, STOPPED, UNVERIFIED_NOTE, system_prompt,
)
from .protocol import AgentAction, Step
from .provenance import provenance
from .tools import Toolbox, jsonable

MAX_RESULT_CHARS = 7000


class AgentResult(BaseModel):
    answer: str
    lang: str
    results: list[dict]            # the run_sql results the answer rests on (rows for display)
    chart: dict | None = None
    provenance: list[dict] = []
    steps: list[Step] = []
    grounded: bool = True
    ungrounded: list[str] = []
    stopped: str | None = None     # why the loop ended without a checked final answer: max_steps | llm_error
    model: str | None = None
    searches: list[list[str]] = [] # card ids each search_catalog returned
    sql_runs: list[dict] = []      # every query tried: {sql, ok, error}
    seconds: float = 0.0


def _table_md(res: dict, limit: int = 20) -> str:
    cell = lambda v: "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(res["columns"]) + " |", "|" + "---|" * len(res["columns"])]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in res["rows"][:limit]]
    return "\n".join(lines)


def _clip(obj: dict) -> str:
    text = json.dumps(jsonable(obj), ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …(cut)"


class _Answer(BaseModel):
    answer: str


def answer(question: str, catalog, llm, settings: Settings | None = None, lang: str = "en",
           on_step: Callable[[Step], None] | None = None) -> AgentResult:
    s = settings or get_settings()
    t_start = time.perf_counter()
    lang = question_language(question, lang)
    tools = Toolbox(catalog, s)
    messages = [{"role": "system", "content": system_prompt(catalog.summary(), lang, s.agent.max_rows_to_llm)},
                {"role": "user", "content": f"Question: {question}"}]
    steps: list[Step] = []
    corrected = False
    stopped = None
    final = None
    try:
        for n in range(1, s.agent.max_steps + 1):
            t0 = time.perf_counter()
            try:
                action = llm.chat_json(messages, AgentAction, name="agent_step", thinking=s.agent.thinking,
                                       max_tokens=2048)
            except LLMError:
                stopped = "llm_error"
                break
            messages.append({"role": "assistant", "content": action.model_dump_json()})
            if action.tool == "final_answer":
                text = str(action.args.get("answer") or "").strip()
                rids = [r for r in (action.args.get("result_ids") or []) if r in tools.results]
                problem = _check(question, text, rids, tools)
                if problem and not corrected:
                    corrected = True
                    result = {"error": problem}
                    steps.append(Step(n=n, thought=action.thought, tool="final_answer", args=jsonable(action.args),
                                      result=result, seconds=round(time.perf_counter() - t0, 2)))
                    if on_step:
                        on_step(steps[-1])
                    messages.append({"role": "user", "content": RESULT_PREFIX.format(tool="final_answer") + problem})
                    continue
                final = (text, rids, action.args.get("chart_id"))
                steps.append(Step(n=n, thought=action.thought, tool="final_answer", args={"result_ids": rids},
                                  result={}, seconds=round(time.perf_counter() - t0, 2)))
                if on_step:
                    on_step(steps[-1])
                break
            result = tools.dispatch(action.tool, action.args)
            display = dict(result)
            if action.tool == "run_sql" and "result_id" in result:
                display["rows"] = result["rows"][:10]
            steps.append(Step(n=n, thought=action.thought, tool=action.tool, args=jsonable(action.args),
                              result=jsonable(display), seconds=round(time.perf_counter() - t0, 2)))
            if on_step:
                on_step(steps[-1])
            messages.append({"role": "user", "content": RESULT_PREFIX.format(tool=action.tool) + _clip(result)})
        else:
            stopped = "max_steps"
    finally:
        tools.close()

    if final is None:
        last = list(tools.results.values())[-1:] if tools.results else []
        key = lang if lang in STOPPED else "en"
        text = MODEL_FAILED[key] if stopped == "llm_error" else STOPPED[key].format(n=s.agent.max_steps)
        if last:
            text += LAST_RESULT.get(lang, LAST_RESULT["en"]) + "\n\n" + _table_md(last[0])
        return _result(text, lang, last, None, catalog, steps, [], stopped or "max_steps", llm, tools, t_start)

    text, rids, chart_id = final
    used = [tools.results[r] for r in rids] or list(tools.results.values())
    if text_language(text) not in (lang, "neutral"):
        try:
            text = llm.chat_json([{"role": "user", "content": f"Rewrite this answer in {LANGUAGE_NAME[lang]}. Keep "
                                   f"every number, name and table exactly as written.\n\n{text}"}], _Answer,
                                 name="rewrite_language").answer.strip() or text
        except LLMError:
            pass
    missing = _ungrounded(question, text, tools)
    if missing:
        text += "\n\n> " + UNVERIFIED_NOTE.get(lang, UNVERIFIED_NOTE["en"]) + ", ".join(missing)
    chart = tools.charts.get(str(chart_id)) if chart_id else None
    return _result(text, lang, used, chart, catalog, steps, missing, None, llm, tools, t_start)


def _ungrounded(question: str, text: str, tools: Toolbox) -> list[str]:
    sources = [question] + [json.dumps(r["rows"], default=str) for r in tools.results.values()]
    extra = tuple(float(r["row_count"]) for r in tools.results.values())
    return ungrounded_numbers(text, sources, extra)


def _check(question: str, text: str, rids: list[str], tools: Toolbox) -> str | None:
    """Why a final answer is not acceptable yet, or None. A figure that is in no query result is refused, which also
    covers an answer with figures and no query at all (§7.5: a quantitative answer must rest on a result)."""
    if not text:
        return "The answer is empty."
    missing = _ungrounded(question, text, tools)
    if missing:
        return (NEEDS_RESULT + " " if not tools.results else "") + NUMBERS_NOT_FOUND.format(numbers=", ".join(missing))
    return None


def _result(text, lang, used, chart, catalog, steps, missing, stopped, llm, tools, t_start) -> AgentResult:
    return AgentResult(
        answer=text, lang=lang,
        results=[{k: r[k] for k in ("result_id", "sql", "columns", "rows", "row_count", "truncated")} for r in used],
        chart=chart, provenance=provenance(catalog, used), steps=steps, grounded=not missing, ungrounded=missing,
        stopped=stopped, model=getattr(llm, "model", None), searches=tools.searches, sql_runs=tools.sql_runs,
        seconds=round(time.perf_counter() - t_start, 2))
