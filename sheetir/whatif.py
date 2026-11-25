"""What-if analysis on the Python twin (sheetir.transpile): scenarios, sensitivity and goal seek.

    eng = Engine.cached(db)                         # the twin of a SheetIR database, compiled once per build
    eng.run({"Inputs!B1": 3})                       # every formula cell whose value changes, and the key outputs
    eng.sensitivity("Calc!D5", step=0.1)            # each input moved ±10 %: how far the target swings
    eng.goal_seek("Calc!D5", 100, "Inputs!B1")      # the value of Inputs!B1 that makes Calc!D5 = 100

The workbook itself is never touched: a scenario only changes the twin's copy of its constants.

Every computed cell carries a status that says how far its value can be trusted:

    verified           its formula group passed V1 (against Excel) and V2 (against pycel)
    v1_passed / v2_passed   one of the two checks passed, the other was not run
    unchecked          the twin was not checked yet (POST /api/workbooks/{id}/twin)
    failed             its group failed V1 or V2
    depends_on_failed  it reads, directly or not, a cell of a failed group
    not_recomputed     it is, or reads, a formula the twin could not translate: that formula keeps the value Excel
                       saved, so this cell does not follow the changes (or follows them only in part)

The baseline of a scenario is the twin's own result with no change, so a difference is caused by the change alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

from . import xl
from .refs import addr, parse_ref
from .transpile import Twin, load_twin

MAX_CHANGES = 2000        # cells one scenario may change
MAX_LISTED = 200          # changed cells listed in a result (all are counted)
MAX_OUTPUTS = 50          # key outputs listed in a result
MAX_SENSITIVITY_INPUTS = 30
GOAL_SEEK_ITERATIONS = 100
TRUSTED = ("verified", "v1_passed", "v2_passed")


class WhatIfError(Exception):
    """A request the engine cannot answer; the message says why, for the user or the model to act on."""


def typed(v):
    """A value as the user typed it (JSON value or text as in Excel) to what the twin computes with."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            raise WhatIfError("a value must be a finite number")
        return float(v)
    if not isinstance(v, str):
        raise WhatIfError(f"unsupported value {v!r}: use a number, text, true / false, or null to clear")
    t = v.strip()
    if t == "":
        return None
    if t.startswith("=") and len(t) > 1:
        raise WhatIfError("a scenario changes values, not formulas: propose a correction to change a formula")
    if t.upper() in ("TRUE", "FALSE"):
        return t.upper() == "TRUE"
    num = t.replace(",", "") if _GROUPED.match(t) else t
    pct = num.endswith("%")
    try:
        x = float(num[:-1] if pct else num)
    except ValueError:
        return v
    if not math.isfinite(x):
        return v
    return x / 100 if pct else x


_GROUPED = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?%?$")


def as_typed(v) -> str:
    """A twin value back to what a user would type in Excel (for writing a scenario into a copy of the file)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    return str(v)


def show(v):
    """A twin value for JSON: numbers as numbers, errors as their text."""
    if isinstance(v, float):
        return float(f"{v:.15g}") if math.isfinite(v) else None  # as Excel shows it: 15 significant digits
    if isinstance(v, xl.XLError):
        return str(v)
    return v


def _number(v) -> float | None:
    return v if isinstance(v, float) and not isinstance(v, bool) and math.isfinite(v) else None


class Engine:
    """The twin of one SheetIR database, with what it needs to answer what-if questions."""

    _cache: "OrderedDict[str, tuple[float, Engine]]" = OrderedDict()
    _cache_lock = threading.Lock()
    CACHE_SIZE = 4

    def __init__(self, db: str | Path):
        self.db = Path(db)
        self.twin: Twin = load_twin(self.db)
        self.twin.compile()
        self._lock = threading.Lock()
        self._base: dict | None = None
        self._edges: dict | None = None
        con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        try:
            self.labels = {}
            for cell_id, label, name in con.execute("SELECT cell_id, label, name FROM params"):
                ref = parse_ref(cell_id)
                if ref is not None:
                    self.labels[(self._sheet(ref.sheet), ref.r1, ref.c1)] = label or name
            self.check = self._check(con)
        finally:
            con.close()
        self.status = self._statuses()

    @classmethod
    def cached(cls, db: str | Path) -> "Engine":
        """One engine per database, rebuilt when the database changes (a re-parse or a new twin check)."""
        key = str(Path(db).resolve())
        mtime = Path(db).stat().st_mtime
        with cls._cache_lock:
            hit = cls._cache.get(key)
            if hit is not None and hit[0] == mtime:
                cls._cache.move_to_end(key)
                return hit[1]
        eng = cls(db)
        with cls._cache_lock:
            cls._cache[key] = (mtime, eng)
            cls._cache.move_to_end(key)
            while len(cls._cache) > cls.CACHE_SIZE:
                cls._cache.popitem(last=False)
        return eng

    # ---- cells ------------------------------------------------------------------------------------------------------

    def _sheet(self, name: str | None) -> str | None:
        if name is None:
            return None
        return next((s for s in self.twin.sheets if s.lower() == name.lower()), None)

    def key(self, cell_id: str) -> tuple:
        """(sheet, row, col) of a cell id such as Calc!D5 or 'My Sheet'!A1."""
        ref = parse_ref(str(cell_id or ""))
        if ref is None or ref.sheet is None or ref.r1 is None or ref.c1 is None or (ref.r1, ref.c1) != (ref.r2, ref.c2):
            raise WhatIfError(f"{cell_id!r} is not a single cell with its sheet, such as Calc!D5")
        sheet = self._sheet(ref.sheet)
        if sheet is None:
            raise WhatIfError(f"{cell_id}: no sheet {ref.sheet!r} (sheets: {', '.join(self.twin.sheets)})")
        return (sheet, ref.r1, ref.c1)

    @staticmethod
    def cell_id(ck: tuple) -> str:
        return addr(*ck)

    def label(self, ck: tuple) -> str | None:
        return self.labels.get(ck) or self.twin.semantic.get(ck)

    def is_formula(self, ck: tuple) -> bool:
        return ck in self.twin.group_of

    def value(self, ck: tuple):
        """The cell's value with no change: its constant, or the twin's result."""
        if ck in self.twin.constants:
            return self.twin.constants[ck]
        if ck in self.twin.group_of:
            return self.base().get(ck)
        return None

    # ---- trust ------------------------------------------------------------------------------------------------------

    def _check(self, con) -> dict:
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'twin'").fetchone()
            groups = dict(con.execute("SELECT group_id, status FROM twin_groups").fetchall())
        except sqlite3.OperationalError:
            return {"status": "not_run", "at": None, "groups": {}}
        if row is None:
            return {"status": "not_run", "at": None, "groups": {}}
        summary = json.loads(row[0])
        return {"status": summary.get("status"), "at": summary.get("at"), "groups": groups,
                "coverage": summary.get("coverage")}

    def _closure(self, start: set) -> set:
        dependents: dict = {}
        for ck, ps in self.twin.precedents.items():
            for p in ps:
                dependents.setdefault(p, []).append(ck)
        out, stack = set(start), list(start)
        while stack:
            for d in dependents.get(stack.pop(), ()):
                if d not in out:
                    out.add(d)
                    stack.append(d)
        return out

    def _statuses(self) -> dict:
        tw = self.twin
        groups = self.check["groups"]
        untranslated = {ck for ck, gid in tw.group_of.items() if tw.groups[gid].status != "translated"}
        failed = {ck for ck, gid in tw.group_of.items() if groups.get(gid) == "failed"}
        stale = self._closure(untranslated)
        suspect = self._closure(failed)
        out = {}
        for ck, gid in tw.group_of.items():
            if ck in stale:
                out[ck] = "not_recomputed"
            elif ck in failed:
                out[ck] = "failed"
            elif ck in suspect:
                out[ck] = "depends_on_failed"
            else:
                out[ck] = groups.get(gid) or "unchecked"
        return out

    def trust(self) -> dict:
        """How far results can be trusted, for the record of a scenario."""
        counts: dict[str, int] = {}
        for st in self.status.values():
            counts[st] = counts.get(st, 0) + 1
        return {"check": self.check["status"], "checked_at": self.check["at"], "cells": counts}

    # ---- computing --------------------------------------------------------------------------------------------------

    def base(self) -> dict:
        """Every formula cell's value with no change (computed once)."""
        with self._lock:
            if self._base is None:
                self._base = self.twin.model().compute_all(self.twin.order)
            return self._base

    def _changes(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise WhatIfError("changes must be an object: {cell id: new value}")
        if len(changes) > MAX_CHANGES:
            raise WhatIfError(f"at most {MAX_CHANGES} cells per scenario")
        out = {}
        for cell, v in changes.items():
            ck = self.key(cell)
            if self.is_formula(ck):
                raise WhatIfError(f"{self.cell_id(ck)} holds a formula: change the cells it reads, or propose a "
                                  "correction to replace the formula")
            out[ck] = typed(v)
        return out

    def values(self, changes: dict) -> tuple[dict, dict]:
        """(every formula cell's value, the parsed changes) under `changes`."""
        ch = self._changes(changes)
        if not ch:
            return self.base(), ch
        return self.twin.model(changes=ch).compute_all(self.twin.order), ch

    def compare(self, a: dict, b: dict) -> dict:
        """Two scenarios of this workbook side by side: the inputs they set differently, and every formula cell whose
        value differs between them (outputs first)."""
        va, cha = self.values(a)
        vb, chb = self.values(b)
        inputs = []
        for ck in sorted(set(cha) | set(chb)):
            x = cha.get(ck, self.twin.constants.get(ck))
            y = chb.get(ck, self.twin.constants.get(ck))
            if not xl.same(x, y):
                inputs.append({"cell": self.cell_id(ck), "label": self.label(ck), "a": show(x), "b": show(y)})
        diff = [ck for ck in self.twin.order if not xl.same(va.get(ck), vb.get(ck))]
        diff.sort(key=lambda ck: self.twin.roles.get(ck) != "output")
        cells = []
        for ck in diff[:MAX_LISTED]:
            x, y = va.get(ck), vb.get(ck)
            d = show(y - x) if _number(x) is not None and _number(y) is not None else None
            cells.append({"cell": self.cell_id(ck), "label": self.label(ck), "a": show(x), "b": show(y), "delta": d,
                          "status": self.status.get(ck)})
        return {"inputs": inputs, "cells": cells, "n_cells": len(diff), "truncated": len(diff) > MAX_LISTED}

    def catalog(self, query: str | None = None, limit: int = 200) -> dict:
        """Cells a what-if screen offers. inputs: constants a scenario can change (labelled ones first, then those
        SheetIR found to be inputs; with `query`, any constant whose id or label contains it). targets: formula cells
        to analyze (the key outputs first; with `query`, any formula cell whose id or label contains it)."""
        q = (query or "").strip().lower()
        tw = self.twin

        def hit(ck) -> bool:
            return q in self.cell_id(ck).lower() or q in str(self.label(ck) or "").lower()

        if q:
            inputs = [ck for ck in tw.constants if hit(ck)]
            inputs.sort(key=lambda ck: (ck not in self.labels, tw.roles.get(ck) != "input", ck))
            targets = [ck for ck in tw.order if hit(ck)]
            targets.sort(key=lambda ck: tw.roles.get(ck) != "output")
        else:
            inputs = [ck for ck in self.labels if ck in tw.constants]
            inputs += [ck for ck, r in tw.roles.items() if r == "input" and ck in tw.constants and ck not in self.labels]
            targets = self.outputs()
        row = lambda ck, st: {"cell": self.cell_id(ck), "label": self.label(ck), "value": show(self.value(ck)),  # noqa: E731
                              "status": st, "number": _number(self.value(ck)) is not None}
        return {"inputs": [row(ck, "input") for ck in inputs[:limit]], "n_inputs": len(inputs),
                "targets": [row(ck, self.status.get(ck)) for ck in targets[:limit]], "n_targets": len(targets),
                "trust": self.trust()}

    def outputs(self) -> list:
        """The workbook's key outputs: cells SheetIR found to be final results (role output)."""
        return [ck for ck in self.twin.order if self.twin.roles.get(ck) == "output"]

    def _entry(self, ck, base, new) -> dict:
        d = None
        if _number(base) is not None and _number(new) is not None:
            d = show(new - base)
        return {"cell": self.cell_id(ck), "label": self.label(ck), "base": show(base), "value": show(new),
                "delta": d, "status": self.status.get(ck, "input")}

    def run(self, changes: dict, outputs: list[str] | None = None) -> dict:
        """Compute the workbook with `changes` ({cell id: new value}). Lists the changes (old -> new), every formula
        cell whose value changed (outputs first), and the key outputs (`outputs`, or the workbook's own)."""
        ch = self._changes(changes)
        base = self.base()
        m = self.twin.model(changes=ch)
        values = m.compute_all(self.twin.order)
        changed = [ck for ck in self.twin.order if not xl.same(base.get(ck), values.get(ck))]
        roles = self.twin.roles
        changed.sort(key=lambda ck: roles.get(ck) != "output")  # stable: precedents first within each
        want = [self.key(c) for c in outputs] if outputs else self.outputs()[:MAX_OUTPUTS]
        for ck in want:
            if ck not in self.twin.group_of and ck not in self.twin.constants:
                raise WhatIfError(f"{self.cell_id(ck)} is empty")
        out_rows = [self._entry(ck, base.get(ck, self.value(ck)), values.get(ck, ch.get(ck, self.value(ck))))
                    for ck in want]
        stale = [ck for ck in self.twin.group_of if self.status[ck] == "not_recomputed"]
        warnings, notices = [], []  # notices: the same, as {code, n}, for a UI to word in its own language
        reads = self._reads_changed(ch)
        hit = [ck for ck in stale if ck in reads]
        if hit:
            notices.append({"code": "untranslated_reads", "n": len(hit)})
            warnings.append(f"{len(hit)} formula cells read the changed cells but are, or read, formulas the twin "
                            "could not translate: those keep Excel's saved values, so these cells do not follow this "
                            "scenario (or follow it only in part)")
        elif stale:
            notices.append({"code": "untranslated", "n": len(stale)})
            warnings.append(f"{len(stale)} formula cells are, or read, formulas the twin could not translate "
                            "(status not_recomputed): they keep Excel's saved values and would not show an effect of "
                            "these changes")
        bad = sum(1 for ck in changed if self.status[ck] in ("failed", "depends_on_failed"))
        if bad:
            notices.append({"code": "failed", "n": bad})
            warnings.append(f"{bad} changed cells belong to (or read) formula groups that failed V1 / V2")
        if self.check["status"] == "not_run":
            notices.append({"code": "not_checked", "n": 0})
            warnings.append("the twin was not checked against Excel yet: run the V1 / V2 check before relying on this")
        return {
            "changes": [{"cell": self.cell_id(ck), "label": self.label(ck), "old": show(self.value(ck)),
                         "new": show(v)} for ck, v in ch.items()],
            "outputs": out_rows,
            "changed": [self._entry(ck, base.get(ck), values.get(ck)) for ck in changed[:MAX_LISTED]],
            "n_changed": len(changed),
            "truncated": len(changed) > MAX_LISTED,
            "warnings": warnings,
            "notices": notices,
            "trust": self.trust(),
            "fingerprint": fingerprint(values),
        }

    # ---- what a cell reads --------------------------------------------------------------------------------------

    def _load_edges(self) -> dict:
        """dst cell -> [(kind, src)] from the IR's edges (ranges kept whole), and range id -> member cell ids."""
        with self._lock:
            if self._edges is None:
                con = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
                try:
                    into: dict = {}
                    for kind, src, dst in con.execute("SELECT src_kind, src_id, dst_cell FROM edges"):
                        into.setdefault(dst, []).append((kind, src))
                    ranges: dict = {}
                    for rid, cid in con.execute("SELECT range_id, cell_id FROM range_cells"):
                        ranges.setdefault(rid, []).append(cid)
                finally:
                    con.close()
                readers: dict = {}
                for dst, srcs in into.items():
                    for _, src in srcs:
                        readers.setdefault(src, []).append(dst)
                member_of: dict = {}
                for rid, cids in ranges.items():
                    for c in cids:
                        member_of.setdefault(c, []).append(rid)
                self._edges = {"into": into, "ranges": ranges, "readers": readers, "member_of": member_of}
            return self._edges

    def reads(self, ck: tuple) -> set:
        """Every cell `ck` reads, directly or not (constants and formulas)."""
        e = self._load_edges()
        into, ranges = e["into"], e["ranges"]
        seen: set = set()
        done_ranges: set = set()
        stack = [self.cell_id(ck)]
        while stack:
            cid = stack.pop()
            for kind, src in into.get(cid, ()):
                if kind == "cell":
                    members = [src]
                elif kind == "range" and src not in done_ranges:
                    done_ranges.add(src)
                    members = ranges.get(src, [])
                else:
                    continue
                for m in members:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
        out = set()
        for cid in seen:
            try:
                out.add(self.key(cid))
            except WhatIfError:
                pass
        return out

    def _reads_changed(self, ch: dict) -> set:
        """Formula cells that read any changed cell (directly or not), from the IR's edges."""
        if not ch:
            return set()
        e = self._load_edges()
        readers, member_of = e["readers"], e["member_of"]
        seen: set = set()
        stack = [self.cell_id(ck) for ck in ch]
        while stack:
            cid = stack.pop()
            for src in [cid, *member_of.get(cid, ())]:
                for dst in readers.get(src, ()):
                    if dst not in seen:
                        seen.add(dst)
                        stack.append(dst)
        out = set()
        for cid in seen:
            try:
                out.add(self.key(cid))
            except WhatIfError:
                pass
        return out

    # ---- one target, many evaluations -------------------------------------------------------------------------------

    def _target(self, cell: str) -> tuple:
        ck = self.key(cell)
        if not self.is_formula(ck):
            raise WhatIfError(f"{self.cell_id(ck)} is not a formula cell: pick a result to analyze")
        if self.status[ck] == "not_recomputed":
            raise WhatIfError(f"{self.cell_id(ck)} is, or reads, a formula the twin could not translate, so it would "
                              "not follow the changes: the answer would be wrong")
        return ck

    def _evaluator(self, target: tuple, extra: dict | None = None):
        """f(changes) -> the target's value; only the formula cells the target reads are computed."""
        cone = {target} | {ck for ck in self.reads(target) if ck in self.twin.group_of}
        order = [ck for ck in self.twin.order if ck in cone]
        fixed = dict(extra or {})

        def f(changes: dict):
            m = self.twin.model(changes={**fixed, **changes})
            m.compute_all(order)
            return m.c(*target)

        return f

    def sensitivity(self, target: str, inputs: list[str] | None = None, step: float = 0.1,
                    changes: dict | None = None) -> dict:
        """Move each input by ±`step` (a fraction: 0.1 = 10 %) on its own and see how far `target` moves. Inputs
        default to the numeric constants the target reads, labelled inputs first. `changes`: a scenario to start
        from (the inputs move from its values)."""
        tk = self._target(target)
        try:
            step = float(step)
        except (TypeError, ValueError):
            raise WhatIfError("step must be a number, e.g. 0.1 for ±10 %") from None
        if not 0 < step <= 10:
            raise WhatIfError("step must be between 0 and 10 (a fraction: 0.1 = ±10 %)")
        start = self._changes(changes or {})
        reads = self.reads(tk)
        if inputs:
            cands = [self.key(c) for c in inputs]
            for ck in cands:
                if self.is_formula(ck):
                    raise WhatIfError(f"{self.cell_id(ck)} holds a formula: pick the constants it reads")
        else:
            nums = [ck for ck in reads if ck not in self.twin.group_of
                    and _number(start.get(ck, self.twin.constants.get(ck))) is not None]
            roles = self.twin.roles
            nums.sort(key=lambda ck: (ck not in self.labels, roles.get(ck) != "input", ck))
            cands = nums
        considered = len(cands)
        cands = cands[:MAX_SENSITIVITY_INPUTS]
        f = self._evaluator(tk, start)
        base = f({})
        if _number(base) is None:
            raise WhatIfError(f"{self.cell_id(tk)} is not a number ({show(base)!r}): sensitivity needs a number")
        rows, skipped = [], []
        for ck in cands:
            v = start.get(ck, self.twin.constants.get(ck))
            if _number(v) is None:
                skipped.append({"cell": self.cell_id(ck), "reason": "not a number"})
                continue
            if v == 0:
                skipped.append({"cell": self.cell_id(ck), "reason": "value is 0: a percent change leaves it at 0"})
                continue
            if ck not in reads:
                skipped.append({"cell": self.cell_id(ck), "reason": f"{self.cell_id(tk)} does not read it"})
                continue
            lo_in, hi_in = v * (1 - step), v * (1 + step)
            lo, hi = f({ck: lo_in}), f({ck: hi_in})
            ok = _number(lo) is not None and _number(hi) is not None
            swing = abs(hi - lo) if ok else None
            el = ((hi - lo) / base) / (2 * step) if ok and base != 0 else None
            rows.append({"cell": self.cell_id(ck), "label": self.label(ck), "value": v, "low_input": show(lo_in),
                         "high_input": show(hi_in), "target_low": show(lo), "target_high": show(hi),
                         "swing": show(swing), "elasticity": show(el)})
        rows.sort(key=lambda r: -(r["swing"] if r["swing"] is not None else -1))
        return {"target": {"cell": self.cell_id(tk), "label": self.label(tk), "value": show(base),
                           "status": self.status[tk]},
                "step": step, "inputs": rows, "skipped": skipped, "considered": considered,
                "truncated": considered > len(cands), "trust": self.trust()}

    def goal_seek(self, target: str, goal, changing: str, changes: dict | None = None,
                  low: float | None = None, high: float | None = None) -> dict:
        """The value of `changing` that makes `target` equal `goal` (as Excel's Goal Seek), searched by secant steps
        from the current value and, if they do not converge, by bisection in a bracket that contains the goal
        (`low` / `high`, or found by widening around the current value)."""
        tk = self._target(target)
        ck = self.key(changing)
        if self.is_formula(ck):
            raise WhatIfError(f"{self.cell_id(ck)} holds a formula: Goal Seek changes a constant")
        try:
            goal = float(goal)
        except (TypeError, ValueError):
            raise WhatIfError("goal must be a number") from None
        start = self._changes(changes or {})
        if ck not in self.reads(tk):
            raise WhatIfError(f"{self.cell_id(tk)} does not read {self.cell_id(ck)}, so changing it cannot reach the goal")
        x0 = start.get(ck, self.twin.constants.get(ck))
        x0 = _number(x0) if _number(x0) is not None else 0.0
        f = self._evaluator(tk, start)
        evals = 0
        tol = 1e-9 * max(1.0, abs(goal))

        def g(x):
            nonlocal evals
            evals += 1
            y = f({ck: x})
            return (y - goal) if _number(y) is not None else None

        best = None

        def keep(x, y):
            nonlocal best
            if y is not None and (best is None or abs(y) < abs(best[1])):
                best = (x, y)

        y0 = g(x0)
        keep(x0, y0)
        found = y0 is not None and abs(y0) <= tol
        # secant
        a, fa = x0, y0
        b = x0 * 1.01 if x0 != 0 else 0.01
        fb = g(b)
        keep(b, fb)
        it = 0
        while not found and it < GOAL_SEEK_ITERATIONS and fa is not None and fb is not None:
            it += 1
            if abs(fb) <= tol:
                found = True
                break
            if fb == fa:
                break
            c = b - fb * (b - a) / (fb - fa)
            if not math.isfinite(c) or abs(c) > 1e300:
                break
            a, fa = b, fb
            b, fb = c, g(c)
            keep(b, fb)
        if not found and best is not None and abs(best[1]) <= tol:
            found = True
        # bisection in a bracket
        if not found:
            br = self._bracket(g, x0, low, high, keep)
            if br is not None:
                (lo, flo), (hi, _) = br
                for _ in range(200):
                    mid = (lo + hi) / 2
                    fm = g(mid)
                    keep(mid, fm)
                    if fm is None:
                        break
                    if abs(fm) <= tol or hi - lo <= 1e-12 * max(1.0, abs(mid)):
                        found = abs(fm) <= max(tol, 1e-6 * max(1.0, abs(goal)))
                        break
                    if (fm < 0) == (flo < 0):
                        lo, flo = mid, fm
                    else:
                        hi = mid
        if best is None:
            raise WhatIfError(f"{self.cell_id(tk)} gives no number for any value tried")
        x, y = best
        return {"target": {"cell": self.cell_id(tk), "label": self.label(tk), "status": self.status[tk],
                           "before": show(y0 + goal) if y0 is not None else None, "goal": goal,
                           "achieved": show(y + goal)},
                "changing": {"cell": self.cell_id(ck), "label": self.label(ck), "before": x0, "value": show(x)},
                "found": bool(found), "evaluations": evals,
                "note": None if found else "no value reaches the goal within tolerance: the closest one is given",
                "trust": self.trust()}

    @staticmethod
    def _bracket(g, x0, low, high, keep):
        """Two values of the changing cell on either side of the goal, or None."""
        pts = []
        if low is not None and high is not None:
            pts = [float(low), float(high)]
        else:
            scale = max(abs(x0), 1.0)
            pts = [x0]
            k = 1.0
            while k <= 2 ** 40:
                pts += [x0 - k * scale, x0 + k * scale]
                k *= 2
        vals = []
        for x in pts:
            y = g(x)
            keep(x, y)
            if y is None:
                continue
            for xv, yv in vals:
                if (y < 0) != (yv < 0):
                    lo, hi = sorted([(x, y), (xv, yv)])
                    return lo, hi
            vals.append((x, y))
            if y == 0:
                return (x, y), (x, y)
        return None


def fingerprint(values: dict) -> str:
    """A digest of every computed value (numbers to 12 significant digits): two runs that agree have the same one."""
    h = hashlib.sha256()
    for ck in sorted(values, key=lambda k: (k[0], k[1], k[2])):
        v = values[ck]
        if isinstance(v, float) and not isinstance(v, bool):
            v = float(f"{v:.12g}") if math.isfinite(v) else str(v)
        h.update(json.dumps([ck[0], ck[1], ck[2], show(v)], ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()[:16]


def result_fingerprint(result: dict) -> str:
    """A digest of an analysis result without what says how far to trust it (that changes when the twin is checked
    again, the values do not)."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k not in ("trust", "status", "fingerprint", "evaluations")}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    body = strip(result)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
