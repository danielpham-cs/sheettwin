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


# ---- one cell, explained in plain language -----------------------------------------------------------------------
# A few sentences built from the IR (what the cell is, how it is calculated and from what, what is wrong with it, what
# uses it), then the technical details. No language model: every statement is read from the database.

CELL = {
    "zh": {
        "body": "`{cell}` 是表格 `{table}` 中 **{key}** 這一列的 **{field}**。",
        "total": "`{cell}` 是表格 `{table}` 中 **{field}** 的合計。",
        "param": "`{cell}` 是輸入參數 **{name}**{unit}。", "unit": "（單位：{u}）",
        "output": "`{cell}` 是結果 **{name}**。",
        "calc": "`{cell}` 是中間計算 **{name}**。",
        "plain": "`{cell}` 是工作表 `{sheet}` 上的一個儲存格。",
        "role": {"input": "它是手動輸入的值 {value}，有公式使用它。",
                 "derived": "它由公式計算，結果會再被其他儲存格使用。",
                 "output": "它由公式計算，是最終結果：沒有其他公式再使用它。",
                 "label": "它是文字標籤：{value}。",
                 "orphan": "它的值是 {value}，但沒有任何公式使用它。"},
        "how": "**計算方式：**`{formula}`，用到：", "how_alone": "**計算方式：**`{formula}`。",
        "range": "範圍 `{id}`（{n} 個儲存格）",
        "const": "直接寫在公式裡的數字 {v}",
        "more": "…還有 {n} 個",
        "result": "Excel 上次計算的結果是 **{v}**。",
        "no_result": "檔案裡沒有 Excel 存下的計算結果（不是由 Excel 存檔），所以這裡看不到它的值。",
        "problems": "**發現的問題：**",
        "inconsistent": "同欄其他列在這裡會是 `{expected}`，這格卻是 `{formula}`，可能打錯或忘了更新。",
        "hardcoded": "數字 {v} 直接寫在公式裡{scope}，而不是放在輸入儲存格，改動時容易漏掉。",
        "scope": "（共 {n} 格）",
        "fix_hint": "可以在「問題」頁面套用建議的修正。",
        "used_by": "**被這些儲存格使用：**", "used_more": "…共 {n} 個", "paren": "（{x}）", "colon": "：",
        "tech": "**技術細節**",
        "of": "{key} 的 {field}", "total_of": "{field} 合計", "param_of": "參數 {name}",
    },
    "en": {
        "body": "`{cell}` is the **{field}** of **{key}** in table `{table}`.",
        "total": "`{cell}` is the total of **{field}** in table `{table}`.",
        "param": "`{cell}` is the input parameter **{name}**{unit}.", "unit": " (in {u})",
        "output": "`{cell}` is the result **{name}**.",
        "calc": "`{cell}` is the intermediate calculation **{name}**.",
        "plain": "`{cell}` is a cell on sheet `{sheet}`.",
        "role": {"input": "It holds a typed-in value, {value}, that formulas use.",
                 "derived": "It is calculated by a formula, and other cells use its result.",
                 "output": "It is calculated by a formula and is a final result: no other formula uses it.",
                 "label": "It is a text label: {value}.",
                 "orphan": "It holds the value {value}, but no formula uses it."},
        "how": "**How it is calculated:** `{formula}`, from:", "how_alone": "**How it is calculated:** `{formula}`.",
        "range": "the range `{id}` ({n} cells)",
        "const": "the number {v}, written into the formula",
        "more": "… and {n} more",
        "result": "Excel's last calculated result is **{v}**.",
        "no_result": "The file holds no result calculated by Excel (it was not last saved by Excel), so its value is "
                     "not known here.",
        "problems": "**Problems found:**",
        "inconsistent": "the other rows of this column would have `{expected}` here, but this cell has `{formula}`. "
                        "Likely a typo or a forgotten update.",
        "hardcoded": "the number {v} is typed into the formula{scope} instead of coming from an input cell, so it is "
                     "easy to miss when it changes.",
        "scope": " (in {n} cells)",
        "fix_hint": "The **Issues** view has a suggested fix.",
        "used_by": "**Used by:** ", "used_more": "… {n} cells in all", "paren": " ({x})", "colon": ": ",
        "tech": "**Technical details**",
        "of": "{field} of {key}", "total_of": "total {field}", "param_of": "parameter {name}",
    },
}

_SEM_ROW = re.compile(r'^(\w+)\["(.*)"\]\.(\w+)$')
_SEM_TOTAL = re.compile(r"^(\w+)\.total\.(\w+)$")


def _plain(v) -> str:
    """A stored (JSON) value as it reads in a sentence."""
    v = None if v is None else json.loads(v)
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, float):
        v = int(v) if v.is_integer() else round(v, 4)
    if isinstance(v, (int, float)):
        return f"{v:,}"
    return f"“{v}”"


def _short_name(semantic: str | None, t: dict) -> str | None:
    """quote["Cover plate"].unit_cost -> 'unit_cost of Cover plate'; params.x -> 'parameter x'."""
    if not semantic:
        return None
    if m := _SEM_ROW.match(semantic):
        return t["of"].format(key=m[2], field=m[3])
    if m := _SEM_TOTAL.match(semantic):
        return t["total_of"].format(field=m[2])
    head, _, name = semantic.partition(".")
    if head == "params":
        return t["param_of"].format(name=name)
    return name if head in ("outputs", "calc") and name else semantic


def _what(con, c: dict, t: dict) -> str:
    cell, sem = c["cell_id"], c["semantic"] or ""
    if m := _SEM_ROW.match(sem):
        return t["body"].format(cell=cell, table=m[1], key=m[2], field=m[3])
    if m := _SEM_TOTAL.match(sem):
        return t["total"].format(cell=cell, table=m[1], field=m[2])
    head, _, name = sem.partition(".")
    if head == "params":
        unit = con.execute("SELECT unit FROM params WHERE cell_id = ?", (cell,)).fetchone()
        return t["param"].format(cell=cell, name=name, unit=t["unit"].format(u=unit[0]) if unit and unit[0] else "")
    if head in ("outputs", "calc") and name:
        return t[{"outputs": "output", "calc": "calc"}[head]].format(cell=cell, name=name)
    return t["plain"].format(cell=cell, sheet=c["sheet"])


def _column_formula(con, c: dict) -> str | None:
    """The formula the rest of the cell's table column has, written for this cell (None if there is no pattern)."""
    from openpyxl.formula.translate import Translator

    if c["table_id"] is None:
        return None
    key = con.execute("SELECT r1c1 FROM fields WHERE table_id = ? AND col = ?", (c["table_id"], c["col"])).fetchone()
    if not key or not key[0] or key[0] == c["r1c1"]:
        return None
    donor = con.execute("SELECT cell_id, a1, formula FROM cells WHERE table_id = ? AND col = ? AND r1c1 = ? "
                        "ORDER BY ABS(row - ?) LIMIT 1", (c["table_id"], c["col"], key[0], c["row"])).fetchone()
    if not donor:
        return None
    try:
        return Translator(donor["formula"], origin=donor["a1"]).translate_formula(c["a1"])
    except Exception:  # noqa: BLE001 - a formula openpyxl cannot move: say nothing rather than something wrong
        return None


def _problem(con, c: dict, i: dict, t: dict, lang: str) -> str:
    from ..report import KINDS

    title, help_ = KINDS.get(i["kind"], {}).get(lang, (i["kind"], i["detail"] or ""))
    text = help_
    if i["kind"] == "inconsistent_formula" and (expected := _column_formula(con, c)):
        text = t["inconsistent"].format(expected=expected, formula=c["formula"])
    elif i["kind"] == "hardcoded_constant" and (m := re.match(r"literal (\S+) in .*\(applies to (\d+) cells?\)",
                                                             i["detail"] or "")):
        n = int(m[2])
        text = t["hardcoded"].format(v=m[1], scope=t["scope"].format(n=n) if n > 1 else "")
    return f"- {MSG[lang]['sev'].get(i['severity'], '')} **{title}**{t['colon']}{text}"


def _cell(con, m, cell_id: str) -> str:
    rows = _rows(con, "SELECT * FROM cells WHERE cell_id = ?", cell_id)
    if not rows:
        return m["cell_empty"].format(cell=cell_id)
    c = rows[0]
    lang = "en" if m is MSG["en"] else "zh"
    t = CELL[lang]
    c_ = t["colon"]
    sep = " " if lang == "en" else ""

    role = t["role"].get(c["role"], "").format(value=_plain(c["value"]))
    out = [f"### `{cell_id}`", "", sep.join(s for s in (_what(con, c, t), role) if s)]

    if c["formula"]:
        items = []
        for kind, src in con.execute("SELECT DISTINCT src_kind, src_id FROM edges WHERE dst_cell = ? ORDER BY rowid",
                                     (cell_id,)).fetchall():
            if kind == "range":
                n = con.execute("SELECT COUNT(*) FROM range_cells WHERE range_id = ?", (src,)).fetchone()[0]
                items.append(t["range"].format(id=src, n=n))
                continue
            p = con.execute("SELECT semantic, formula, value, cached FROM cells WHERE cell_id = ?", (src,)).fetchone()
            name = _short_name(p["semantic"], t) if p else None
            val = (p["cached"] if p["formula"] else p["value"]) if p else None
            shown = _plain(val)
            items.append(f"`{src}`" + (f" {name}" if name else "") + (f" = {shown}" if shown != "—" else ""))
        consts = [r[0] for r in con.execute("SELECT DISTINCT value FROM literals WHERE cell_id = ?", (cell_id,))]
        items += [t["const"].format(v=f"{v:g}") for v in consts if v not in (0, 1)]
        out += ["", t["how" if items else "how_alone"].format(formula=c["formula"])]
        out += [f"- {x}" for x in items[:8]] + ([f"- {t['more'].format(n=len(items) - 8)}"] if len(items) > 8 else [])
        out += ["", t["result"].format(v=_plain(c["cached"])) if c["cached"] is not None else t["no_result"]]

    issues = _rows(con, f"SELECT kind, severity, detail FROM issues WHERE cell_id = ? ORDER BY {_SEV}", cell_id)
    if issues:
        out += ["", t["problems"], ""] + [_problem(con, c, i, t, lang) for i in issues] + ["", t["fix_hint"]]

    deps = [r[0] for r in con.execute("SELECT DISTINCT d.dst_cell FROM deps d JOIN cells c ON c.cell_id = d.dst_cell "
                                      "WHERE d.src_cell = ? ORDER BY c.sheet, c.col, c.row", (cell_id,))]
    if deps:
        named = []
        for d in deps[:5]:
            s = con.execute("SELECT semantic FROM cells WHERE cell_id = ?", (d,)).fetchone()
            name = _short_name(s[0], t) if s else None
            named.append(f"`{d}`" + (t["paren"].format(x=name) if name else ""))
        more = t["used_more"].format(n=len(deps)) if len(deps) > 5 else ""
        out += ["", t["used_by"] + m["list_sep"].join(named) + (m["list_sep"] + more if more else "")]

    # the same facts as before, for those who want them
    out += ["", "---", "", t["tech"], "", f"- **{m['semantic']}**{c_}`{c['semantic'] or '—'}`",
            f"- **{m['role']}**{c_}{c['role']}"]
    if c["formula"]:
        out += [f"- **{m['formula']}**{c_}`{c['formula']}`", f"- **R1C1**{c_}`{c['r1c1']}`",
                f"- **{m['cached']}**{c_}{_val(c['cached'])}"]
    else:
        out.append(f"- **{m['value']}**{c_}{_val(c['value'])}")
    out += [f"- `{i['kind']}`{c_}{i['detail']}" for i in issues]
    trace = _rows(con, TRACE_SQL, cell_id, 3)[1:]
    if trace:
        out += ["", m["trace_head"], "", m["trace_table"], "|---|---|---|---|"]
        out += [f"| {r['depth']} | `{r['cell_id']}` | {r['semantic'] or '—'} | "
                f"{'`' + r['formula'] + '`' if r['formula'] else _val(r['value'])} |" for r in trace[:25]]
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
