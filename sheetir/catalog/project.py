"""The catalogs of several workbooks seen as one, for the query agent of a project.

    cat = ProjectCatalog([(workbook_id, file_name, CatalogHandle), ...])

It offers what the agent uses of a CatalogHandle (cards, card, sql_names, search, summary, sources, connect). Every
table or view gets the file's short name as a prefix (`<file>__<sql_name>`) so names from different workbooks never
clash, and the agent can join tables across files. connect() attaches each workbook's DuckDB read-only to an
in-memory database, defines the prefixed views over them, then turns off file and network access (design §7.3.1).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import duckdb

from ..config import Settings, get_settings
from .cards import index_text
from .index import CardIndex, Embedder
from .models import CatalogCard
from .store import VECTORS_FILE, CatalogHandle

MAX_ALIAS = 24


def alias_for(name: str, taken: set[str]) -> str:
    """A short SQL-safe name for a file: `Doanh thu Q1.xlsx` -> `doanh_thu_q1`, unique among `taken`."""
    stem = Path(name).stem
    ascii_ = unicodedata.normalize("NFKD", stem.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_.lower()).strip("_")[:MAX_ALIAS].rstrip("_") or "file"
    if base[0].isdigit():
        base = "f_" + base
    alias, n = base, 2
    while alias in taken:
        alias, n = f"{base}_{n}", n + 1
    taken.add(alias)
    return alias


class ProjectCatalog:
    def __init__(self, members: list[tuple[str, str, CatalogHandle]], settings: Settings | None = None):
        self.settings = settings or get_settings()
        taken: set[str] = set()
        self.files: list[dict] = []          # {id, name, alias, handle}
        self.cards: list[CatalogCard] = []
        self._origin: dict[str, tuple[dict, str]] = {}   # prefixed sql_name -> (file, original sql_name)
        for wid, name, h in members:
            f = {"id": wid, "name": name, "alias": alias_for(name, taken), "handle": h}
            self.files.append(f)
            rename = {c.sql_name: f"{f['alias']}__{c.sql_name}" for c in h.cards}
            pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(rename, key=len, reverse=True))) + r")\b") \
                if rename else None
            for c in h.cards:
                new = rename[c.sql_name]
                self.cards.append(c.model_copy(update={
                    "card_id": f"{f['alias']}__{c.card_id}", "sql_name": new, "title": f"[{name}] {c.title}",
                    "relations": [pattern.sub(lambda m: rename[m[1]], r) if pattern else r for r in c.relations],
                    "sources": [{**s, "workbook": name, "workbook_id": wid} for s in c.sources],
                }))
                self._origin[new] = (f, c.sql_name)
        self._by_id = {c.card_id: c for c in self.cards}
        self._by_sql = {c.sql_name: c for c in self.cards}
        self._index = None

    @property
    def sql_names(self) -> set[str]:
        return set(self._by_sql)

    def card(self, key: str) -> CatalogCard | None:
        return self._by_id.get(key) or self._by_sql.get(key)

    def connect(self):
        con = duckdb.connect(":memory:")
        try:
            for i, f in enumerate(self.files):
                path = f["handle"].db_path.as_posix().replace("'", "''")
                con.execute(f"ATTACH '{path}' AS w{i} (READ_ONLY)")
                f["_db"] = f"w{i}"
            for new, (f, old) in self._origin.items():
                con.execute(f'CREATE VIEW "{new}" AS SELECT * FROM {f["_db"]}.main."{old}"')
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
        except Exception:
            con.close()
            raise
        return con

    def search(self, query: str, k: int = 8) -> list[tuple[CatalogCard, float]]:
        if self._index is None:
            vectors, embedder = self._vectors()
            self._index = CardIndex([c.card_id for c in self.cards], [index_text(c) for c in self.cards], vectors,
                                    embedder, self.settings.embedding.hybrid_alpha)
        return [(self._by_id[cid], score) for cid, score in self._index.search(query, k)]

    def _vectors(self):
        """The workbooks' card vectors stacked in card order, when every workbook has them; else keyword search."""
        paths = [f["handle"].folder / VECTORS_FILE for f in self.files]
        if not self.cards or not all(p.exists() for p in paths):
            return None, None
        embedder = Embedder(self.settings.embedding.model_path)
        if embedder.model is None:
            return None, None
        import numpy as np
        parts = [np.load(p) for p in paths]
        if len({p.shape[1] for p in parts}) != 1 or sum(len(p) for p in parts) != len(self.cards):
            return None, None
        return np.vstack(parts), embedder

    def summary(self, max_titles: int = 30) -> str:
        n = len(self.cards)
        lines = [f"A project of {len(self.files)} workbook files with {n} queryable table{'s' if n != 1 else ''} in "
                 "all. Each table name starts with its file's short name and two underscores; tables from different "
                 "files can be joined or compared in one query. Say which file each figure comes from.",
                 "Files:"]
        for f in self.files:
            h = f["handle"]
            sheets = list(dict.fromkeys(s["sheet"] for c in h.cards for s in c.sources))[:20]
            lines.append(f"- {f['alias']}: {f['name']} ({len(h.cards)} tables; sheets: {', '.join(sheets)})")
        if n <= max_titles:
            lines.append("Tables:")
            lines += [f"- {c.card_id}: {c.title} ({c.row_count} rows"
                      f"{', ' + c.time_coverage if c.time_coverage else ''})" for c in self.cards]
        else:
            lines.append("Too many tables to list here: use search_catalog to find the relevant ones.")
        return "\n".join(lines)

    def sources(self, sql_name: str, values: set[str] | None = None) -> list[dict]:
        hit = self._origin.get(sql_name)
        if hit is None:
            return []
        f, old = hit
        return [{**s, "card_id": self._by_sql[sql_name].card_id, "workbook": f["name"], "workbook_id": f["id"]}
                for s in f["handle"].sources(old, values)]
