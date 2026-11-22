"""B1: the same agent as B2, over the raw sheets instead of the catalog.

Design §11.4 describes B1 as an agent writing pandas over raw tabs in a sandbox. This runs the agent's own SQL tools
instead, so that no model-written Python is executed: each sheet is one DuckDB table s_<sheet> with a column per Excel
column (A, B, …, all text, as typed or as Excel last calculated) and _row, the Excel row. No table detection, header
reading, typing, total-row removal, families, relations or cards: what B2 adds is exactly what B1 lacks.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import duckdb

from sheetir.catalog.index import CardIndex
from sheetir.catalog.models import CatalogCard, ColumnDesc
from sheetir.clean.headers import slug
from sheetir.refs import num_to_col


class RawCatalog:
    """Duck-types sheetir.catalog.store.CatalogHandle for the agent: one card per sheet."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        meta = json.loads((self.folder / "raw.json").read_text(encoding="utf-8"))
        self.cards = [CatalogCard(**c) for c in meta["cards"]]
        self._by = {c.card_id: c for c in self.cards} | {c.sql_name: c for c in self.cards}
        self._index = CardIndex([c.card_id for c in self.cards],
                                [f"{c.title} {' '.join(x.description for x in c.columns)}" for c in self.cards])

    @classmethod
    def build(cls, ir_db: Path, folder: Path) -> "RawCatalog":
        folder = Path(folder)
        db = folder / "raw.duckdb"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(f"file:{Path(ir_db).as_posix()}?mode=ro", uri=True)
        dcon = duckdb.connect(str(db))
        cards = []
        try:
            sheets = json.loads(con.execute("SELECT value FROM meta WHERE key = 'sheets'").fetchone()[0])
            for s in sheets:
                cells: dict[int, dict[int, str]] = {}
                for row, col, value, cached, formula in con.execute(
                        "SELECT row, col, value, cached, formula FROM cells WHERE sheet = ?", (s,)):
                    raw = cached if formula is not None else value
                    if raw is not None:
                        v = json.loads(raw)
                        cells.setdefault(row, {})[col] = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
                if not cells:
                    continue
                ncol = max(c for r in cells.values() for c in r)
                cols = [num_to_col(c) for c in range(1, ncol + 1)]
                name = f"s_{slug(s)}"
                dcon.execute(f'CREATE TABLE "{name}" ("_row" INTEGER, ' + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
                rows = [[r] + [cells[r].get(c) for c in range(1, ncol + 1)] for r in sorted(cells)]
                dcon.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * (ncol + 1))})', rows)
                sample = [{"_row": r[0], **{c: v for c, v in zip(cols, r[1:]) if v is not None}} for r in rows[:8]]
                cards.append(CatalogCard(
                    card_id=name, sql_name=name, title=f"Sheet {s}", row_count=len(rows), sample_rows=sample,
                    description=f"Raw cells of sheet {s}: one row per Excel row (_row), one text column per Excel "
                                f"column. Headers, titles and totals are ordinary rows.",
                    columns=[ColumnDesc(name="_row", description="Excel row number", dtype="int")] +
                            [ColumnDesc(name=c, description=f"Excel column {c} (text)", dtype="text") for c in cols],
                    sources=[{"sheet": s, "range": f"A1:{cols[-1]}{max(cells)}"}]))
        finally:
            dcon.close()
            con.close()
        (folder / "raw.json").write_text(json.dumps({"cards": [c.model_dump(mode="json") for c in cards]},
                                                    ensure_ascii=False), encoding="utf-8")
        return cls(folder)

    def connect(self):
        return duckdb.connect(str(self.folder / "raw.duckdb"), read_only=True,
                              config={"enable_external_access": False})

    def card(self, key):
        return self._by.get(key)

    @property
    def sql_names(self):
        return {c.sql_name for c in self.cards}

    def search(self, query, k=8):
        return [(self._by[cid], s) for cid, s in self._index.search(query, k)]

    def summary(self, max_titles=30):
        return (f"{len(self.cards)} raw sheets loaded as text tables (columns are Excel letters; cast with "
                "TRY_CAST(x AS DOUBLE)):\n" + "\n".join(f"- {c.sql_name}: {c.title} ({c.row_count} rows)"
                                                       for c in self.cards[:max_titles]))

    def sources(self, sql_name, values=None):
        c = self._by.get(sql_name)
        return [{"sheet": s["sheet"], "range": s["range"], "table_title": c.title, "card_id": c.card_id}
                for s in c.sources] if c else []
