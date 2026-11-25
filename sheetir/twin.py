"""The verified Python twin: translate a workbook's formulas (sheetir.transpile), then check the translation.

    V1 (L1 parity)       the twin, given the workbook's own inputs, must reproduce the values Excel saved in the file.
                         Two passes: "isolated" computes each formula cell from Excel's saved values of the cells it
                         reads, so a mismatch points at that cell's group; "end to end" computes the whole workbook
                         from its constants alone, which is what answers and scenarios will use.
    V2 (L2 differential) on random input scenarios, the twin must agree with an independent engine (pycel). Inputs
                         are changed within their data-validation rule when there is one, otherwise around the observed
                         value; lookup keys are drawn from the keys their lookup table actually has.

A group that is not translated keeps Excel's saved values ("keep baseline"); V2 skips every cell that depends on such a
group, since the twin cannot recompute it under new inputs. Results go into the SheetIR database (tables twin_groups,
twin_checks, twin_mismatches and meta key "twin"), so the web UI and SQL can show which calculations are verified.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from . import xl
from .refs import addr, num_to_col, parse_ref
from .transpile import Twin, load_twin, module_source

TABLES = """
CREATE TABLE IF NOT EXISTS twin_groups(
    group_id INT PRIMARY KEY, status TEXT, reason TEXT, functions TEXT, python TEXT,
    v1_checked INT, v1_passed INT, v2_checked INT, v2_passed INT);
CREATE TABLE IF NOT EXISTS twin_mismatches(
    level TEXT, mode TEXT, scenario INT, cell_id TEXT, group_id INT, expected TEXT, got TEXT, inputs TEXT);
"""

MAX_MISMATCHES = 200  # per level: enough to diagnose, small enough to keep in the database
MAX_CHANGES = 50  # inputs changed per V2 scenario (30% of them, up to this)


def _show(v) -> str:
    if isinstance(v, xl.XLError):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- V1 ------------------------------------------------

def run_v1(twin: Twin) -> dict:
    """Compare the twin with the values Excel saved. Only translated cells with a saved value are compared."""
    targets = [ck for ck in twin.order if twin.groups[twin.group_of[ck]].status == "translated"]
    with_baseline = [ck for ck in targets if twin.baseline.get(ck) is not None]
    out = {"cells": len(targets), "with_baseline": len(with_baseline), "groups": {}, "mismatches": []}
    if not with_baseline:
        out.update(status="no_baseline",
                   summary="the file has no values saved by Excel (it was not last saved by Excel), so there is "
                           "nothing to compare with; V2 still checks the twin against an independent engine")
        return out
    t0 = time.perf_counter()
    iso = twin.model(frozen=True).compute_all(with_baseline)
    e2e = twin.model().compute_all(twin.order)
    per: dict[int, dict] = {}
    failed_iso = failed_e2e = 0
    for ck in with_baseline:
        gid = twin.group_of[ck]
        g = per.setdefault(gid, {"checked": 0, "passed": 0, "e2e_passed": 0})
        exp = twin.baseline[ck]
        ok_iso, ok_e2e = xl.same(exp, iso[ck]), xl.same(exp, e2e[ck])
        g["checked"] += 1
        g["passed"] += ok_iso
        g["e2e_passed"] += ok_e2e
        failed_iso += not ok_iso
        failed_e2e += not ok_e2e
        for mode, ok, got in (("isolated", ok_iso, iso[ck]), ("end_to_end", ok_e2e, e2e[ck])):
            if not ok and len(out["mismatches"]) < MAX_MISMATCHES:
                out["mismatches"].append({"mode": mode, "cell": addr(*ck), "group": gid,
                                          "expected": _show(exp), "got": _show(got)})
    n = len(with_baseline)
    out.update(
        groups=per, seconds=round(time.perf_counter() - t0, 3),
        isolated={"checked": n, "passed": n - failed_iso},
        end_to_end={"checked": n, "passed": n - failed_e2e},
        status="passed" if not failed_iso and not failed_e2e else "failed",
        summary=f"{n - failed_iso}/{n} cells match Excel cell by cell, {n - failed_e2e}/{n} when the whole "
                "workbook is computed from its inputs",
    )
    return out


# ---------------------------------------------------------------- V2: the independent engine -----------------------

def _plain(ck) -> str:
    """A cell address as pycel keys its cell map: Sheet!A1 with the sheet name unquoted."""
    return f"{ck[0]}!{num_to_col(ck[2])}{ck[1]}"


def _pycel():
    """pycel's ExcelCompiler, with its defined-names reader fixed for openpyxl >= 3.1."""
    try:
        from pycel import ExcelCompiler
        from pycel.excelwrapper import ExcelOpxWrapper
    except ImportError as exc:
        raise RuntimeError("pycel is not installed (uv sync, or pip install pycel)") from exc

    if not getattr(ExcelOpxWrapper, "_sheetir_patched", False):
        def defined_names(self):
            if self.workbook is not None and self._defined_names is None:
                self._defined_names = {}
                # pycel has no notion of a name's scope: a sheet-level name is given to it only when no other sheet
                # defines the same name (otherwise formulas using it are left to fail, and V2 skips them)
                scoped = [(n, d) for ws in self.workbook.worksheets for n, d in ws.defined_names.items()]
                counts: dict = {}
                for n, _ in scoped:
                    counts[n.upper()] = counts.get(n.upper(), 0) + 1
                scoped = [(n, d) for n, d in scoped if counts[n.upper()] == 1]
                for name, d in list(self.workbook.defined_names.items()) + scoped:
                    try:
                        dests = [(alias, ws) for ws, alias in d.destinations if ws in self.workbook]
                    except Exception:  # noqa: BLE001 - a constant or formula name has no destinations
                        dests = []
                    if dests:
                        self._defined_names[str(name)] = dests
            return self._defined_names

        ExcelOpxWrapper.defined_names = property(defined_names)
        ExcelOpxWrapper._sheetir_patched = True
    return ExcelCompiler


_NUMBER_IN_TEXT = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")


def _agree(theirs, ours, functions=()) -> bool | str:
    """V2's equality: True, False, or the name of a known difference between pycel and Excel where the twin does
    what Excel does (V1 checks that against Excel's own values):

        number_text        pycel turns numbers into text with Python's 17 digits (=1/3&""), Excel with 15
        cancellation_zero  Excel (and the twin) return exactly 0 when + or - cancels to rounding noise; pycel keeps
                           the noise (2e-9 instead of 0)
        rounding_15_digits ROUND, ROUNDUP and ROUNDDOWN in Excel work on the value at 15 significant digits
                           (=ROUND(1.635,2) is 1.64); pycel rounds the binary value (1.63)
    """
    if xl.same(theirs, ours):
        return True
    if isinstance(theirs, str) and isinstance(ours, str) and not isinstance(theirs, xl.XLError):
        norm = lambda t: _NUMBER_IN_TEXT.sub(lambda m: f"{float(m.group()):.15g}", t)  # noqa: E731
        return "number_text" if norm(theirs) == norm(ours) else False
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if num(theirs) and num(ours):
        if ours == 0 and abs(theirs) < 1e-7:
            return "cancellation_zero"
        diff = abs(theirs - ours)
        if {"ROUND", "ROUNDUP", "ROUNDDOWN"} & set(functions) and any(
                math.isclose(diff, 10.0 ** -k, rel_tol=1e-6) for k in range(0, 10)):
            return "rounding_15_digits"
    return False


def _from_pycel(v):
    try:
        import numpy as np

        if isinstance(v, np.generic):
            v = v.item()
    except ImportError:
        pass
    if isinstance(v, str) and v in xl.ERRORS:
        return xl.XLError(v)
    if isinstance(v, (tuple, list)):  # a range-valued result
        return xl.VALUE
    if v == "":
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


# ---------------------------------------------------------------- V2: scenarios ------------------------------------

def _domain(rule: dict | None, sheet: str, twin: Twin):
    """Allowed values from a data-validation rule: ("list", [values]) or ("range", lo, hi, integer) or None."""
    if not rule:
        return None
    kind, op = rule.get("type"), rule.get("operator") or "between"
    f1, f2 = rule.get("formula1"), rule.get("formula2")
    if kind == "list" and f1:
        f1 = f1.strip()
        if f1.startswith('"'):
            return ("list", [_typed(x.strip()) for x in f1.strip('"').split(",") if x.strip()])
        ref = parse_ref(f1.lstrip("="))
        if ref is not None and ref.r1 is not None:
            s = twin_sheet(twin, ref.sheet or sheet)
            vals = [twin.constants.get((s, r, c)) for r in range(ref.r1, ref.r2 + 1) for c in range(ref.c1, ref.c2 + 1)]
            vals = [v for v in vals if v is not None]
            return ("list", vals) if vals else None
        return None
    if kind in ("whole", "decimal"):
        try:
            a = float(f1) if f1 is not None else None
            b = float(f2) if f2 is not None else None
        except ValueError:
            return None
        lo, hi = {"between": (a, b), "greaterThan": (a, None), "greaterThanOrEqual": (a, None),
                  "lessThan": (None, a), "lessThanOrEqual": (None, a)}.get(op, (None, None))
        if lo is None and hi is None:
            return None
        return ("range", lo, hi, kind == "whole")
    return None


def twin_sheet(twin: Twin, name: str) -> str:
    return next((s for s in twin.sheets if s.lower() == name.lower()), name)


def _typed(text: str):
    try:
        return float(text)
    except ValueError:
        return text


def _inputs(twin: Twin, con: sqlite3.Connection) -> dict:
    """Cells a scenario may change, each with how to draw a new value."""
    domains = {}
    for cell_id, domain in con.execute("SELECT cell_id, domain FROM params WHERE domain IS NOT NULL"):
        ref = parse_ref(cell_id)
        if ref is not None:
            domains[(twin_sheet(twin, ref.sheet), ref.r1, ref.c1)] = _domain(json.loads(domain), ref.sheet, twin)
    keys_for: dict = {}
    for value_cell, key_range in con.execute(
            "SELECT value_cell, key_range FROM relations WHERE value_cell IS NOT NULL AND key_range IS NOT NULL"):
        v, k = parse_ref(value_cell), parse_ref(key_range)
        if v is None or k is None or k.r1 is None or k.c1 is None:
            continue
        s = twin_sheet(twin, k.sheet)
        keys = sorted({x for r in range(k.r1, k.r2 + 1) for c in range(k.c1, k.c2 + 1)
                       if isinstance(x := twin.constants.get((s, r, c)), str)})
        if keys:
            keys_for[(twin_sheet(twin, v.sheet), v.r1, v.c1)] = keys
    out = {}
    for ck, role in twin.roles.items():
        if role != "input" or ck not in twin.constants:
            continue
        v = twin.constants[ck]
        if ck in domains and domains[ck] is not None:
            out[ck] = domains[ck]
        elif isinstance(v, str) and ck in keys_for:
            out[ck] = ("list", keys_for[ck])
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[ck] = ("around", float(v))
    return out


def _draw(rng: random.Random, how):
    if how[0] == "list":
        return rng.choice(how[1])
    if how[0] == "range":
        _, lo, hi, whole = how
        lo = lo if lo is not None else (hi - abs(hi) - 10 if hi is not None else 0)
        hi = hi if hi is not None else lo + abs(lo) + 10
        x = rng.uniform(lo, hi)
        return float(round(x)) if whole else x
    v = how[1]
    if v == 0:
        return 0.0 if rng.random() < 0.5 else float(rng.randint(1, 10))
    x = v * rng.uniform(0.5, 1.5)
    return float(round(x)) if float(v).is_integer() and abs(v) >= 1 else x


def _engine_limits(twin: Twin) -> set:
    """Formula cells pycel is known to get wrong, whatever the twin does: those reading a sheet whose name is a
    number ('9'!J2 resolves to another sheet after a value changes), and those using a defined name that several
    sheets define (pycel ignores a name's scope)."""
    digit = [s for s in twin.sheets if s.strip().isdigit()]
    names: dict = {}
    for name, scope, _ in twin.names:
        if scope is not None:
            names[name.upper()] = names.get(name.upper(), 0) + 1
    ambiguous = {n for n, k in names.items() if k > 1}
    bad = set()
    for ck, gid in twin.group_of.items():
        g = twin.groups[gid]
        if g.expr is None:
            continue
        if any(f"m.c({s!r}" in g.expr or f"m.r({s!r}" in g.expr for s in digit) or (
                ambiguous & set(g.names)):
            bad.add(ck)
    return bad


def _tainted(twin: Twin, extra: set = frozenset()) -> set:
    """Formula cells that read, directly or not, a cell whose group is not translated (or one in `extra`)."""
    bad = {ck for ck, gid in twin.group_of.items() if twin.groups[gid].status != "translated"} | set(extra)
    dependents: dict = {}
    for ck, ps in twin.precedents.items():
        for p in ps:
            dependents.setdefault(p, []).append(ck)
    stack = list(bad)
    while stack:
        for d in dependents.get(stack.pop(), ()):
            if d not in bad:
                bad.add(d)
                stack.append(d)
    return bad


def _choose(twin: Twin, tainted: set, rng: random.Random, max_cells: int) -> list:
    """Formula cells V2 compares: every output, then cells spread across groups (first, last, random members)."""
    candidates = [ck for ck in twin.order if ck not in tainted]
    chosen = [ck for ck in candidates if twin.roles.get(ck) == "output"][:max_cells]
    taken = set(chosen)
    by_group: dict[int, list] = {}
    for ck in candidates:
        if ck not in taken:
            by_group.setdefault(twin.group_of[ck], []).append(ck)
    budget = max(max_cells - len(chosen), 0)
    k = max(1, budget // max(len(by_group), 1)) if by_group else 0
    for gid, cks in sorted(by_group.items()):
        if len(chosen) >= max_cells:
            break
        pick = cks if len(cks) <= k else [cks[0], cks[-1]] + rng.sample(cks[1:-1], max(k - 2, 0))
        chosen.extend(pick[:max(k, 1)])
    return chosen[:max_cells]


def run_v2(twin: Twin, db: str | Path, xlsx: str | Path, scenarios: int = 10, seed: int = 0,
           max_cells: int = 400, time_limit: float = 120.0) -> dict:
    """Compare the twin with pycel on the file's own inputs (scenario 0) and on `scenarios` random input sets.
    Stops starting new comparisons after `time_limit` seconds and says how far it got."""
    import traceback

    # pycel formats a full traceback for every error it meets, at every level of a long chain of formulas; on a
    # workbook where an error runs down thousands of cells that takes gigabytes. Only the error values matter here.
    format_exc = traceback.format_exc
    traceback.format_exc = lambda *a, **k: ""
    try:
        return _run_v2(twin, db, xlsx, scenarios, seed, max_cells, time_limit)
    finally:
        traceback.format_exc = format_exc


def _run_v2(twin, db, xlsx, scenarios, seed, max_cells, time_limit) -> dict:
    ExcelCompiler = _pycel()
    import logging

    logging.getLogger("pycel").setLevel(logging.CRITICAL)  # it logs every formula it cannot evaluate
    t0 = time.perf_counter()
    con = sqlite3.connect(db)
    try:
        inputs = _inputs(twin, con)
    finally:
        con.close()
    limits = _engine_limits(twin)
    tainted = _tainted(twin, limits)
    for ck in list(inputs):
        if ck[0].strip().isdigit():
            inputs.pop(ck)
    rng = random.Random(seed)
    chosen = _choose(twin, tainted, rng, max_cells)
    untranslated = _tainted(twin)
    out = {"cells": len(chosen), "skipped_untranslated_inputs": len(untranslated & set(twin.group_of)),
           "skipped_engine_limits": len((tainted - untranslated) & set(twin.group_of)),
           "scenarios": scenarios, "seed": seed,
           "groups": {}, "mismatches": [], "known_differences": {},
           "reference_errors": 0}
    if not chosen:
        why = "no translated formula cells to compare"
        if out["skipped_engine_limits"]:
            why = (f"the {out['skipped_engine_limits']} translated cells all read sheets or names pycel cannot "
                   "handle (sheets named with a number, names defined on several sheets)")
        out.update(status="nothing_to_check", summary=why)
        return out
    engine = ExcelCompiler(filename=str(xlsx))
    from pycel.excelcompiler import _CellRange
    for ck in chosen:  # builds pycel's cell map for everything the chosen cells read
        try:
            engine.evaluate(addr(*ck))
        except Exception:  # noqa: BLE001 - counted when compared
            pass
    cell_map = getattr(engine, "cell_map", None)
    if cell_map is not None:  # an input none of the chosen cells reads cannot change them: leave it alone
        for ck in list(inputs):
            if _plain(ck) not in cell_map:
                inputs.pop(ck)

    def dirty():
        # pycel's set_value does not always reach cells that read the changed one through a range (SUM(B6:B15)
        # keeps its old total), so every formula and range is marked for recalculation instead
        for cell in engine.cell_map.values():
            if isinstance(cell, _CellRange) or getattr(cell, "formula", None):
                cell.value = None

    original = {ck: twin.constants[ck] for ck in inputs}
    per: dict[int, dict] = {}
    checked = failed = 0
    done = 0
    for s in range(scenarios + 1):
        if time.perf_counter() - t0 > time_limit:
            out["stopped"] = f"time limit of {time_limit:g}s reached after {done - 1} random scenarios"
            break
        done += 1
        changes = {}
        if s > 0 and inputs:
            cells = list(inputs)
            n = min(max(1, round(len(cells) * 0.3)), MAX_CHANGES)
            for ck in rng.sample(cells, min(n, len(cells))):
                changes[ck] = _draw(rng, inputs[ck])
        try:
            for ck, v in changes.items():
                engine.set_value(addr(*ck), v)
            if changes:
                dirty()
            m = twin.model(changes=changes)
            for ck in chosen:
                if time.perf_counter() - t0 > time_limit:
                    break
                ours = m.c(*ck)
                try:
                    theirs = _from_pycel(engine.evaluate(addr(*ck)))
                except Exception:  # noqa: BLE001 - the reference engine cannot evaluate this cell
                    out["reference_errors"] += 1
                    continue
                if isinstance(theirs, xl.XLError) and str(theirs) == "#NAME?" and not isinstance(ours, xl.XLError):
                    out["reference_errors"] += 1  # pycel lacks a function the twin has
                    continue
                gid = twin.group_of[ck]
                g = per.setdefault(gid, {"checked": 0, "passed": 0})
                ok = _agree(theirs, ours, twin.groups[gid].functions)
                if isinstance(ok, str):
                    out["known_differences"][ok] = out["known_differences"].get(ok, 0) + 1
                    ok = True
                g["checked"] += 1
                g["passed"] += ok
                checked += 1
                failed += not ok
                if not ok and len(out["mismatches"]) < MAX_MISMATCHES:
                    out["mismatches"].append({
                        "mode": "scenario", "scenario": s, "cell": addr(*ck), "group": gid,
                        "expected": _show(theirs), "got": _show(ours),
                        "inputs": {addr(*c): v for c, v in changes.items()}})
        finally:
            for ck in changes:
                engine.set_value(addr(*ck), original[ck])
            if changes:
                dirty()
    out.update(groups=per, checked=checked, passed=checked - failed, inputs=len(inputs),
               seconds=round(time.perf_counter() - t0, 2))
    if checked == 0:
        out.update(status="nothing_to_check", summary="the reference engine could not evaluate the chosen cells")
    else:
        ran = max(done - 1, 0)
        out.update(status="passed" if failed == 0 else "failed", scenarios_run=ran,
                   summary=f"{checked - failed}/{checked} comparisons agree with pycel over {ran} random "
                           f"scenarios plus the file's own inputs" + (f" ({out['stopped']})" if "stopped" in out else ""))
    return out


# ---------------------------------------------------------------- run and record -----------------------------------

def _group_status(g, v1: dict, v2: dict | None) -> str:
    if g.status != "translated":
        return "untranslated"
    a = (v1.get("groups") or {}).get(g.group_id)
    b = ((v2 or {}).get("groups") or {}).get(g.group_id)
    v1_ok = a is not None and a["checked"] and a["passed"] == a["checked"] and a["e2e_passed"] == a["checked"]
    v2_ok = b is not None and b["checked"] and b["passed"] == b["checked"]
    if (a and (a["passed"] < a["checked"] or a["e2e_passed"] < a["checked"])) or (b and b["passed"] < b["checked"]):
        return "failed"
    if v1_ok and v2_ok:
        return "verified"
    if v1_ok:
        return "v1_passed"
    if v2_ok:
        return "v2_passed"
    return "unchecked"


def check(db: str | Path, xlsx: str | Path | None = None, v2: bool = True, scenarios: int = 10, seed: int = 0,
          max_cells: int = 400, time_limit: float = 120.0) -> dict:
    """Translate, run V1 (and V2 when `xlsx` is the workbook or can be found), and store the results in `db`."""
    t0 = time.perf_counter()
    twin = load_twin(db)
    twin.compile()
    report: dict = {"at": dt.datetime.now().isoformat(timespec="seconds"), "coverage": twin.coverage()}
    report["v1"] = run_v1(twin)
    report["v2"] = None
    if v2:
        book = _workbook_for(db, xlsx)
        if book is None:
            report["v2"] = {"status": "not_run", "summary": "the original workbook is not available"}
        else:
            with tempfile.TemporaryDirectory() as tmp:
                from .convert import as_xlsx

                path, _ = as_xlsx(Path(book), Path(tmp))
                try:
                    report["v2"] = run_v2(twin, db, path, scenarios, seed, max_cells, time_limit)
                except Exception as exc:  # noqa: BLE001 - the reference engine failing must not lose V1
                    report["v2"] = {"status": "error", "summary": f"{type(exc).__name__}: {exc}"}
    statuses = {gid: _group_status(g, report["v1"], report["v2"]) for gid, g in twin.groups.items()}
    counts: dict[str, int] = {}
    cells: dict[str, int] = {}
    for gid, st in statuses.items():
        counts[st] = counts.get(st, 0) + 1
        cells[st] = cells.get(st, 0) + twin.groups[gid].n_cells
    report["groups"] = counts
    report["cells"] = cells
    report["seconds"] = round(time.perf_counter() - t0, 2)
    report["status"] = _overall(report)
    _record(db, twin, report, statuses)
    return report


def _overall(report: dict) -> str:
    v1, v2 = report["v1"]["status"], (report["v2"] or {}).get("status")
    if "failed" in (v1, v2):
        return "failed"
    if report["coverage"]["groups_translated"] == 0:
        return "untranslated"
    if v1 == "passed" and v2 == "passed":
        return "verified" if not report["coverage"]["untranslated"] else "partly_verified"
    if v1 == "passed" or v2 == "passed":
        return "partly_verified"
    return "unchecked"


def _workbook_for(db: str | Path, xlsx: str | Path | None) -> Path | None:
    if xlsx is not None:
        return Path(xlsx)
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()
    finally:
        con.close()
    if row and Path(row[0]).exists():
        return Path(row[0])
    folder = Path(db).parent  # the web UI keeps the upload next to ir.db
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv"):
            return p
    return None


def _record(db: str | Path, twin: Twin, report: dict, statuses: dict) -> None:
    con = sqlite3.connect(db)
    try:
        con.executescript(TABLES)
        con.execute("DELETE FROM twin_groups")
        con.execute("DELETE FROM twin_mismatches")
        v1g = report["v1"].get("groups") or {}
        v2g = (report["v2"] or {}).get("groups") or {}
        con.executemany("INSERT INTO twin_groups VALUES (?,?,?,?,?,?,?,?,?)", [
            (gid, statuses[gid], g.reason if g.detail is None else f"{g.reason} ({g.detail})",
             json.dumps(g.functions), g.source,
             (v1g.get(gid) or {}).get("checked"), (v1g.get(gid) or {}).get("passed"),
             (v2g.get(gid) or {}).get("checked"), (v2g.get(gid) or {}).get("passed"))
            for gid, g in twin.groups.items()])
        rows = []
        for level in ("v1", "v2"):
            for mm in (report.get(level) or {}).get("mismatches") or []:
                rows.append((level, mm["mode"], mm.get("scenario"), mm["cell"], mm["group"], mm["expected"],
                             mm["got"], json.dumps(mm.get("inputs"), ensure_ascii=False) if mm.get("inputs") else None))
        con.executemany("INSERT INTO twin_mismatches VALUES (?,?,?,?,?,?,?,?)", rows)
        summary = {k: v for k, v in report.items() if k not in ("v1", "v2")}
        for level in ("v1", "v2"):
            r = report.get(level)
            summary[level] = None if r is None else {k: v for k, v in r.items() if k not in ("groups", "mismatches")}
        con.execute("INSERT OR REPLACE INTO meta VALUES ('twin', ?)", (json.dumps(summary, ensure_ascii=False),))
        con.commit()
    finally:
        con.close()


def stored(db: str | Path) -> dict | None:
    """The last check's summary, groups and mismatches, or None if the twin was never checked."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'twin'").fetchone()
        if row is None:
            return None
        out = json.loads(row[0])
        out["group_results"] = [dict(r) for r in con.execute(
            "SELECT t.group_id, t.status, t.reason, t.functions, t.v1_checked, t.v1_passed, t.v2_checked, "
            "t.v2_passed, f.anchor, f.last, f.n_cells, f.r1c1, c.semantic "
            "FROM twin_groups t JOIN formula_groups f USING (group_id) "
            "LEFT JOIN cells c ON c.cell_id = f.anchor ORDER BY t.group_id")]
        for g in out["group_results"]:
            g["functions"] = json.loads(g["functions"] or "[]")
        out["mismatches"] = [dict(r) for r in con.execute("SELECT * FROM twin_mismatches")]
        return out
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def export_module(db: str | Path) -> str:
    twin = load_twin(db)
    report = stored(db)
    return module_source(twin, report)
