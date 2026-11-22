"""TableFamily, Relation and CatalogCard (design §5.4, §5.5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TableFamily(BaseModel):
    family_id: str                     # 'fam_dt'
    member_table_ids: list[str]
    discriminator: str                 # the column the view adds: 'period' (or 'part' when no period could be read)
    member_values: dict[str, str]      # table_id -> '2026-01'
    duckdb_view: str                   # 'v_fam_dt'
    columns: list[str] = []            # the view's columns after the discriminator and _source_sheet
    units: dict[str, str | None] = {}  # the view's unit of each column (after any conversion)
    notes: list[str] = []              # e.g. units converted to make members comparable


class Relation(BaseModel):
    left: str                          # 'v_fam_dt.ma_cn'   (the referencing side)
    right: str                         # 't_chinhanh__t1.ma_cn'   (the key side)
    kind: Literal["fk_candidate", "formula_ref", "same_family"]
    score: float
    confirmed_by: Literal["rule", "llm"]


class ColumnDesc(BaseModel):
    name: str
    description: str
    unit: str | None = None
    dtype: str | None = None
    display_name: str | None = None


class CatalogCard(BaseModel):
    card_id: str                       # = table_id or family_id
    sql_name: str                      # table or view to query
    title: str
    description: str                   # 1–3 sentences
    columns: list[ColumnDesc]
    time_coverage: str | None = None
    row_count: int
    sample_rows: list[dict] = []
    sources: list[dict] = []           # [{"sheet": "DT_T1", "range": "A3:H55", "table_id": ...}, ...]
    relations: list[str] = []          # 'joins t_chinhanh__t1 on ma_cn'
    keywords: list[str] = []
    low_confidence: bool = False
    warnings: list[str] = []
    described_by: Literal["rules", "llm"] = "rules"
