"""Command line: sheetir sample | build | trace | sql."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .build import build_ir
from .refs import addr, parse_ref
from .sample import make_quotation
from .store import TRACE_SQL


def _cmd_sample(args) -> int:
    print(f"wrote {make_quotation(args.out)}")
    return 0


def _cmd_build(args) -> int:
    xlsx = Path(args.xlsx)
    db = Path(args.out) if args.out else xlsx.with_suffix(".sheetir.db")
    res = build_ir(xlsx, db, args.tables)
    print(f"SheetIR -> {res.db_path}")
    print("  " + ", ".join(f"{k}={v}" for k, v in res.counts.items()))
    print("checks:")
    for name, passed, severity, detail in res.checks:
        mark = "PASS" if passed else ("FAIL" if severity == "error" else "WARN")
        print(f"  [{mark}] {name}: {detail}")
    print("issues:")
    for kind, n in sorted(res.issues_by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<30} {n}")
    v = res.verification
    if v and v["status"] == "unverified":
        print(f"verification: UNVERIFIED ({v['error']})")
    elif v:
        c, cov = v["checks"], v["coverage"]
        print(f"verification: {v['status'].upper()} ({v['seconds']}s, workbook re-read independently)")
        print(f"  content    {c['content']['matched']}/{c['content']['cells']} cells match")
        print(f"  rebuild    {c['rebuild']['identical']}/{c['rebuild']['cells']} cells identical when rebuilt from the IR")
        print(f"  references {c['references']['agree']}/{c['references']['checked']} formulas read the same cells"
              + (f" ({c['references']['skipped']} not checkable)" if c['references']['skipped'] else ""))
        print(f"  groups     {c['groups']['ok']}/{c['groups']['members']} members reproduce their group's formula")
        print(f"  coverage   {cov['understood']}/{cov['formulas']} formulas fully understood"
              + (f"; not modeled: {', '.join(str(x['count']) + ' ' + x['kind'] for x in cov['ignored'])}" if cov['ignored'] else ""))
    ok = all(p or s != "error" for _, p, s, _ in res.checks)
    return 0 if ok and (not v or v["status"] != "check_failed") else 1


def _cmd_trace(args) -> int:
    con = sqlite3.connect(args.db)
    ref = parse_ref(args.cell)
    if ref is None or not ref.is_cell or ref.sheet is None:
        raise SystemExit(f"expected a sheet-qualified cell like Quote!J5, got {args.cell!r}")
    sheets = {s.lower(): s for (s,) in con.execute("SELECT DISTINCT sheet FROM cells")}
    cell = addr(sheets.get(ref.sheet.lower(), ref.sheet), ref.r1, ref.c1)
    for cell_id, depth, role, semantic, formula, value in con.execute(TRACE_SQL, (cell, args.depth)):
        what = formula or value or ""
        label = f"  ({semantic})" if semantic else ""
        print(f"{'  ' * depth}{cell_id} [{role or 'empty'}]{label}  {what}")
    return 0


def _cmd_sql(args) -> int:
    con = sqlite3.connect(args.db)
    cur = con.execute(args.query)
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchall()
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(cols)]
    widths = [min(w, 60) for w in widths]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(v)[:60].ljust(w) for v, w in zip(r, widths)))
    return 0


def _cmd_serve(args) -> int:
    import os

    import uvicorn

    os.environ["SHEETIR_DATA"] = args.data
    print(f"SheetTwin UI: http://{args.host}:{args.port}  (data dir: {Path(args.data).resolve()})")
    uvicorn.run("sheetir.web.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="sheetir", description="Excel workbook -> queryable SheetIR (SQLite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="write the synthetic quotation workbook")
    s.add_argument("out", nargs="?", default="samples/quotation.xlsx")
    s.set_defaults(fn=_cmd_sample)

    b = sub.add_parser("build", help="parse a workbook into a SheetIR database")
    b.add_argument("xlsx")
    b.add_argument("-o", "--out")
    b.add_argument("--tables", choices=["hand", "model"], default="hand",
                   help="how header rows are recognized: hand-set weights (default) or the model trained on StatSheets")
    b.set_defaults(fn=_cmd_build)

    t = sub.add_parser("trace", help="show the precedent chain of a cell")
    t.add_argument("db")
    t.add_argument("cell", help="e.g. Quote!J5")
    t.add_argument("--depth", type=int, default=10)
    t.set_defaults(fn=_cmd_trace)

    q = sub.add_parser("sql", help="run a SQL query against a SheetIR database")
    q.add_argument("db")
    q.add_argument("query")
    q.set_defaults(fn=_cmd_sql)

    w = sub.add_parser("serve", help="start the local web UI")
    w.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to share on the LAN")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--data", default="data", help="where uploads and SheetIR databases are kept")
    w.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
