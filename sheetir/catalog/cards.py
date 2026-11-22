"""Catalog cards (design §6.6): what a table or family holds, written so that search finds it and an analyst (or the
agent) understands it.

Every card is first written by rules from the clean table: title (the caption above the table, else the sheet), a
description, columns with types and units, sample rows, time coverage, relations and keywords. When a language model is
available it rewrites the title, description and column descriptions (prompt §15.2), in the language most headers are
in; the model never adds columns, and the rule-written card stays if it fails.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel
from unidecode import unidecode

from ..clean.headers import strip_unit
from ..clean.models import CleanTable
from .models import CatalogCard, ColumnDesc, Relation, TableFamily

_VI = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹáàãéèíìóòõúùýĂÂĐÊÔƠƯ]")
_CJK = re.compile(r"[㐀-鿿]")


def language(texts: list[str]) -> str:
    """vi, zh or en: the language most of the texts are written in."""
    vi = sum(bool(_VI.search(t)) for t in texts)
    zh = sum(bool(_CJK.search(t)) for t in texts)
    if zh > vi and zh * 3 >= len(texts):
        return "zh"
    if vi and vi * 4 >= len(texts):
        return "vi"
    return "en"


def _humanize(name: str) -> str:
    return re.sub(r"_+", " ", name).strip().capitalize()


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


def rule_card(card_id: str, sql_name: str, tables: list[CleanTable], family: TableFamily | None,
              sample_rows: list[dict], row_count: int, time_coverage: str | None, relations: list[str],
              columns: list[str] | None = None) -> CatalogCard:
    first = tables[0]
    cols = first.columns
    if columns is not None:  # a family view: its own column order
        by_name = {c.name: c for c in cols}
        cols = [by_name[n] for n in columns if n in by_name]
    sheets = list(dict.fromkeys(t.spec.sheet for t in tables))
    title = first.spec.title or (first.spec.name and _humanize(first.spec.name)) or first.spec.sheet
    if family:
        title = re.sub(r"\b(t(háng)?\s*\d{1,2}|q\d|month\s*\d+)\b", "", title, flags=re.I).strip(" -_/:") or title
    lang = language([c.display_name for c in cols] + [title])
    def shown(c):
        if not family:
            return c.display_name
        u = family.units.get(c.name, c.unit)
        return strip_unit(c.display_name) + (f" ({u})" if u else "")

    col_list = ", ".join(shown(c) for c in cols[:12]) + ("…" if len(cols) > 12 else "")
    if family:
        span = f"{len(tables)} sheets ({sheets[0]} … {sheets[-1]})"
        desc = {"vi": f"{title}: gộp {span}, mỗi sheet một {family.discriminator}. Cột: {col_list}.",
                "zh": f"{title}：合併 {span}，每個工作表一個 {family.discriminator}。欄位：{col_list}。",
                "en": f"{title}: {span} combined, one {family.discriminator} per sheet. Columns: {col_list}."}[lang]
    else:
        desc = {"vi": f"Bảng {title} trên sheet {sheets[0]}, {row_count} dòng. Cột: {col_list}.",
                "zh": f"{title}（工作表 {sheets[0]}，{row_count} 列）。欄位：{col_list}。",
                "en": f"{title} on sheet {sheets[0]}, {row_count} rows. Columns: {col_list}."}[lang]
    col_descs = []
    if family:
        col_descs.append(ColumnDesc(name=family.discriminator, dtype="text", display_name=family.discriminator,
                                    description=f"{family.discriminator} of each source sheet "
                                                f"({', '.join(list(family.member_values.values())[:4])}…)"))
        col_descs.append(ColumnDesc(name="_source_sheet", dtype="text", display_name="source sheet",
                                    description="the sheet each row comes from"))
    for c in cols:
        unit, display = c.unit, c.display_name
        if family:
            # the view's unit (members in another unit are converted), not the first member's
            unit = family.units.get(c.name, c.unit)
            display = strip_unit(c.display_name) + (f" ({unit})" if unit else "")
        bits = [display]
        if unit:
            bits.append(f"unit {unit}")
        if c.sample_values and not family:
            bits.append("e.g. " + ", ".join(c.sample_values[:3]))
        col_descs.append(ColumnDesc(name=c.name, description="; ".join(bits), unit=unit, dtype=c.dtype,
                                    display_name=display))
    words = [title, *sheets, *(c.display_name for c in cols), *(t.spec.title or "" for t in tables)]
    keywords = list(dict.fromkeys(w for w in words if w))[:40]
    sources = [{"sheet": t.spec.sheet, "range": t.spec.range, "table_id": t.table_id, "title": t.spec.title,
                **({"value": family.member_values[t.table_id]} if family else {})} for t in tables]
    warnings = [w for t in tables for w in t.warnings][:10] + (family.notes if family else [])
    return CatalogCard(card_id=card_id, sql_name=sql_name, title=title, description=desc, columns=col_descs,
                       time_coverage=time_coverage, row_count=row_count, sample_rows=sample_rows, sources=sources,
                       relations=relations, keywords=keywords, warnings=warnings,
                       low_confidence=any(t.spec.low_confidence for t in tables))


class _ColText(BaseModel):
    name: str
    description: str


class CardText(BaseModel):
    title: str
    description: str
    columns: list[_ColText]
    keywords: list[str]


CARD_SYSTEM = """You write a short catalog entry for a data table so that a search system can find it and an analyst
can understand it. Return JSON only, matching the schema. Write in {language}. Do not invent columns or facts not
present in the input. The description is 1-3 sentences: what one row is, what the table covers, and anything unusual
(units, periods, warnings). Give at most 10 keywords, including likely search words in {language} and English."""

LANG_NAME = {"vi": "Vietnamese", "zh": "Traditional Chinese", "en": "English"}


def llm_describe(card: CatalogCard, llm) -> CatalogCard:
    """The card with title, description, column descriptions and keywords written by the model. On any failure, the
    card as it was."""
    lang = language([c.display_name or c.name for c in card.columns] + [card.title])
    cols = "\n".join(f"{c.name} | {c.display_name} | {c.dtype} | {c.unit or '-'}" for c in card.columns)
    sample = json.dumps(card.sample_rows[:5], ensure_ascii=False, default=str)
    user = (f"Sheet name(s): {', '.join(s['sheet'] for s in card.sources[:12])}\n"
            f"Title above table: {card.title}\nRows: {card.row_count}\n"
            f"Columns (name | display name | type | unit):\n{cols}\n"
            f"Sample rows:\n{sample}\nTime coverage: {card.time_coverage or '-'}\n"
            f"Known relations: {'; '.join(card.relations) or '-'}\n"
            f"Notes: {'; '.join(card.warnings) or '-'}")
    try:
        r = llm.chat_json([{"role": "system", "content": CARD_SYSTEM.format(language=LANG_NAME[lang])},
                           {"role": "user", "content": user}], CardText, name="catalog_card")
    except Exception:  # noqa: BLE001 - the rule-written card is a fine fallback
        return card
    written = {x.name: x.description for x in r.columns if x.description.strip()}
    columns = [c.model_copy(update={"description": written.get(c.name, c.description)}) for c in card.columns]
    keywords = list(dict.fromkeys([*r.keywords[:10], *card.keywords]))[:50]
    return card.model_copy(update={"title": r.title.strip() or card.title,
                                   "description": r.description.strip() or card.description,
                                   "columns": columns, "keywords": keywords, "described_by": "llm"})


def relation_text(rel: Relation, sql_name: str) -> str | None:
    """'joins t_chinhanh__t1 on ma_cn = ma_cn' from the point of view of `sql_name`."""
    lo, lc = rel.left.rsplit(".", 1)
    ro, rc = rel.right.rsplit(".", 1)
    if lo == sql_name:
        return f"joins {ro} on {sql_name}.{lc} = {ro}.{rc}"
    if ro == sql_name:
        return f"joins {lo} on {lo}.{lc} = {sql_name}.{rc}"
    return None


def index_text(card: CatalogCard) -> str:
    """What the search index reads for a card (design §6.7)."""
    parts = [card.title, card.description, *(f"{c.name} {c.display_name or ''} {c.description}" for c in card.columns),
             *card.keywords, *(s["sheet"] for s in card.sources)]
    text = " ".join(p for p in parts if p)
    return f"{text} {unidecode(text)}"
