"""Dependency graphs over one SheetIR database, shaped for drawing.

overview(): one node per block (formula group, table data column, or standalone input cell),
            edges aggregated from cell-level deps. Small enough to read at a glance.
focus():    cell-level neighbourhood of one cell (precedents up, dependents down).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_ROW_KEY = re.compile(r'\["(?:[^"\\]|\\.)*"\]')
MAX_FOCUS_NODES = 150

DOWN_SQL = """
WITH RECURSIVE down(cell_id, depth) AS (
    SELECT ?, 0
    UNION
    SELECT d.dst_cell, t.depth + 1 FROM deps d JOIN down t ON d.src_cell = t.cell_id
    WHERE t.depth < ?
)
SELECT cell_id, MIN(depth) AS depth FROM down GROUP BY cell_id
"""
UP_SQL = """
WITH RECURSIVE up(cell_id, depth) AS (
    SELECT ?, 0
    UNION
    SELECT d.src_cell, t.depth + 1 FROM deps d JOIN up t ON d.dst_cell = t.cell_id
    WHERE t.depth < ?
)
SELECT cell_id, MIN(depth) AS depth FROM up GROUP BY cell_id
"""


def _block_label(semantics: list[str | None], fallback: str) -> str:
    """Name a block from its cells' semantic addresses.

    quote["A"].margin, quote["B"].margin -> quote[*].margin (a name covering most cells wins);
    quote.total.price, quote.total.unit_cost -> quote.total.* (common prefix).
    """
    sem = [s for s in semantics if s]
    if len(sem) == 1:
        return sem[0]
    counts = Counter(_ROW_KEY.sub("[*]", s) for s in sem)
    if not counts:
        return fallback
    name, n = counts.most_common(1)[0]
    if n * 2 > len(sem):
        return name
    names = sorted(counts)
    prefix = names[0]
    for other in names[1:]:
        while not other.startswith(prefix):
            prefix = prefix[:-1]
    prefix = prefix[: prefix.rfind(".") + 1]
    return f"{prefix}*" if prefix else fallback


def _issue_summary(issues: list[dict]) -> dict | None:
    if not issues:
        return None
    worst = min(issues, key=lambda i: _SEV_RANK.get(i["severity"], 9))
    return {"count": len(issues), "severity": worst["severity"],
            "kinds": sorted({i["kind"] for i in issues})}


def overview(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    cells = {r["cell_id"]: dict(r) for r in con.execute(
        "SELECT cell_id, sheet, row, col, role, vtype, value, formula, group_id, table_id, semantic FROM cells")}
    groups = {r["group_id"]: dict(r) for r in con.execute("SELECT * FROM formula_groups")}
    field_kind = {(r["table_id"], r["col"]): (r["kind"], r["name"]) for r in con.execute(
        "SELECT table_id, col, kind, name FROM fields")}
    tables = {r["table_id"]: dict(r) for r in con.execute("SELECT * FROM tables")}

    def block_of(cid: str) -> str:
        c = cells.get(cid)
        if c is None:
            return f"c:{cid}"
        if c["group_id"] is not None:
            return f"g:{c['group_id']}"
        t = tables.get(c["table_id"])
        if t and t["row_start"] <= c["row"] <= t["row_end"] and field_kind.get((t["table_id"], c["col"]), ("",))[0] == "data":
            return f"f:{t['table_id']}:{c['col']}"
        return f"c:{cid}"

    weights: dict[tuple[str, str], int] = defaultdict(int)
    members: dict[str, set[str]] = defaultdict(set)
    for src, dst in con.execute("SELECT src_cell, dst_cell FROM deps"):
        a, b = block_of(src), block_of(dst)
        members[a].add(src)
        members[b].add(dst)
        if a != b:
            weights[(a, b)] += 1

    issues_by_block: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute("SELECT cell_id, kind, severity FROM issues WHERE cell_id IS NOT NULL"):
        issues_by_block[block_of(r["cell_id"])].append(dict(r))

    nodes = []
    for bid, cids in members.items():
        ordered = sorted(cids, key=lambda x: (cells.get(x, {}).get("row", 0), cells.get(x, {}).get("col", 0)))
        anchor = ordered[0]
        c = cells.get(anchor, {"sheet": anchor.split("!")[0], "role": "empty", "semantic": None})
        node = {"id": bid, "anchor": anchor, "sheet": c["sheet"], "cells": len(cids),
                "issues": _issue_summary(issues_by_block.get(bid, []))}
        if bid.startswith("g:"):
            g = groups[int(bid[2:])]
            gcells = [x for x, v in cells.items() if v["group_id"] == g["group_id"]]
            roles = {cells[x]["role"] for x in gcells}
            node.update(kind="output" if roles == {"output"} else "formula", cells=g["n_cells"],
                        label=_block_label([cells[x]["semantic"] for x in gcells], g["anchor"]),
                        range=g["anchor"] if g["n_cells"] == 1 else f"{g['anchor']}:{g['last'].split('!')[1]}",
                        formula=g["r1c1"], example=cells[g["anchor"]]["formula"])
        elif bid.startswith("f:"):
            t = tables[int(bid.split(":")[1])]
            name = field_kind[(t["table_id"], c["col"])][1]
            node.update(kind="input", label=f"{t['name']}[*].{name}",
                        range=f"{ordered[0]}:{ordered[-1].split('!')[1]}" if len(ordered) > 1 else anchor)
        else:
            role = c.get("role")
            node.update(kind="output" if role == "output" else ("formula" if c.get("formula") else "param"),
                        label=c.get("semantic") or anchor, range=anchor, formula=c.get("formula"),
                        value=json.loads(c["value"]) if c.get("value") else None)
        nodes.append(node)
    edges = [{"source": a, "target": b, "weight": w} for (a, b), w in weights.items()]
    return {"mode": "overview", "nodes": nodes, "edges": edges}


def focus(con: sqlite3.Connection, cell_id: str, up: int = 3, down: int = 2) -> dict:
    con.row_factory = sqlite3.Row
    depth: dict[str, int] = {}
    for r in con.execute(UP_SQL, (cell_id, up)):
        depth[r["cell_id"]] = r["depth"]
    for r in con.execute(DOWN_SQL, (cell_id, down)):
        depth.setdefault(r["cell_id"], -r["depth"])
    truncated = len(depth) > MAX_FOCUS_NODES
    if truncated:  # keep the closest cells
        depth = dict(sorted(depth.items(), key=lambda kv: abs(kv[1]))[:MAX_FOCUS_NODES])
    marks = ",".join("?" * len(depth))
    ids = list(depth)
    cells = {r["cell_id"]: dict(r) for r in con.execute(
        f"SELECT cell_id, sheet, role, formula, value, semantic FROM cells WHERE cell_id IN ({marks})", ids)}
    issues: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute(f"SELECT cell_id, kind, severity FROM issues WHERE cell_id IN ({marks})", ids):
        issues[r["cell_id"]].append(dict(r))
    nodes = []
    for cid, d in depth.items():
        c = cells.get(cid, {"sheet": cid.split("!")[0], "role": "empty", "formula": None, "value": None, "semantic": None})
        role = c["role"]
        kind = {"output": "output", "derived": "formula"}.get(role, "formula" if c["formula"] else "param")
        nodes.append({"id": cid, "anchor": cid, "kind": kind, "label": c["semantic"] or cid, "range": cid,
                      "sheet": c["sheet"], "cells": 1, "formula": c["formula"], "depth": d, "focus": cid == cell_id,
                      "value": json.loads(c["value"]) if c["value"] else None,
                      "issues": _issue_summary(issues.get(cid, []))})
    edges = [{"source": s, "target": t, "weight": 1} for s, t in con.execute(
        f"SELECT DISTINCT src_cell, dst_cell FROM deps WHERE src_cell IN ({marks}) AND dst_cell IN ({marks})", ids + ids)]
    return {"mode": "focus", "focus": cell_id, "nodes": nodes, "edges": edges, "truncated": truncated}
