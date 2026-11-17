"""Build and open a workbook's catalog (design §6.4–6.8).

    build_catalog(ir_db, folder, settings, llm)  ->  CatalogHandle
    CatalogHandle.open(folder)                    ->  CatalogHandle | None (none built, or built by another version)

Files, next to the IR database in the workbook's folder (the folder is named after the file's sha256, so this is the
cache of design §6.8; a catalog built by another PIPELINE_VERSION is ignored and rebuilt):

    data.duckdb      clean tables t_<table_id> and family views v_fam_<name>
    catalog.json     table specs, clean tables, families, relations, cards, build statistics
    index.npy        card embeddings, when a local embedding model is available

Steps: table specs from the IR → (language model for unsure tables and uncovered blocks) → clean → DuckDB → families
→ relations → cards (rules, then the language model) → index. A table that fails to clean is reported in `errors` and
left out; the rest of the catalog is still built.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path

import duckdb

from ..clean.models import CleanTable
from ..clean.normalize import SheetGrid, refine_spec, to_clean_table
from ..config import Settings, get_settings
from ..detect.llm_detect import llm_detect, uncovered_blocks
from ..detect.models import TableSpec, a1, specs_from_ir
from ..recalc import recalc_missing
from .cards import index_text, llm_describe, relation_text, rule_card
from .families import find_families, view_sql
from .index import CardIndex, Embedder
from .models import CatalogCard, Relation, TableFamily
from .relations import column_infos, find_relations

PIPELINE_VERSION = 2  # 2: one form for a family's periods
CATALOG_FILE, DB_FILE, VECTORS_FILE = "catalog.json", "data.duckdb", "index.npy"
_SQL_TYPE = {"int": "BIGINT", "float": "DOUBLE", "text": "VARCHAR", "date": "DATE", "bool": "BOOLEAN"}


# ---- build ---------------------------------------------------------------------------------------------------------

def _renumber(specs: list[TableSpec], order: dict[str, int]) -> list[TableSpec]:
    from ..clean.headers import slug

    specs = sorted(specs, key=lambda s: (order.get(s.sheet, 1e9), s.bounds[1], s.bounds[0]))
    seen: dict[str, int] = {}
    out = []
    for s in specs:
        seen[s.sheet] = seen.get(s.sheet, 0) + 1
        out.append(s.model_copy(update={"table_id": f"{slug(s.sheet)}__t{seen[s.sheet]}"}))
    return out


def _confirm_relation(llm):
    from pydantic import BaseModel

    class Same(BaseModel):
        same_entity: bool
        reason: str

    system = ("Decide whether two columns from different tables refer to the same entity and can be joined. "
              'Answer JSON: {"same_entity": true|false, "reason": "<one sentence>"}.')

    def confirm(a, b, containment) -> bool:
        user = (f"Column A: {a.obj}.{a.name} — samples {sorted(a.values)[:8]}\n"
                f"Column B: {b.obj}.{b.name} — samples {sorted(b.values)[:8]}\n"
                f"Value containment A→B: {containment:.2f}")
        try:
            return llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], Same,
                                 name="confirm_relation").same_entity
        except Exception:  # noqa: BLE001
            return False
    return confirm


def _formula_pairs(con: sqlite3.Connection, tables: list[CleanTable], obj_of: dict[str, str]) -> set:
    """Column pairs a workbook lookup (VLOOKUP, INDEX/MATCH, SUMIF…) joins, as ((object, column), (object, column))."""
    by_ir = {t.spec.ir_table_id: t for t in tables if t.spec.ir_table_id is not None}
    fields = {(tid, name): col for tid, name, col in con.execute("SELECT table_id, name, col FROM fields")}
    pairs = set()
    for ft, ff, tt, kf in con.execute("SELECT DISTINCT from_table, from_field, to_table, key_field FROM relations "
                                      "WHERE from_table IS NOT NULL AND to_table IS NOT NULL AND key_field IS NOT NULL"):
        a, b = by_ir.get(ft), by_ir.get(tt)
        ca, cb = fields.get((ft, ff)), fields.get((tt, kf))
        if not (a and b and ca and cb):
            continue
        na = next((c.name for c in a.columns if c.source_col == ca), None)
        nb = next((c.name for c in b.columns if c.source_col == cb), None)
        if na and nb:
            pairs.add(((obj_of[a.table_id], na), (obj_of[b.table_id], nb)))
    return pairs


def _sample_rows(dcon, sql_name: str, n: int = 5) -> list[dict]:
    cur = dcon.execute(f'SELECT * FROM "{sql_name}" LIMIT {n}')
    cols = [d[0] for d in cur.description]
    return [{k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in zip(cols, r)
             if k != "_row"} for r in cur.fetchall()]


def _time_coverage(dcon, sql_name: str, family: TableFamily | None, columns) -> str | None:
    if family is not None and family.discriminator == "period":
        vals = sorted(family.member_values.values())
        return f"{vals[0]} → {vals[-1]}"
    for c in columns:
        if c.dtype == "date":
            lo, hi = dcon.execute(f'SELECT MIN("{c.name}"), MAX("{c.name}") FROM "{sql_name}"').fetchone()
            if lo is not None:
                return f"{lo} → {hi}"
    return None


def build_catalog(ir_db: str | Path, folder: str | Path, settings: Settings | None = None, llm=None,
                  progress=None) -> "CatalogHandle":
    s = settings or get_settings()
    folder = Path(folder)
    say = progress or (lambda stage, info=None: None)
    t0 = time.perf_counter()
    con = sqlite3.connect(f"file:{Path(ir_db).as_posix()}?mode=ro", uri=True)
    errors: list[str] = []
    try:
        order = {sh: i for i, sh in enumerate(json.loads(
            (con.execute("SELECT value FROM meta WHERE key = 'sheets'").fetchone() or ["[]"])[0]))}
        sha = (con.execute("SELECT value FROM meta WHERE key = 'sha256'").fetchone() or [None])[0]
        specs = specs_from_ir(con)
        # formulas Excel stored no result for (a file written by another program): computed from the workbook
        missing = con.execute("SELECT COUNT(*) FROM cells WHERE formula IS NOT NULL AND cached IS NULL").fetchone()[0]
        computed = recalc_missing(con) if missing else {}
        grids = {sh: SheetGrid.load(con, sh, computed) for sh in order}
        n_rule = len(specs)

        # ---- language model for unsure tables and blocks no table covers (design §6.3.4) ----------------------------
        n_llm, unsure = 0, [sp for sp in specs if sp.source == "rule:heuristic"
                            and sp.confidence < s.detect.accept_threshold]
        if llm is not None:
            jobs = []
            for sp in unsure:
                c1, r1, c2, r2 = sp.bounds
                used = grids[sp.sheet].used_range() or (c1, r1, c2, r2)
                region = (c1, max(used[1], r1 - 2), c2, min(used[3], r2 + 2))
                jobs.append(("unsure", sp, region))
            for sh, g in grids.items():
                for box in uncovered_blocks(g, [x for x in specs if x.sheet == sh]):
                    jobs.append(("uncovered", None, (sh, box)))
            jobs = jobs[:40]  # a bound on model calls per workbook

            def run(job):
                kind, sp, region = job
                if kind == "unsure":
                    others = [x for x in specs if x.sheet == sp.sheet and x is not sp]
                    return llm_detect(grids[sp.sheet], region, sp, others, llm, s.detect.llm_retries,
                                      s.detect.max_tokens_per_block * 4)
                sh, box = region
                return llm_detect(grids[sh], box, None, [x for x in specs if x.sheet == sh], llm,
                                  s.detect.llm_retries, s.detect.max_tokens_per_block * 4)

            results = llm.map(run, jobs) if jobs else []
            replaced: dict[int, list[TableSpec]] = {}
            added: list[TableSpec] = []
            for (kind, sp, _), res in zip(jobs, results):
                if kind == "unsure":
                    if res:  # the model's reading replaces the rules'
                        replaced[id(sp)] = [r.model_copy(update={"name": sp.name}) for r in res]
                elif res:
                    added += res
            new_specs = []
            for sp in specs:
                if id(sp) in replaced:
                    new_specs += replaced[id(sp)]
                    n_llm += len(replaced[id(sp)])
                else:
                    if sp in unsure:
                        sp = sp.model_copy(update={"low_confidence": True})
                    new_specs.append(sp)
            # tables added for uncovered blocks must not overlap each other or the rest
            for a in added:
                ac1, ar1, ac2, ar2 = a.bounds
                if all(not (x.sheet == a.sheet and not (ac2 < x.bounds[0] or x.bounds[2] < ac1 or ar2 < x.bounds[1]
                                                       or x.bounds[3] < ar1)) for x in new_specs):
                    new_specs.append(a)
                    n_llm += 1
            specs = new_specs
        else:
            specs = [sp.model_copy(update={"low_confidence": True}) if sp in unsure else sp for sp in specs]
        specs = _renumber([refine_spec(sp, grids[sp.sheet]) for sp in specs], order)
        say("detect", {"tables": len(specs), "rule": n_rule, "llm": n_llm,
                       "low_confidence": sum(sp.low_confidence for sp in specs)})

        # ---- clean into DuckDB --------------------------------------------------------------------------------------
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (DB_FILE + ".tmp")
        for p in (tmp, Path(str(tmp) + ".wal")):
            if p.exists():
                p.unlink()
        dcon = duckdb.connect(str(tmp))
        cleaned: list[CleanTable] = []
        try:
            for sp in specs:
                try:
                    names, rows, ct = to_clean_table(sp, grids[sp.sheet], f"t_{sp.table_id}",
                                                     s.clean.ffill_vertical_merges)
                except Exception as e:  # noqa: BLE001 - one odd table must not cost the catalog
                    errors.append(f"{sp.sheet}!{sp.range}: {type(e).__name__}: {e}")
                    continue
                if not ct.n_rows:
                    errors.append(f"{sp.sheet}!{sp.range}: no data rows")
                    continue
                types = ["INTEGER"] + (["VARCHAR"] if names[1:2] == ["section"] else [])
                types += [_SQL_TYPE[c.dtype] for c in ct.columns if c.source_col is not None]
                cols = ", ".join(f'"{n}" {t}' for n, t in zip(names, types))
                dcon.execute(f'CREATE TABLE "{ct.duckdb_name}" ({cols})')
                marks = ", ".join("?" * len(names))
                for i in range(0, len(rows), 5000):
                    dcon.executemany(f'INSERT INTO "{ct.duckdb_name}" VALUES ({marks})', rows[i:i + 5000])
                cleaned.append(ct)
            say("clean", {"tables": len(cleaned), "rows": sum(t.n_rows for t in cleaned)})

            # ---- families (design §6.5.1) -------------------------------------------------------------------------
            by_id = {t.table_id: t for t in cleaned}
            families = find_families(cleaned, s.relations.family_jaccard)
            for f in families:
                sql, cols_, units, notes = view_sql(f, by_id)
                dcon.execute(sql)
                f.columns, f.units, f.notes = cols_, units, notes
            members = {m: f for f in families for m in f.member_table_ids}
            say("families", {"families": len(families), "tabs": len(members)})

            # objects the agent sees: family views and tables in no family
            objects: list[tuple[str, str, list[CleanTable], TableFamily | None]] = [
                (f.family_id, f.duckdb_view, [by_id[m] for m in f.member_table_ids], f) for f in families]
            objects += [(t.table_id, t.duckdb_name, [t], None) for t in cleaned if t.table_id not in members]
            obj_of = {t.table_id: (members[t.table_id].duckdb_view if t.table_id in members else t.duckdb_name)
                      for t in cleaned}
            n_rows = {sql: sum(t.n_rows for t in ts) for _, sql, ts, _ in objects}

            # ---- relations (design §6.5.2) ------------------------------------------------------------------------
            def obj_columns(ts, fam):
                cols_ = [(c.name, c.dtype) for c in ts[0].columns]
                return cols_ if fam is None else [(n, d) for n, d in cols_ if n in fam.columns]

            infos = column_infos(dcon, [(sql, obj_columns(ts, fam), n_rows[sql]) for _, sql, ts, fam in objects],
                                 s.relations.sample_distinct)
            relations: list[Relation] = find_relations(
                infos, s.relations.llm_confirm_low, s.relations.accept_score, _formula_pairs(con, cleaned, obj_of),
                _confirm_relation(llm) if llm is not None else None)
            say("relations", {"relations": len(relations)})

            # ---- cards (design §6.6) ------------------------------------------------------------------------------
            cards: list[CatalogCard] = []
            for card_id, sql, ts, fam in objects:
                rels = [x for r in relations if (x := relation_text(r, sql))]
                cards.append(rule_card(card_id, sql, ts, fam, _sample_rows(dcon, sql), n_rows[sql],
                                       _time_coverage(dcon, sql, fam, ts[0].columns), rels,
                                       fam.columns if fam else None))
            if llm is not None and cards:
                cards = llm.map(lambda c: llm_describe(c, llm), cards)
            say("cards", {"cards": len(cards), "llm": sum(c.described_by == "llm" for c in cards)})
        finally:
            dcon.close()

        # ---- index (design §6.7) ------------------------------------------------------------------------------------
        embedder = Embedder(s.embedding.model_path)
        vectors = embedder.encode([index_text(c) for c in cards]) if cards else None
        vec_path = folder / VECTORS_FILE
        if vectors is not None:
            import numpy as np
            np.save(vec_path, vectors)
        elif vec_path.exists():
            vec_path.unlink()

        _replace(tmp, folder / DB_FILE)
        data = {
            "version": PIPELINE_VERSION, "built_at": dt.datetime.now().isoformat(timespec="seconds"),
            "sha256": sha, "model": getattr(llm, "model", None),
            "specs": [sp.model_dump() for sp in specs], "tables": [t.model_dump() for t in cleaned],
            "families": [f.model_dump() for f in families], "relations": [r.model_dump() for r in relations],
            "cards": [c.model_dump(mode="json") for c in cards],
            "stats": {"tables": len(cleaned), "rule_tables": n_rule, "llm_tables": n_llm,
                      "low_confidence": sum(sp.low_confidence for sp in specs), "families": len(families),
                      "family_tabs": len(members), "relations": len(relations), "cards": len(cards),
                      "rows": sum(t.n_rows for t in cleaned), "embedding": vectors is not None,
                      "formulas_without_result": missing, "formulas_computed": len(computed),
                      "seconds": round(time.perf_counter() - t0, 2)},
            "errors": errors,
        }
        (folder / CATALOG_FILE).write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    finally:
        con.close()
    say("index", data["stats"])
    return CatalogHandle(folder, s)


def _replace(src: Path, dst: Path) -> None:
    """Move the new database into place. On Windows a reader still holding the old file blocks this for a moment."""
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


# ---- open ----------------------------------------------------------------------------------------------------------

class CatalogHandle:
    """A built catalog: its cards, search, and read-only access to its DuckDB database."""

    def __init__(self, folder: str | Path, settings: Settings | None = None):
        self.folder = Path(folder)
        self.settings = settings or get_settings()
        self.data = json.loads((self.folder / CATALOG_FILE).read_text(encoding="utf-8"))
        self.cards = [CatalogCard(**c) for c in self.data["cards"]]
        self.tables = {t["table_id"]: CleanTable(**t) for t in self.data["tables"]}
        self.families = [TableFamily(**f) for f in self.data["families"]]
        self.relations = [Relation(**r) for r in self.data["relations"]]
        self._by_id = {c.card_id: c for c in self.cards}
        self._by_sql = {c.sql_name: c for c in self.cards}
        self._index = None

    @classmethod
    def open(cls, folder: str | Path, settings: Settings | None = None) -> "CatalogHandle | None":
        p = Path(folder) / CATALOG_FILE
        if not p.exists() or not (Path(folder) / DB_FILE).exists():
            return None
        try:
            h = cls(folder, settings)
        except (ValueError, KeyError, TypeError):
            return None
        return h if h.data.get("version") == PIPELINE_VERSION else None

    @property
    def stats(self) -> dict:
        return self.data.get("stats", {})

    @property
    def db_path(self) -> Path:
        return self.folder / DB_FILE

    def connect(self):
        """Read-only DuckDB connection with no access to files or the network (design §7.3.1)."""
        return duckdb.connect(str(self.db_path), read_only=True, config={"enable_external_access": False})

    def card(self, key: str) -> CatalogCard | None:
        return self._by_id.get(key) or self._by_sql.get(key)

    @property
    def sql_names(self) -> set[str]:
        return set(self._by_sql)

    def search(self, query: str, k: int = 8) -> list[tuple[CatalogCard, float]]:
        if self._index is None:
            texts = [index_text(c) for c in self.cards]
            vectors, embedder = None, None
            vp = self.folder / VECTORS_FILE
            if vp.exists():
                embedder = Embedder(self.settings.embedding.model_path)
                if embedder.model is not None:
                    import numpy as np
                    vectors = np.load(vp)
            self._index = CardIndex([c.card_id for c in self.cards], texts, vectors, embedder,
                                    self.settings.embedding.hybrid_alpha)
        return [(self._by_id[cid], score) for cid, score in self._index.search(query, k)]

    def summary(self, max_titles: int = 30) -> str:
        """For the agent's system prompt (design §7.1): how many tables, and their titles when there are few."""
        n = len(self.cards)
        lines = [f"{n} queryable table{'s' if n != 1 else ''} (families of same-shaped sheets are combined into one "
                 f"view each). Sheets: {', '.join(list(dict.fromkeys(s['sheet'] for c in self.cards for s in c.sources))[:40])}."]
        if n <= max_titles:
            lines += [f"- {c.card_id}: {c.title} ({c.row_count} rows"
                      f"{', ' + c.time_coverage if c.time_coverage else ''})" for c in self.cards]
        else:
            lines.append("Too many to list here: use search_catalog to find the relevant ones.")
        return "\n".join(lines)

    def sources(self, sql_name: str, values: set[str] | None = None) -> list[dict]:
        """The sheets and ranges behind a table or view; for a family, only the members whose discriminator value or
        sheet is in `values` when given."""
        c = self._by_sql.get(sql_name)
        if c is None:
            return []
        out = []
        for s in c.sources:
            if values is not None and s.get("value") not in values and s["sheet"] not in values:
                continue
            out.append({"sheet": s["sheet"], "range": s["range"], "table_title": s.get("title") or c.title,
                        "card_id": c.card_id})
        return out

    def specs(self) -> list[TableSpec]:
        return [TableSpec(**sp) for sp in self.data.get("specs", [])]


__all__ = ["build_catalog", "CatalogHandle", "PIPELINE_VERSION", "a1"]
