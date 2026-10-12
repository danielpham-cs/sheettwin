"""Audit report of a parsed workbook, as Markdown or as a self-contained HTML page (printable to PDF from a browser).

Everything in it is read from the SheetIR database by fixed queries: the findings by severity and kind with a
plain-language explanation of each kind, the cells they concern, the parse verification and the parts of the file
SheetIR does not model. No language model is involved, so the report is the same every time for the same database.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import sqlite3
from pathlib import Path

SEVERITIES = ("high", "medium", "low")

# Title and plain-language explanation of each issue kind (the explanations match the web UI's issueHelp).
KINDS = {
    "inconsistent_formula": {
        "zh": ("同一欄的公式不一致", "這格的公式和同欄其他格不同，常是打錯或忘了更新。"),
        "en": ("Inconsistent formula in a column", "This cell's formula differs from the rest of its column, often a typo or a forgotten update.")},
    "hardcoded_in_formula_column": {
        "zh": ("公式欄裡的手打數值", "整欄都是公式，這格卻是手打的數值：公式可能被覆蓋了。"),
        "en": ("Typed-in value in a formula column", "A typed-in value sits where the column otherwise calculates: the formula was probably overwritten.")},
    "missing_formula": {
        "zh": ("計算欄中的空白格", "整欄都在計算，這格卻是空的。"),
        "en": ("Empty cell in a calculated column", "An empty cell in a column that otherwise calculates.")},
    "hardcoded_constant": {
        "zh": ("寫死在公式裡的常數", "業務數字直接寫在公式裡，而不是放在輸入儲存格，改動時容易漏掉。"),
        "en": ("Constant hard-coded in formulas", "A business number is written inside a formula instead of an input cell, so it is easy to miss when it changes.")},
    "approximate_lookup": {
        "zh": ("非精確比對的查找", "查找沒有用精確比對，鍵是文字時，除非已排序，否則可能回傳錯的列。"),
        "en": ("Approximate lookup over text keys", "A lookup without exact match over text keys can return the wrong row unless the keys are sorted.")},
    "dynamic_ref": {
        "zh": ("動態參照（INDIRECT / OFFSET）", "INDIRECT / OFFSET 用文字組出參照，無法追溯它依賴哪些儲存格。"),
        "en": ("Dynamic reference (INDIRECT / OFFSET)", "INDIRECT / OFFSET build references from text, so the cells it depends on cannot be traced.")},
    "volatile": {
        "zh": ("易變函數", "每次開檔都會重算（TODAY、NOW、RAND…），結果會變動。"),
        "en": ("Volatile function", "Recalculates on every open (TODAY, NOW, RAND…), so results change between runs.")},
    "orphan": {
        "zh": ("沒有被使用的值", "沒有任何公式使用的值：可能是殘留，或漏了引用。"),
        "en": ("Unused value", "A value that no formula uses: a leftover, or a reference that was forgotten.")},
    "broken_ref": {
        "zh": ("損壞的參照（#REF!）", "公式含 #REF!：原本參照的儲存格被刪除了。"),
        "en": ("Broken reference (#REF!)", "The formula contains #REF!: a cell it pointed to was deleted.")},
    "external_link": {
        "zh": ("連到其他活頁簿", "公式讀取另一個活頁簿，SheetIR 看不到。"),
        "en": ("Link to another workbook", "The formula reads another workbook, which SheetIR cannot see.")},
    "unknown_sheet": {
        "zh": ("不存在的工作表", "公式指向不存在的工作表。"),
        "en": ("Unknown sheet", "The formula points to a sheet that does not exist.")},
    "parse_error": {
        "zh": ("無法解析的公式", "SheetIR 無法解析這個公式。"),
        "en": ("Formula could not be read", "SheetIR could not read this formula.")},
    "data_table": {
        "zh": ("模擬分析資料表", "模擬分析的資料表，SheetIR 尚未支援。"),
        "en": ("What-if data table", "A what-if data table, which SheetIR does not model.")},
    "cycle": {
        "zh": ("循環參照", "這格在循環參照中。"),
        "en": ("Circular reference", "The cell is part of a circular reference.")},
    "unresolved_ref": {
        "zh": ("無法解析的結構化參照", "無法解析的結構化參照。"),
        "en": ("Unresolved structured reference", "A structured reference that could not be resolved.")},
    "unresolved_name": {
        "zh": ("無法解析的名稱", "無法解析的已定義名稱。"),
        "en": ("Unresolved name", "A defined name that could not be resolved.")},
    "array_formula": {
        "zh": ("陣列公式", "陣列公式（CSE）：已處理，但值得檢查。"),
        "en": ("Array formula", "An array (CSE) formula: handled, but worth reviewing.")},
}

TEXT = {
    "zh": {
        "title": "稽核報告", "file": "檔案", "sha": "SHA-256", "built": "解析時間", "generated": "報告產生時間",
        "version": "SheetIR 版本", "status": "解析狀態",
        "summary": "摘要", "findings": "發現的問題", "verification": "解析驗證與涵蓋範圍", "about": "關於這份報告",
        "sev": {"high": "高嚴重度", "medium": "中嚴重度", "low": "低嚴重度"},
        "colon": "：",
        "lead": lambda n, c: f"SheetIR 在這份活頁簿中發現 {n} 個問題：" + "、".join(f"{k} {v} 個" for k, v in c) + "。",
        "none": "SheetIR 沒有在這份活頁簿中發現問題。",
        "first": lambda n: f"建議優先處理 {n} 個高嚴重度問題。",
        "size": "活頁簿規模", "count": "數量",
        "stats": {"sheets": "工作表", "cells": "非空白儲存格", "formulas": "公式", "groups": "公式群組（相同計算）",
                  "tables": "表格", "params": "參數"},
        "cols": ("儲存格", "位置", "公式", "技術細節"), "n_items": lambda n: f"{n} 項",
        "statuses": {"verified": "已驗證：所有檢查一致，每個公式都能追溯",
                     "partial": "部分理解：所有檢查一致，但有些公式無法完整追溯",
                     "check_failed": "驗證未通過：解析結果可能有誤",
                     "unverified": "尚未驗證", None: "尚未驗證"},
        "independent": "獨立重讀檢查（不使用 SheetIR 的解析程式，重新讀取檔案後比對）",
        "checks": {"content": lambda c: f"內容：{c['matched']}/{c['cells']} 個儲存格的值或公式一致",
                   "rebuild": lambda c: f"重建：由 IR 重建的活頁簿有 {c['identical']}/{c['cells']} 個儲存格相同",
                   "references": lambda c: f"參照：{c['agree']}/{c['checked']} 個公式讀取的儲存格一致",
                   "groups": lambda c: f"公式群組：{c['ok']}/{c['members']} 個成員能由群組公式重現"},
        "self": "自我檢查", "check_cols": ("檢查", "結果", "層級", "說明"), "pass": "通過", "fail": "未通過",
        "not_understood": lambda n, total: f"無法完整追溯的公式：{n}/{total}",
        "ignored": "SheetIR 未建模的檔案內容", "ignored_none": "無（沒有巨集、樞紐分析表、圖表、外部連結等）",
        "no_verification": "這份結果是由較舊版本的 SheetIR 解析的，沒有驗證紀錄。請重新解析。",
        "about_lines": [
            "本報告由 SheetIR 依固定規則從解析結果（IR 資料庫）產生，沒有使用 AI，相同的檔案每次產生的內容相同。",
            "儲存格位址對應原始活頁簿；原始檔案沒有被修改。",
            "「技術細節」欄位為 SheetIR 規則的原始輸出（英文、R1C1 寫法：RC[-1] 表示同一列左邊一欄）。",
            "規則式檢查只能找出特定類型的問題，沒有列出的問題不代表活頁簿一定正確。",
        ],
        "no_cached": "這個檔案沒有 Excel 存下的計算結果（不是由 Excel 存檔），因此報告無法比對公式的計算值。",
    },
    "en": {
        "title": "Audit report", "file": "File", "sha": "SHA-256", "built": "Parsed at", "generated": "Report generated",
        "version": "SheetIR version", "status": "Parse status",
        "summary": "Summary", "findings": "Findings", "verification": "Parse verification and coverage",
        "about": "About this report",
        "sev": {"high": "high severity", "medium": "medium severity", "low": "low severity"},
        "colon": ": ",
        "lead": lambda n, c: f"SheetIR found {n} issue{'s' if n != 1 else ''} in this workbook: "
                             + ", ".join(f"{v} {k}" for k, v in c) + ".",
        "none": "SheetIR found no issues in this workbook.",
        "first": lambda n: f"Start with the {n} high-severity issue{'s' if n != 1 else ''}.",
        "size": "Workbook size", "count": "Count",
        "stats": {"sheets": "Sheets", "cells": "Non-empty cells", "formulas": "Formulas",
                  "groups": "Formula groups (same calculation)", "tables": "Tables", "params": "Parameters"},
        "cols": ("Cell", "Location", "Formula", "Technical detail"), "n_items": lambda n: f"{n} item{'s' if n != 1 else ''}",
        "statuses": {"verified": "Verified: every check matches and every formula can be traced",
                     "partial": "Partly understood: every check matches, but some formulas cannot be traced fully",
                     "check_failed": "Check failed: the parse result may be wrong",
                     "unverified": "Not verified", None: "Not verified"},
        "independent": "Independent re-read (the file read again without SheetIR's parser, then compared)",
        "checks": {"content": lambda c: f"Contents: {c['matched']}/{c['cells']} cells have the same value or formula",
                   "rebuild": lambda c: f"Rebuild: {c['identical']}/{c['cells']} cells identical in a workbook rebuilt from the IR",
                   "references": lambda c: f"References: {c['agree']}/{c['checked']} formulas read the same cells",
                   "groups": lambda c: f"Formula groups: {c['ok']}/{c['members']} members reproduced from their group's formula"},
        "self": "Self-checks", "check_cols": ("Check", "Result", "Level", "Detail"), "pass": "pass", "fail": "fail",
        "not_understood": lambda n, total: f"Formulas that cannot be traced fully: {n}/{total}",
        "ignored": "Parts of the file SheetIR does not model",
        "ignored_none": "None (no macros, pivot tables, charts, external links, …)",
        "no_verification": "This result was parsed by an older SheetIR and has no verification record. Re-parse it.",
        "about_lines": [
            "SheetIR generated this report from the parse result (the IR database) with fixed rules. No AI was used, "
            "so the same file always gives the same report.",
            "Cell addresses refer to the original workbook, which was not modified.",
            "The technical detail column is the raw output of SheetIR's rules (in R1C1 notation: RC[-1] is the cell "
            "one column to the left in the same row).",
            "Rule-based checks find specific kinds of problems; an issue not listed here does not prove the workbook "
            "correct.",
        ],
        "no_cached": "This file holds no results calculated by Excel (it was not saved by Excel), so the report "
                     "cannot compare formula values.",
    },
}


def audit_report(con: sqlite3.Connection, lang: str = "zh") -> dict:
    """The report's content, read from the database; render it with to_markdown or to_html."""
    lang = lang if lang in TEXT else "zh"
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    meta = dict(q("SELECT key, value FROM meta"))
    one = lambda sql: q(sql)[0][0]  # noqa: E731
    stats = {"sheets": len(json.loads(meta.get("sheets") or "[]")), "cells": one("SELECT COUNT(*) FROM cells"),
             "formulas": one("SELECT COUNT(*) FROM cells WHERE vtype = 'formula'"),
             "groups": one("SELECT COUNT(*) FROM formula_groups"), "tables": one("SELECT COUNT(*) FROM tables"),
             "params": one("SELECT COUNT(*) FROM params")}
    rows = q("SELECT i.cell_id, i.kind, i.severity, i.detail, c.semantic, c.formula FROM issues i "
             "LEFT JOIN cells c USING (cell_id) ORDER BY i.issue_id")
    findings = []
    for sev in SEVERITIES:
        kinds: dict[str, list] = {}
        for cell, kind, s, detail, semantic, formula in rows:
            if s == sev:
                kinds.setdefault(kind, []).append({"cell": cell, "semantic": semantic, "formula": formula, "detail": detail})
        if kinds:
            findings.append({"severity": sev, "kinds": [
                {"kind": k, "title": KINDS.get(k, {}).get(lang, (k, ""))[0],
                 "help": KINDS.get(k, {}).get(lang, (k, ""))[1], "items": items} for k, items in kinds.items()]})
    verification = json.loads(meta["verification"]) if meta.get("verification") else None
    return {
        "lang": lang, "name": Path(meta.get("source", "")).name, "sha256": meta.get("sha256"),
        "built_at": meta.get("built_at"), "sheetir_version": meta.get("sheetir_version"),
        "status": meta.get("parse_status"), "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "stats": stats, "counts": {s: sum(1 for r in rows if r[2] == s) for s in SEVERITIES}, "findings": findings,
        "verification": verification,
        "checks": [{"name": n, "passed": bool(p), "severity": s, "detail": d} for n, p, s, d in
                   q("SELECT name, passed, severity, detail FROM checks ORDER BY rowid")],
        "has_cached": one("SELECT COUNT(cached) FROM cells WHERE vtype = 'formula'") > 0 or not stats["formulas"],
    }


def _sections(r: dict):
    """The report as (heading level, heading, list of blocks); a block is ("p", text), ("list", items) or
    ("table", header, rows). Both renderers walk this, so Markdown and HTML always say the same thing."""
    T = TEXT[r["lang"]]
    n = sum(r["counts"].values())
    present = [(T["sev"][s], r["counts"][s]) for s in SEVERITIES if r["counts"][s]]
    summary = [("p", T["lead"](n, present) if n else T["none"])]
    if r["counts"]["high"]:
        summary.append(("p", T["first"](r["counts"]["high"])))
    summary.append(("p", f"{T['status']}{T['colon']}{T['statuses'].get(r['status'], r['status'])}"))
    summary.append(("table", (T["size"], T["count"]), [(T["stats"][k], str(v)) for k, v in r["stats"].items()]))
    if not r["has_cached"]:
        summary.append(("p", T["no_cached"]))
    yield 2, f"1. {T['summary']}", summary

    if r["findings"]:
        yield 2, f"2. {T['findings']}", []
        for f in r["findings"]:
            yield 3, f"{T['sev'][f['severity']].capitalize()} ({r['counts'][f['severity']]})", []
            for k in f["kinds"]:
                yield 4, f"{k['title']} — {T['n_items'](len(k['items']))}", [
                    ("p", k["help"]),
                    ("table", T["cols"], [(i["cell"], i["semantic"] or "", i["formula"] or "", i["detail"] or "")
                                          for i in k["items"]])]

    v, blocks = r["verification"], []
    if v:
        blocks.append(("p", T["independent"]))
        blocks.append(("list", [("✓ " if c.get("pass") else "✗ ") + T["checks"][name](c)
                                for name, c in v.get("checks", {}).items() if name in T["checks"]]))
        cov = v.get("coverage", {})
        if cov:
            blocks.append(("p", T["not_understood"](cov.get("n_not_understood", 0), cov.get("formulas", 0))))
            if cov.get("not_understood"):
                blocks.append(("list", [f"{x['cell']}: {KINDS.get(x['kind'], {}).get(r['lang'], (x['kind'],))[0]}"
                                        + (f" ({x['detail']})" if x.get("detail") else "") for x in cov["not_understood"]]))
            blocks.append(("p", f"{T['ignored']}{T['colon']}" + (", ".join(f"{x['kind']} ({x['count']})" for x in cov["ignored"])
                                                        if cov.get("ignored") else T["ignored_none"])))
    else:
        blocks.append(("p", T["no_verification"]))
    if r["checks"]:
        blocks.append(("p", T["self"]))
        blocks.append(("table", T["check_cols"], [(c["name"], T["pass"] if c["passed"] else T["fail"], c["severity"],
                                                   c["detail"] or "") for c in r["checks"]]))
    yield 2, f"{3 if r['findings'] else 2}. {T['verification']}", blocks
    yield 2, T["about"], [("list", T["about_lines"])]


def _meta_rows(r: dict) -> list[tuple[str, str]]:
    T = TEXT[r["lang"]]
    return [(T["file"], r["name"]), (T["sha"], r["sha256"] or ""), (T["built"], r["built_at"] or ""),
            (T["version"], r["sheetir_version"] or ""), (T["generated"], r["generated_at"])]


def to_markdown(r: dict) -> str:
    def cell(s: str) -> str:
        return str(s).replace("|", "\\|").replace("\n", " ")

    def table(header, rows) -> list[str]:
        return (["| " + " | ".join(cell(h) for h in header) + " |", "|" + "---|" * len(header)]
                + ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows])

    T = TEXT[r["lang"]]
    out = [f"# {T['title']}{T['colon']}{r['name']}", ""]
    out += [f"- **{k}**{T['colon']}{v}" for k, v in _meta_rows(r)] + [""]
    for level, heading, blocks in _sections(r):
        out += ["#" * level + " " + heading, ""]
        for b in blocks:
            if b[0] == "p":
                out += [b[1], ""]
            elif b[0] == "list":
                out += [f"- {x}" for x in b[1]] + [""]
            else:
                code_cols = (0, 2) if b[1] in (TEXT["zh"]["cols"], TEXT["en"]["cols"]) else ()
                out += table(b[1], [[f"`{c}`" if i in code_cols and c else c for i, c in enumerate(row)]
                                    for row in b[2]]) + [""]
    return "\n".join(out).rstrip() + "\n"


CSS = """
:root { color-scheme: light; --ink: #1f2328; --muted: #59636e; --line: #d1d9e0; --soft: #f6f8fa;
  --high: #b42318; --high-bg: #fef3f2; --medium: #b54708; --medium-bg: #fffaeb; --low: #475467; --low-bg: #f2f4f7; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink); font: 14px/1.6 system-ui, -apple-system, "Segoe UI",
  "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 40px 24px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; } h2 { font-size: 19px; margin: 36px 0 12px; padding-bottom: 6px;
  border-bottom: 1px solid var(--line); } h3 { font-size: 16px; margin: 26px 0 8px; } h4 { font-size: 14px; margin: 18px 0 6px; }
p { margin: 8px 0; } ul { margin: 8px 0; padding-left: 22px; }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; color: var(--muted); font-size: 12.5px;
  margin: 12px 0 0; } .meta dt { font-weight: 600; } .meta dd { margin: 0; overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.chip { padding: 2px 10px; border-radius: 999px; font-size: 12.5px; font-weight: 600; }
.high { color: var(--high); background: var(--high-bg); } .medium { color: var(--medium); background: var(--medium-bg); }
.low { color: var(--low); background: var(--low-bg); }
h3.high, h3.medium, h3.low { background: none; border-left: 4px solid currentColor; padding-left: 10px; }
.wrap { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 8px 0; }
th, td { border: 1px solid var(--line); padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: var(--soft); font-weight: 600; } code { font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-wrap: anywhere; } .help { color: var(--muted); }
.print { float: right; border: 1px solid var(--line); background: var(--soft); border-radius: 8px; padding: 6px 12px;
  font: inherit; cursor: pointer; }
@media print { main { padding: 0; max-width: none; } .print { display: none; } h2, h3, h4 { break-after: avoid; }
  tr { break-inside: avoid; } @page { margin: 16mm 14mm; } }
"""


def to_html(r: dict) -> str:
    e = html.escape
    T = TEXT[r["lang"]]
    sev_class = {f"{T['sev'][s].capitalize()} ({r['counts'][s]})": s for s in SEVERITIES}
    out = [f'<!doctype html><html lang="{"zh-Hant" if r["lang"] == "zh" else "en"}"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>{e(T['title'])} · {e(r['name'])}</title><style>{CSS}</style></head><body><main>",
           f'<button class="print" onclick="window.print()">{"列印 / 存成 PDF" if r["lang"] == "zh" else "Print / Save as PDF"}</button>',
           f"<h1>{e(T['title'])}{e(T['colon'])}{e(r['name'])}</h1>", '<dl class="meta">']
    out += [f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in _meta_rows(r)] + ["</dl>", '<div class="chips">']
    out += [f'<span class="chip {s}">{e(T["sev"][s])}{e(T["colon"])}{r["counts"][s]}</span>' for s in SEVERITIES] + ["</div>"]
    for level, heading, blocks in _sections(r):
        cls = sev_class.get(heading, "") if level == 3 else ""
        out.append(f'<h{level}{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{e(heading)}</h{level}>')
        for b in blocks:
            if b[0] == "p":
                out.append(f'<p{" class=help" if level == 4 else ""}>{e(b[1])}</p>')
            elif b[0] == "list":
                out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in b[1]) + "</ul>")
            else:
                code_cols = (0, 2) if b[1] in (TEXT["zh"]["cols"], TEXT["en"]["cols"]) else ()
                out.append('<div class="wrap"><table><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in b[1])
                           + "</tr></thead><tbody>")
                out += ["<tr>" + "".join(f"<td><code>{e(c)}</code></td>" if i in code_cols and c else f"<td>{e(c)}</td>"
                                         for i, c in enumerate(row)) + "</tr>" for row in b[2]]
                out.append("</tbody></table></div>")
    out.append("</main></body></html>")
    return "\n".join(out)
