"""Pipeline: workbook -> SheetIR (L0..L3) -> self-checks -> SQLite."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl.formula.translate import Translator

from . import __version__, store
from .convert import as_xlsx
from .formula import STRUCTURAL_ARGS, ParsedFormula, parse_formula
from .groups import group_formulas
from .loader import CellKey, RawWorkbook, load
from .refs import Ref, addr, num_to_col, parse_ref
from .relations import Relation, extract_relations
from .store import dumps
from .structure import (
    Table, assign_field_keys, cell_in, classify_roles, detect_tables,
    field_anomalies, find_label, split_label, unique,
)
from .tablemodel import get_scorer
from .verify import NOT_UNDERSTOOD

TRIVIAL_LITERALS = {0.0, 1.0, -1.0, 2.0, 12.0, 100.0, 1000.0}

SEVERITY = {
    "inconsistent_formula": "high", "hardcoded_in_formula_column": "high", "missing_formula": "medium",
    "broken_ref": "high", "external_link": "high", "unknown_sheet": "high", "parse_error": "high",
    "dynamic_ref": "medium", "volatile": "medium", "unresolved_ref": "medium", "unresolved_name": "medium",
    "array_formula": "medium", "data_table": "high", "cycle": "high",
    "hardcoded_constant": "medium", "approximate_lookup": "medium", "orphan": "low",
}


class WorkbookResolver:
    """Resolves sheet names, defined names and structured (Table[Col]) references."""

    _STRUCT = re.compile(r"^(?P<table>[^\[\]]+)?\[(?P<body>.*)\]$")

    def __init__(self, raw: RawWorkbook):
        self._sheets = {s.lower(): s for s in raw.sheets}
        self._names = {}
        for n in raw.names:
            self._names[(n.scope, n.name.upper())] = n.refers_to
        self._tables = {}
        for t in raw.tables:
            ref = parse_ref(t.ref)
            if ref is not None:
                self._tables[t.name.upper()] = (t, ref.with_sheet(t.sheet))

    def sheet(self, name: str) -> str | None:
        return self._sheets.get(name.lower())

    def name(self, name: str, host_sheet: str) -> list[Ref] | None:
        text = self._names.get((host_sheet, name.upper())) or self._names.get((None, name.upper()))
        if text is None:
            hit = self._tables.get(name.upper())
            return [self._data_area(*hit)] if hit else None
        out = []
        for part in text.split(","):
            ref = parse_ref(part)
            if ref is None or ref.sheet is None or self.sheet(ref.sheet) is None:
                return None  # constant or formula name: not a plain range
            out.append(ref.with_sheet(self.sheet(ref.sheet)))
        return out

    def structured(self, text: str, host_sheet: str, row: int, col: int) -> list[Ref] | None:
        m = self._STRUCT.match(text)
        if not m:
            return None
        if m.group("table"):
            hit = self._tables.get(m.group("table").upper())
        else:
            hit = next(((t, r) for t, r in self._tables.values()
                        if r.sheet == host_sheet and r.contains(row, col)), None)
        if hit is None:
            return None
        tdef, area = hit
        body = m.group("body")
        this_row = body.startswith("@") or "#THIS ROW" in body.upper()
        parts = re.findall(r"\[([^\[\]]+)\]", body) or [body]
        cols = [p.lstrip("@").strip() for p in parts if not p.startswith("#")]
        data = self._data_area(tdef, area)
        if not cols:
            return [data]
        out = []
        for name in cols:
            idx = next((i for i, c in enumerate(tdef.columns) if c.upper() == name.upper()), None)
            if idx is None:
                return None
            c = area.c1 + idx
            r1, r2 = (row, row) if this_row else (data.r1, data.r2)
            out.append(Ref(area.sheet, r1, c, r2, c))
        return out

    @staticmethod
    def _data_area(tdef, area: Ref) -> Ref:
        return Ref(area.sheet, area.r1 + tdef.header_rows, area.c1, area.r2 - tdef.totals_rows, area.c2)


@dataclass
class BuildResult:
    db_path: Path
    counts: dict[str, int]
    checks: list[tuple[str, bool, str, str]]
    issues_by_kind: dict[str, int] = field(default_factory=dict)
    verification: dict | None = None  # sheetir.verify report: status, checks, coverage


def _expand(ref: Ref, cells_by_sheet: dict[str, dict[tuple[int, int], CellKey]]) -> list[CellKey]:
    """Non-empty cells covered by a reference (never materializes empty cells)."""
    sheet_cells = cells_by_sheet.get(ref.sheet, {})
    if ref.r1 is not None and ref.c1 is not None and ref.area <= len(sheet_cells):
        return [sheet_cells[(r, c)] for r in range(ref.r1, ref.r2 + 1)
                for c in range(ref.c1, ref.c2 + 1) if (r, c) in sheet_cells]
    return sorted(k for (r, c), k in sheet_cells.items() if ref.contains(r, c))


def _range_id(ref: Ref | None) -> str | None:
    """Sheet-qualified A1 text without `$` anchors, so equal ranges compare equal."""
    return None if ref is None else Ref(ref.sheet, ref.r1, ref.c1, ref.r2, ref.c2).a1()


def _locate(ref: Ref | None, tables: list[Table], axis: str = "col") -> tuple[Table | None, str | None]:
    """Table a range falls in, and the field when the range is one column of it."""
    if ref is None:
        return None, None
    for t in tables:
        top, bottom = t.top, t.bottom
        if (t.sheet == ref.sheet
                and (ref.r1 is None or (ref.r1 <= bottom and ref.r2 >= top))
                and (ref.c1 is None or (ref.c1 <= t.c2 and ref.c2 >= t.c1))):
            field = None
            if axis == "col" and ref.c1 is not None and ref.c1 == ref.c2:
                field = next((f.name for f in t.fields if f.col == ref.c1), None)
            return t, field
    return None, None


def _norm(formula: str) -> str:
    """Whitespace-insensitive, case-insensitive (outside string literals) comparison form."""
    out, in_str = [], False
    for ch in formula:
        if ch == '"':
            in_str = not in_str
        if in_str:
            out.append(ch)
        elif not ch.isspace():
            out.append(ch.upper())
    return "".join(out)


def build_ir(xlsx: str | Path, db_path: str | Path, table_scorer: str = "hand", verify: bool = True,
             user_tables: dict[str, list[dict]] | None = None, ignored: list[dict] | None = None) -> BuildResult:
    """Parse `xlsx` into a SheetIR database. `table_scorer` picks how header rows are recognized:
    "hand" (hand-set weights) or "model" (classifiers trained on StatSheets). With `verify`, the database is then
    checked against the workbook re-read independently (sheetir.verify), and the report stored in its meta table.
    `user_tables` (sheet -> table specs) replaces table detection on those sheets with the user's tables.
    `ignored` ({"cell", "kind", "detail", "note"?, "at"?} each) lists issues the user marked as intended: an issue
    matching one on all three of cell, kind and detail goes to the ignored_issues table instead of issues. Formulas
    SheetIR cannot follow (sheetir.verify.NOT_UNDERSTOOD) are never ignored, so the check's coverage stays honest.
    `xlsx` may also be a .csv, .xls or .ods file: it is converted to an .xlsx first (sheetir.convert), and both the
    parse and the check read that conversion."""
    with tempfile.TemporaryDirectory() as tmp:
        book, notes = as_xlsx(Path(xlsx), Path(tmp))
        return _build(Path(xlsx), book, notes, db_path, table_scorer, verify, user_tables, ignored or [])


def _build(src: Path, book: Path, notes: list[str], db_path: str | Path, table_scorer: str, verify: bool,
           user_tables: dict[str, list[dict]] | None, ignored: list[dict]) -> BuildResult:
    raw = load(book)
    if book != src:
        raw.path, raw.sha256 = src, hashlib.sha256(src.read_bytes()).hexdigest()
    raw.warnings = notes + raw.warnings
    cells = raw.cells
    resolver = WorkbookResolver(raw)

    # ---- L1: parse formulas -------------------------------------------------
    parsed: dict[CellKey, ParsedFormula] = {}
    for ck, c in cells.items():
        if c.formula is not None:
            parsed[ck] = parse_formula(c.formula, c.sheet, c.row, c.col, resolver)
    keys = {ck: pf.key for ck, pf in parsed.items()}
    refs_of = {ck: pf.refs for ck, pf in parsed.items()}

    # ---- L2: groups, tables, references, roles -------------------------------
    groups = group_formulas(keys)
    group_of = {ck: g.group_id for g in groups for ck in g.cells}

    unknown = sorted(set(user_tables or {}) - set(raw.sheets))
    if unknown:
        raise ValueError(f"user tables name sheets that do not exist: {unknown}")
    tables = detect_tables(raw, refs_of, {g.anchor for g in groups if g.axis == "col"}, get_scorer(table_scorer),
                           user_tables)
    for t in tables:
        assign_field_keys(t, keys)

    cells_by_sheet: dict[str, dict[tuple[int, int], CellKey]] = defaultdict(dict)
    for ck in cells:
        cells_by_sheet[ck[0]][(ck[1], ck[2])] = ck

    referenced: set[CellKey] = set()
    direct_refs: set[CellKey] = set()
    ranges: dict[str, Ref] = {}
    range_members: dict[str, list[CellKey]] = {}
    edges: list[tuple] = []
    precedents: dict[CellKey, set[CellKey]] = defaultdict(set)
    for ck, pf in parsed.items():
        dst = addr(*ck)
        for ref, via in pf.refs:
            if ref.is_cell:
                src = (ref.sheet, ref.r1, ref.c1)
                edges.append(("cell", addr(*src), dst, via))
                if src in cells:
                    referenced.add(src)
                    direct_refs.add(src)
                    precedents[ck].add(src)
            else:
                rid = ref.a1()
                if rid not in ranges:
                    ranges[rid] = ref
                    range_members[rid] = _expand(ref, cells_by_sheet)
                edges.append(("range", rid, dst, via))
                referenced.update(range_members[rid])
                precedents[ck].update(range_members[rid])

    roles = classify_roles(cells, referenced)

    table_of: dict[CellKey, Table] = {}
    for t in tables:
        for r in range(t.top, t.bottom + 1):
            for c in range(t.c1, t.c2 + 1):
                table_of[(t.sheet, r, c)] = t
        # A 2-column "label | value" block whose values are referenced one by one
        # is a parameter list, not an entity table.
        value_fields = [f for f in t.fields if f.col != t.key_col]
        if t.key_col is not None and len(value_fields) == 1 and value_fields[0].kind == "data":
            vals = [(t.sheet, r, value_fields[0].col) for r in t.data_rows()]
            if sum(v in direct_refs for v in vals) * 2 >= len(vals):
                t.kind = "parameter_list"

    # ---- relations: lookups and conditional aggregates --------------------------
    relations: list[Relation] = extract_relations(parsed)
    relation_rows = []
    for rel in relations:
        to_t, key_field = _locate(rel.key, tables, rel.key_axis)
        ret_t, ret_field = _locate(rel.ret, tables, rel.key_axis)
        from_t = table_of.get(rel.value_cell) if rel.value_cell else None
        from_field = next((f.name for f in from_t.fields if f.col == rel.value_cell[2]), None) if from_t else None
        relation_rows.append((
            None, addr(*rel.cell), group_of.get(rel.cell), rel.function, rel.kind, rel.match, rel.value,
            addr(*rel.value_cell) if rel.value_cell else None,
            _range_id(rel.key), _range_id(rel.ret),
            from_t.table_id if from_t else None, from_field,
            to_t.table_id if to_t else None, key_field,
            ret_field if ret_t is not None and ret_t is to_t else None,
        ))

    # ---- L3: semantic names --------------------------------------------------
    semantic: dict[CellKey, str] = {}
    params: list[tuple] = []
    taken: set[str] = set()
    names_by_cell: dict[CellKey, str] = {}
    for n in raw.names:
        refs = resolver.name(n.name, n.scope or "")
        if refs and len(refs) == 1 and refs[0].is_cell:
            names_by_cell[(refs[0].sheet, refs[0].r1, refs[0].c1)] = n.name

    def add_param(ck: CellKey, label: str | None):
        guess, unit = split_label(label) if label else (f"{ck[0]}_{num_to_col(ck[2])}{ck[1]}".lower(), None)
        name = unique(split_label(names_by_cell[ck])[0] if ck in names_by_cell else guess, taken)
        domain = next((v.rule for v in raw.validations
                       if v.sheet == ck[0] and cell_in(
                           [r.with_sheet(v.sheet) for r in map(parse_ref, v.sqref.split()) if r], *ck)), None)
        params.append((None, addr(*ck), label, name, unit, dumps(domain)))
        semantic[ck] = f"params.{name}"

    for t in tables:
        if t.kind == "parameter_list":
            vf = next(f for f in t.fields if f.col != t.key_col)
            for r in t.data_rows():
                ck = (t.sheet, r, vf.col)
                if ck in cells:
                    add_param(ck, str(cells[(t.sheet, r, t.key_col)].value))
            continue
        fname = {f.col: f.name for f in t.fields}
        for r in t.data_rows():
            key_cell = cells.get((t.sheet, r, t.key_col)) if t.key_col else None
            row_key = str(key_cell.value) if key_cell else str(r - t.row_start)
            for c in range(t.c1, t.c2 + 1):
                if (t.sheet, r, c) in cells:
                    semantic[(t.sheet, r, c)] = f'{t.name}["{row_key}"].{fname[c]}'
        if t.total_row:
            for c in range(t.c1, t.c2 + 1):
                if (t.sheet, t.total_row, c) in cells:
                    semantic[(t.sheet, t.total_row, c)] = f"{t.name}.total.{fname[c]}"

    calc_taken: set[str] = set()
    for ck in sorted(cells):
        if ck in table_of:
            continue
        if roles[ck] == "input":
            add_param(ck, find_label(cells, *ck))
        elif roles[ck] in ("derived", "output"):
            label = names_by_cell.get(ck) or find_label(cells, *ck)
            if label:
                prefix = "outputs" if roles[ck] == "output" else "calc"
                semantic[ck] = f"{prefix}.{unique(split_label(label)[0], calc_taken)}"

    # ---- issues --------------------------------------------------------------
    issues: list[tuple] = []

    def issue(kind, detail, cell=None, group=None):
        issues.append((None, addr(*cell) if cell else None, group, kind, SEVERITY.get(kind, "low"), detail))

    for ck, pf in parsed.items():
        for kind, detail in pf.issues:
            issue(kind, detail, ck)
        for flag in cells[ck].flags:
            issue(flag, cells[ck].formula, ck)
    for t in tables:
        if t.kind == "data":
            for ck, kind, detail in field_anomalies(t, cells, keys):
                issue(kind, detail, ck)
    for g in groups:
        pf = parsed[g.anchor]
        consts = sorted({v for v, fn in pf.literals if v not in TRIVIAL_LITERALS and fn not in STRUCTURAL_ARGS})
        for v in consts:
            issue("hardcoded_constant", f"literal {v:g} in {g.key} (applies to {len(g.cells)} cells)",
                  g.anchor, g.group_id)
    flagged_groups: set[int] = set()
    for rel in relations:
        if rel.kind != "lookup" or rel.match != "approximate" or rel.key is None:
            continue
        g = group_of.get(rel.cell)
        if g in flagged_groups:
            continue
        keys_in_range = [cells[k] for k in _expand(rel.key, cells_by_sheet)]
        if sum(c.vtype == "text" for c in keys_in_range) * 2 > len(keys_in_range):
            flagged_groups.add(g)
            issue("approximate_lookup", f"{rel.function} uses approximate match on text keys in {_range_id(rel.key)}; "
                  "unsorted keys give wrong answers, add an exact-match argument", rel.cell, g)
    for ck, role in roles.items():
        if role == "orphan":
            issue("orphan", f"constant {cells[ck].value!r} is not used by any formula", ck)

    waived = {(i["cell"], i["kind"], i["detail"]): i for i in ignored if i["kind"] not in NOT_UNDERSTOOD}
    ignored_rows = []
    for row in list(issues):
        w = waived.get((row[1], row[3], row[5]))
        if w is not None:
            issues.remove(row)
            ignored_rows.append((row[1], row[2], row[3], row[4], row[5], w.get("note"), w.get("at")))

    # ---- self-checks -----------------------------------------------------------
    checks: list[tuple[str, bool, str, str]] = []
    missing = [ck for ck in parsed if ck not in group_of]
    checks.append(("formula_coverage", not missing, "error",
                   f"{len(parsed) - len(missing)}/{len(parsed)} formula cells assigned to a group"))

    # Independent oracle: openpyxl's Translator must re-derive every member from its anchor.
    bad = []
    for g in groups:
        if len(g.cells) < 2:
            continue
        a_sheet, a_row, a_col = g.anchor
        anchor_formula = cells[g.anchor].formula
        for ck in g.cells[1:]:
            moved = Translator(anchor_formula, origin=f"{num_to_col(a_col)}{a_row}").translate_formula(
                f"{num_to_col(ck[2])}{ck[1]}")
            if _norm(moved) != _norm(cells[ck].formula):
                bad.append(f"{addr(*ck)}: {cells[ck].formula} != {moved}")
    checks.append(("group_translation", not bad, "error",
                   f"{sum(len(g.cells) - 1 for g in groups)} members re-derived; mismatches: {bad[:5]}"))

    cyc = _find_cycle_cells(parsed, precedents)
    for ck in cyc:
        issue("cycle", "cell is part of a circular reference", ck)
    checks.append(("acyclic", not cyc, "error", f"{len(cyc)} cells in cycles"))

    unresolved = sum(1 for i in issues if i[3] in ("unresolved_ref", "unresolved_name", "unknown_sheet"))
    checks.append(("references_resolved", unresolved == 0, "warn", f"{unresolved} unresolved references"))
    no_cache = sum(1 for ck in parsed if cells[ck].cached is None)
    checks.append(("cached_values_present", no_cache == 0, "warn",
                   f"{no_cache}/{len(parsed)} formula cells have no cached value"))

    # ---- persist ---------------------------------------------------------------
    cell_rows = []
    for ck, c in sorted(cells.items()):
        t = table_of.get(ck)
        cell_rows.append((
            addr(*ck), c.sheet, c.row, c.col, f"{num_to_col(c.col)}{c.row}", c.vtype, roles[ck],
            dumps(c.value), dumps(c.cached), c.formula, keys.get(ck), group_of.get(ck),
            t.table_id if t else None, semantic.get(ck), c.number_format,
        ))
    group_rows = [
        (g.group_id, g.sheet, g.axis, addr(*g.anchor), addr(*g.cells[-1]), len(g.cells), g.key,
         dumps(parsed[g.anchor].template))
        for g in groups
    ]
    rows = {
        "meta": [("source", str(raw.path)), ("sha256", raw.sha256), ("sheetir_version", __version__),
                 ("ir_schema", str(store.IR_SCHEMA)), ("table_scorer", table_scorer), ("sheets", dumps(raw.sheets)),
                 ("load_warnings", dumps(raw.warnings)), ("user_tables", dumps(user_tables or {})),
                 ("built_at", dt.datetime.now().isoformat(timespec="seconds"))],
        "cells": cell_rows,
        "formula_groups": group_rows,
        "ranges": [(rid, r.sheet, r.r1, r.c1, r.r2, r.c2) for rid, r in ranges.items()],
        "range_cells": [(rid, addr(*ck)) for rid, members in range_members.items() for ck in members],
        "edges": edges,
        "literals": [(addr(*ck), v, fn) for ck, pf in parsed.items() for v, fn in pf.literals],
        "tables": [(t.table_id, t.sheet, t.name, t.kind, t.source, t.header_row, t.row_start, t.row_end,
                    t.c1, t.c2, t.key_col, t.total_row, t.header_top, t.confidence) for t in tables],
        "fields": [(None, t.table_id, f.col, f.header, f.name, f.unit, f.kind, f.key, f.number_format)
                   for t in tables for f in t.fields],
        "params": params,
        "relations": relation_rows,
        "names": [(n.name, n.scope, n.refers_to) for n in raw.names],
        "issues": issues,
        "ignored_issues": ignored_rows,
        "checks": [(n, int(p), s, d) for n, p, s, d in checks],
    }
    store.write(db_path, rows)

    by_kind: dict[str, int] = defaultdict(int)
    for i in issues:
        by_kind[i[3]] += 1
    counts = {
        "sheets": len(raw.sheets), "cells": len(cells), "formula_cells": len(parsed),
        "formula_groups": len(groups), "tables": len(tables), "params": len(params),
        "edges": len(edges), "relations": len(relations), "issues": len(issues),
    }
    report = None
    if verify:
        from . import verify as independent
        try:
            report = independent.verify(book, db_path)
        except Exception as exc:  # noqa: BLE001 - a failed check must not lose the parse result
            report = {"status": "unverified", "error": f"{type(exc).__name__}: {exc}", "checks": {}, "coverage": None}
        independent.record(db_path, report)
    return BuildResult(Path(db_path), counts, checks, dict(by_kind), report)


def _find_cycle_cells(parsed, precedents) -> list[CellKey]:
    """Kahn's algorithm over formula cells; whatever cannot be ordered is in (or behind) a cycle."""
    deps = {ck: {p for p in precedents.get(ck, ()) if p in parsed} for ck in parsed}
    dependents: dict[CellKey, list[CellKey]] = defaultdict(list)
    for ck, ps in deps.items():
        for p in ps:
            dependents[p].append(ck)
    indeg = {ck: len(ps) for ck, ps in deps.items()}
    ready = [ck for ck, n in indeg.items() if n == 0]
    while ready:
        ck = ready.pop()
        for d in dependents[ck]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    return sorted(ck for ck, n in indeg.items() if n > 0)
