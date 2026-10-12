"""Persist the IR as a single SQLite file, queryable with plain SQL."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# Bumped when the tables below change; the web UI offers to re-parse databases built with an older one.
IR_SCHEMA = 4  # 3: meta carries parse_status, verification and load_warnings; 4: ignored_issues

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE cells(
    cell_id TEXT PRIMARY KEY, sheet TEXT, row INT, col INT, a1 TEXT,
    vtype TEXT, role TEXT, value TEXT, cached TEXT, formula TEXT, r1c1 TEXT,
    group_id INT, table_id INT, semantic TEXT, number_format TEXT);
CREATE TABLE formula_groups(
    group_id INT PRIMARY KEY, sheet TEXT, axis TEXT, anchor TEXT, last TEXT,
    n_cells INT, r1c1 TEXT, template TEXT);
CREATE TABLE ranges(range_id TEXT PRIMARY KEY, sheet TEXT, r1 INT, c1 INT, r2 INT, c2 INT);
CREATE TABLE range_cells(range_id TEXT, cell_id TEXT);
CREATE TABLE edges(src_kind TEXT, src_id TEXT, dst_cell TEXT, via TEXT);
CREATE TABLE literals(cell_id TEXT, value REAL, function TEXT);  -- function = enclosing call
CREATE TABLE tables(
    table_id INT PRIMARY KEY, sheet TEXT, name TEXT, kind TEXT, source TEXT,
    header_row INT, row_start INT, row_end INT, col_start INT, col_end INT,
    key_col INT, total_row INT, header_top INT, confidence REAL);
CREATE TABLE fields(
    field_id INTEGER PRIMARY KEY, table_id INT, col INT, header TEXT, name TEXT,
    unit TEXT, kind TEXT, r1c1 TEXT, number_format TEXT);
CREATE TABLE params(param_id INTEGER PRIMARY KEY, cell_id TEXT, label TEXT, name TEXT, unit TEXT, domain TEXT);
CREATE TABLE relations(
    relation_id INTEGER PRIMARY KEY, cell_id TEXT, group_id INT, function TEXT, kind TEXT, match TEXT,
    value TEXT, value_cell TEXT, key_range TEXT, return_range TEXT,
    from_table INT, from_field TEXT, to_table INT, key_field TEXT, return_field TEXT);
CREATE TABLE names(name TEXT, scope TEXT, refers_to TEXT);
CREATE TABLE issues(issue_id INTEGER PRIMARY KEY, cell_id TEXT, group_id INT, kind TEXT, severity TEXT, detail TEXT);
-- Issues the user marked as intended: kept out of `issues`, so nothing reports them, but still queryable.
CREATE TABLE ignored_issues(cell_id TEXT, group_id INT, kind TEXT, severity TEXT, detail TEXT, note TEXT, ignored_at TEXT);
CREATE TABLE checks(name TEXT, passed INT, severity TEXT, detail TEXT);

CREATE INDEX ix_edges_dst ON edges(dst_cell);
CREATE INDEX ix_edges_src ON edges(src_id);
CREATE INDEX ix_range_cells ON range_cells(range_id);
CREATE INDEX ix_relations_cell ON relations(cell_id);

-- Cell-level dependency view: precedent -> dependent, ranges expanded.
CREATE VIEW deps AS
    SELECT src_id AS src_cell, dst_cell, via FROM edges WHERE src_kind = 'cell'
    UNION
    SELECT rc.cell_id, e.dst_cell, e.via FROM edges e
    JOIN range_cells rc ON rc.range_id = e.src_id WHERE e.src_kind = 'range';

-- Table-level links: relations between detected tables, one row per distinct pattern.
CREATE VIEW table_links AS
    SELECT ft.name AS from_table, r.from_field, tt.name AS to_table, r.key_field, r.return_field,
           r.function, r.kind, r.match, COUNT(*) AS n_cells, MIN(r.cell_id) AS example
    FROM relations r JOIN tables tt ON tt.table_id = r.to_table
    LEFT JOIN tables ft ON ft.table_id = r.from_table
    GROUP BY ft.name, r.from_field, tt.name, r.key_field, r.return_field, r.function, r.kind, r.match;
"""

TRACE_SQL = """
WITH RECURSIVE trace(cell_id, depth) AS (
    SELECT ?, 0
    UNION
    SELECT d.src_cell, t.depth + 1 FROM deps d JOIN trace t ON d.dst_cell = t.cell_id
    WHERE t.depth < ?
)
SELECT t.cell_id, MIN(t.depth) AS depth, c.role, c.semantic, c.formula, c.value
FROM trace t LEFT JOIN cells c ON c.cell_id = t.cell_id
GROUP BY t.cell_id ORDER BY depth, c.sheet, c.col, c.row, t.cell_id
"""


def dumps(v) -> str | None:
    return None if v is None else json.dumps(v, default=str, ensure_ascii=False)


def write(db_path: str | Path, rows: dict[str, list[tuple]]) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        for table, data in rows.items():
            if data:
                marks = ",".join("?" * len(data[0]))
                con.executemany(f"INSERT INTO {table} VALUES ({marks})", data)
        con.commit()
    finally:
        con.close()
