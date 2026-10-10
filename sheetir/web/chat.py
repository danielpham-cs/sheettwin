"""Rule-based chat over one SheetIR database.

Placeholder for the later LLM Q&A stage: it answers a fixed set of intents (summary,
issues, parameters, tables, outputs, checks, one cell) straight from the IR, in Markdown.
Replies come in Traditional Chinese ("zh") or English ("en"); questions may be in either.
"""

from __future__ import annotations

import json
import re
import sqlite3

from ..refs import addr, parse_ref
from ..store import TRACE_SQL

_CELL_RE = re.compile(r"(?:'[^']+'|[A-Za-z_][\w.]*)![A-Za-z]{1,3}\d+")
_SEV = "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"

INTENTS = [
    ("issues", ("問題", "錯誤", "異常", "風險", "issue", "error", "problem", "audit")),
    ("params", ("參數", "輸入", "假設", "param", "input", "assumption")),
    ("tables", ("表格", "資料表", "欄位", "table", "field")),
    ("outputs", ("輸出", "結果", "output", "result")),
    ("checks", ("檢查", "驗證", "自我", "check", "verify")),
    ("summary", ("摘要", "概覽", "總覽", "介紹", "這份", "summary", "overview")),
]

MSG = {
    "zh": {
        "sev": {"high": "🔴 高", "medium": "🟠 中", "low": "⚪ 低"},
        "list_sep": "、",
        "summary_head": "**{name}** 共有 {n} 個工作表：{sheets}。",
        "summary_table": "| 項目 | 數量 |",
        "summary_rows": ["儲存格", "公式", "公式群組", "表格", "參數", "問題（高嚴重度）"],
        "summary_hint": "可以問我「有哪些問題」、「參數」、「表格」，或直接輸入儲存格位址，例如 `Quote!J13`。",
        "no_issues": "沒有偵測到任何問題 🎉",
        "issues_head": "共偵測到 **{total}** 個問題",
        "issues_more": "，以下列出前 30 個",
        "colon": "：",
        "issues_table": "| 嚴重度 | 儲存格 | 類型 | 說明 |",
        "no_params": "這份活頁簿沒有偵測到參數。",
        "params_table": "| 參數 | 儲存格 | 值 | 單位 | 允許範圍 |",
        "no_tables": "沒有偵測到表格。",
        "table_head": "### `{name}`（{sheet}，{n} 列）",
        "table_table": "| 欄位 | 類型 | 單位 | 公式 (R1C1) |",
        "derived": "計算", "data": "資料",
        "no_outputs": "沒有偵測到輸出儲存格。",
        "outputs_head": "沒有其他公式引用的最終結果：",
        "outputs_table": "| 儲存格 | 語意名稱 | 公式 |",
        "checks_table": "| | 檢查 | 結果 |",
        "cell_empty": "`{cell}` 是空白儲存格，或不在這份活頁簿裡。",
        "semantic": "語意名稱", "role": "角色", "formula": "公式", "cached": "Excel 快取值", "value": "值",
        "trace_head": "**計算來源**（往上 3 層）：",
        "trace_table": "| 層 | 儲存格 | 語意名稱 | 公式 / 值 |",
        "used_by": "**被這些儲存格使用**：",
        "help": "我目前是規則式助理（LLM 問答是之後的階段），聽得懂這些：\n\n"
                "- **摘要**：這份活頁簿的概況\n- **問題**：稽核發現的錯誤與風險\n"
                "- **參數**：輸入值、單位與允許範圍\n- **表格**：偵測到的表格與欄位公式\n"
                "- **輸出**：最終結果儲存格\n- **檢查**：IR 的自我驗證結果\n"
                "- **儲存格**：輸入位址，例如 `Quote!J13`，可以追溯計算來源",
    },
    "en": {
        "sev": {"high": "🔴 High", "medium": "🟠 Medium", "low": "⚪ Low"},
        "list_sep": ", ",
        "summary_head": "**{name}** has {n} sheets: {sheets}.",
        "summary_table": "| Item | Count |",
        "summary_rows": ["Cells", "Formulas", "Formula groups", "Tables", "Parameters", "Issues (high severity)"],
        "summary_hint": "Ask me about *issues*, *parameters* or *tables*, or type a cell address such as `Quote!J13`.",
        "no_issues": "No issues detected 🎉",
        "issues_head": "Found **{total}** issues",
        "issues_more": "; showing the first 30",
        "colon": ":",
        "issues_table": "| Severity | Cell | Kind | Detail |",
        "no_params": "No parameters detected in this workbook.",
        "params_table": "| Parameter | Cell | Value | Unit | Allowed range |",
        "no_tables": "No tables detected.",
        "table_head": "### `{name}` ({sheet}, {n} rows)",
        "table_table": "| Field | Type | Unit | Formula (R1C1) |",
        "derived": "computed", "data": "data",
        "no_outputs": "No output cells detected.",
        "outputs_head": "Final results that no other formula uses:",
        "outputs_table": "| Cell | Semantic name | Formula |",
        "checks_table": "| | Check | Result |",
        "cell_empty": "`{cell}` is empty or not in this workbook.",
        "semantic": "Semantic name", "role": "Role", "formula": "Formula", "cached": "Excel cached value",
        "value": "Value",
        "trace_head": "**Calculated from** (3 levels up):",
        "trace_table": "| Level | Cell | Semantic name | Formula / value |",
        "used_by": "**Used by**: ",
        "help": "I'm a rule-based assistant for now (LLM Q&A is a later stage). I understand:\n\n"
                "- **summary**: an overview of this workbook\n- **issues**: errors and risks found by the audit\n"
                "- **parameters**: inputs with units and allowed ranges\n- **tables**: detected tables and their field formulas\n"
                "- **outputs**: final result cells\n- **checks**: the IR's self-checks\n"
                "- **a cell**: type an address such as `Quote!J13` to trace how it is calculated",
    },
}


def _val(v):
    v = None if v is None else json.loads(v)
    return "—" if v is None else f"`{v}`"


def _rows(con, sql, *args):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _summary(con, m) -> str:
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    n = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    sheets = json.loads(meta.get("sheets") or "[]")
    name = meta["source"].replace("\\", "/").split("/")[-1]
    high = n("SELECT COUNT(*) FROM issues WHERE severity = 'high'")
    issues = f"{n('SELECT COUNT(*) FROM issues')} ({high})"
    counts = [n("SELECT COUNT(*) FROM cells"), n("SELECT COUNT(*) FROM cells WHERE vtype='formula'"),
              n("SELECT COUNT(*) FROM formula_groups"), n("SELECT COUNT(*) FROM tables"),
              n("SELECT COUNT(*) FROM params"), issues]
    rows = "\n".join(f"| {label} | {v} |" for label, v in zip(m["summary_rows"], counts))
    head = m["summary_head"].format(name=name, n=len(sheets), sheets=m["list_sep"].join(f"`{s}`" for s in sheets))
    return f"{head}\n\n{m['summary_table']}\n|---|---|\n{rows}\n\n{m['summary_hint']}"


def _issues(con, m) -> str:
    rows = _rows(con, f"SELECT i.cell_id, i.kind, i.severity, i.detail FROM issues i ORDER BY {_SEV}, kind LIMIT 30")
    if not rows:
        return m["no_issues"]
    total = con.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    lines = [m["issues_head"].format(total=total) + (m["issues_more"] if total > 30 else "") + m["colon"] + "\n",
             m["issues_table"], "|---|---|---|---|"]
    lines += [f"| {m['sev'].get(r['severity'], r['severity'])} | `{r['cell_id'] or '—'}` | {r['kind']} | "
              f"{(r['detail'] or '').replace('|', '/')} |" for r in rows]
    return "\n".join(lines)


def _params(con, m) -> str:
    rows = _rows(con, "SELECT p.cell_id, p.name, p.unit, p.domain, c.value FROM params p "
                      "JOIN cells c USING(cell_id) ORDER BY p.param_id")
    if not rows:
        return m["no_params"]
    lines = [m["params_table"], "|---|---|---|---|---|"]
    for r in rows:
        dom = json.loads(r["domain"]) if r["domain"] else None
        dom_s = "—" if not dom else ", ".join(f"{k}={v}" for k, v in dom.items() if v is not None)
        lines.append(f"| **{r['name']}** | `{r['cell_id']}` | {_val(r['value'])} | {r['unit'] or '—'} | {dom_s} |")
    return "\n".join(lines)


def _tables(con, m) -> str:
    tables = _rows(con, "SELECT * FROM tables ORDER BY table_id")
    if not tables:
        return m["no_tables"]
    out = []
    for t in tables:
        fields = _rows(con, "SELECT name, kind, unit, r1c1 FROM fields WHERE table_id = ? ORDER BY col", t["table_id"])
        out.append(m["table_head"].format(name=t["name"], sheet=t["sheet"], n=t["row_end"] - t["row_start"] + 1))
        out.append(m["table_table"] + "\n|---|---|---|---|")
        out += [f"| {f['name']} | {m['derived'] if f['kind'] == 'derived' else m['data']} | {f['unit'] or '—'} | "
                f"{'`' + f['r1c1'] + '`' if f['r1c1'] else '—'} |" for f in fields]
    return "\n".join(out)


def _outputs(con, m) -> str:
    rows = _rows(con, "SELECT cell_id, semantic, formula FROM cells WHERE role='output' ORDER BY sheet, row, col LIMIT 40")
    if not rows:
        return m["no_outputs"]
    return f"{m['outputs_head']}\n\n{m['outputs_table']}\n|---|---|---|\n" + "\n".join(
        f"| `{r['cell_id']}` | {r['semantic'] or '—'} | `{r['formula']}` |" for r in rows)


def _checks(con, m) -> str:
    rows = _rows(con, "SELECT name, passed, severity, detail FROM checks")
    mark = lambda r: "✅" if r["passed"] else ("❌" if r["severity"] == "error" else "⚠️")  # noqa: E731
    return f"{m['checks_table']}\n|---|---|---|\n" + "\n".join(
        f"| {mark(r)} | {r['name']} | {r['detail']} |" for r in rows)


def _cell(con, m, cell_id: str) -> str:
    rows = _rows(con, "SELECT * FROM cells WHERE cell_id = ?", cell_id)
    if not rows:
        return m["cell_empty"].format(cell=cell_id)
    c = rows[0]
    c_ = m["colon"]
    out = [f"### `{cell_id}`", f"- **{m['semantic']}**{c_}{c['semantic'] or '—'}", f"- **{m['role']}**{c_}{c['role']}"]
    if c["formula"]:
        out += [f"- **{m['formula']}**{c_}`{c['formula']}`", f"- **R1C1**{c_}`{c['r1c1']}`",
                f"- **{m['cached']}**{c_}{_val(c['cached'])}"]
    else:
        out.append(f"- **{m['value']}**{c_}{_val(c['value'])}")
    for i in _rows(con, "SELECT kind, severity, detail FROM issues WHERE cell_id = ?", cell_id):
        out.append(f"- {m['sev'].get(i['severity'], '')} **{i['kind']}**{c_}{i['detail']}")
    trace = _rows(con, TRACE_SQL, cell_id, 3)[1:]
    if trace:
        out += ["", m["trace_head"], "", m["trace_table"], "|---|---|---|---|"]
        out += [f"| {t['depth']} | `{t['cell_id']}` | {t['semantic'] or '—'} | "
                f"{'`' + t['formula'] + '`' if t['formula'] else _val(t['value'])} |" for t in trace[:25]]
    deps = [r[0] for r in con.execute("SELECT DISTINCT dst_cell FROM deps WHERE src_cell = ? LIMIT 12", (cell_id,))]
    if deps:
        out += ["", m["used_by"] + m["list_sep"].join(f"`{d}`" for d in deps)]
    return "\n".join(out)


def find_cells(con: sqlite3.Connection, text: str) -> list[str]:
    """Canonical ids of the sheet-qualified cells in text, e.g. 'quote!j13' -> 'Quote!J13'."""
    sheets = {s.lower(): s for (s,) in con.execute("SELECT DISTINCT sheet FROM cells")}
    cells = []
    for m in _CELL_RE.findall(text):
        ref = parse_ref(m)
        if ref and ref.is_cell and ref.sheet:
            cells.append(addr(sheets.get(ref.sheet.lower(), ref.sheet), ref.r1, ref.c1))
    return list(dict.fromkeys(cells))


def answer(con: sqlite3.Connection, message: str, lang: str = "zh") -> dict:
    """Return {"reply": markdown, "cells": [cell ids mentioned]}."""
    con.row_factory = sqlite3.Row
    m = MSG.get(lang, MSG["zh"])
    cells = find_cells(con, message)
    if cells:
        return {"reply": "\n\n---\n\n".join(_cell(con, m, c) for c in cells), "cells": cells}
    low = message.lower()
    handlers = {"issues": _issues, "params": _params, "tables": _tables, "outputs": _outputs,
                "checks": _checks, "summary": _summary}
    for intent, words in INTENTS:
        if any(w in low for w in words):
            return {"reply": handlers[intent](con, m), "cells": []}
    return {"reply": m["help"], "cells": []}
