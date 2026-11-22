"""ColumnSpec and CleanTable (design §5.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..detect.models import TableSpec


class ColumnSpec(BaseModel):
    name: str                       # SQL identifier
    display_name: str               # 'Q1 / Doanh thu (triệu đồng)'
    header_path: list[str]          # ['Q1', 'Doanh thu (triệu đồng)']
    dtype: Literal["int", "float", "text", "date", "bool"]
    unit: str | None
    null_ratio: float
    distinct_count: int
    sample_values: list[str]        # at most 5
    source_col: int | None = None   # Excel column number; None for a column the pipeline added (section)


class CleanTable(BaseModel):
    table_id: str
    duckdb_name: str
    spec: TableSpec
    columns: list[ColumnSpec]
    n_rows: int
    totals_check: dict[str, bool] = {}
    subtotal_rows: list[int] = []   # rows dropped as subtotals (inside the body)
    section_rows: list[int] = []    # rows that only label the rows below (turned into the `section` column)
    warnings: list[str] = []
