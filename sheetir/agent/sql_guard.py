"""run_sql's guard (design §7.3.1).

1. Parsed with sqlglot (DuckDB dialect): exactly one statement, a SELECT (WITH … SELECT, UNION and the like included).
2. No function that reads files or reaches outside the database (read_csv, read_parquet, glob, …), and every table it
   reads is a catalog table or view, or a CTE it defines.
3. Run on a read-only connection with external access disabled (CatalogHandle.connect).
4. Interrupted after agent.sql_timeout_s seconds.
5. At most agent.max_rows_return rows are kept; the true row count is reported.
"""

from __future__ import annotations

import threading
import time

import duckdb
import sqlglot
from sqlglot import exp

_BLOCKED_PREFIXES = ("read_", "sniff_", "parquet_", "iceberg_", "delta_", "sqlite_", "postgres_", "mysql_")
_BLOCKED = {"glob", "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_ndjson",
            "read_text", "read_blob", "parquet_scan", "csv_scan", "sqlite_scan", "postgres_scan", "mysql_scan",
            "getenv", "duckdb_secrets", "load_extension", "install_extension", "query", "query_table"}


class SQLRejected(ValueError):
    pass


def check_sql(sql: str, allowed: set[str]) -> list[str]:
    """The tables and views `sql` reads (lowercase). Raises SQLRejected with the reason when it may not run."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SQLRejected(f"syntax error: {str(e).splitlines()[0][:300]}") from e
    if len(statements) != 1:
        raise SQLRejected("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise SQLRejected(f"only SELECT queries are allowed, not {stmt.key.upper()}")
    for node in stmt.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command,
                             exp.Copy, exp.Pragma, exp.Set, exp.Use, exp.Attach, exp.Detach)):
            raise SQLRejected(f"{node.key.upper()} is not allowed")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _BLOCKED or name.startswith(_BLOCKED_PREFIXES):
                raise SQLRejected(f"function {name} is not allowed: query the catalog's tables only")
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    allowed_l = {a.lower() for a in allowed}
    used = []
    for t in stmt.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise SQLRejected("tables must be named: table functions and file paths are not allowed")
        name = t.name.lower()
        if t.args.get("db") or t.args.get("catalog"):
            raise SQLRejected(f"{t.sql()}: use the catalog's table names without a schema")
        if name in ctes:
            continue
        if name not in allowed_l:
            raise SQLRejected(f"unknown table {t.name!r}: use a table or view from the catalog")
        used.append(name)
    return list(dict.fromkeys(used))


def run_sql(con, sql: str, timeout_s: float, max_rows: int) -> dict:
    """Run a checked query: {columns, rows, row_count, truncated, seconds}. Raises duckdb.Error, or TimeoutError."""
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    t0 = time.perf_counter()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        count = len(rows)
        if truncated:
            count = con.execute(f"SELECT COUNT(*) FROM ({sql.strip().rstrip(';')}) AS q").fetchone()[0]
    except duckdb.InterruptException as e:
        raise TimeoutError(f"the query ran longer than {timeout_s:g} s and was stopped") from e
    finally:
        timer.cancel()
    return {"columns": columns, "rows": [list(r) for r in rows], "row_count": count, "truncated": truncated,
            "seconds": round(time.perf_counter() - t0, 3)}
