"""The agent's what-if tools: questions such as "what if the rate were 3 %?", "which input matters most for the total?"
or "what price gives a 20 % margin?" are answered by the Python twin (sheetir.whatif), never by arithmetic in SQL or in
the model. Each analysis is recorded in the project's history (source "agent", the question as its note), and its rows
are kept under the record's id (S12), which the answer cites like a query result.

    model_cells {"workbook"?, "query"?}                        inputs and key outputs, with their cells and values
    what_if     {"workbook"?, "changes": {cell: value}, "outputs"?: [cell]}
    sensitivity {"workbook"?, "target": cell, "inputs"?: [cell], "step"?: 0.1}
    goal_seek   {"workbook"?, "target": cell, "goal": number, "changing": cell}
    propose_correction {"workbook"?, "edits": {cell: contents as typed in Excel}, "reason": str}
                a pending change of the stored data (C13), applied only when the user approves it in the history
                (POST …/history/C13/approve): the model has no tool to approve
"""

from __future__ import annotations

from .. import xl
from ..history import History, HistoryError
from ..whatif import Engine, WhatIfError, result_fingerprint, show

NAMES = ("model_cells", "what_if", "sensitivity", "goal_seek", "propose_correction")
MAX_ROWS = 30
UNTRUSTED = ("not_recomputed", "failed", "depends_on_failed")

PROMPT = """What-if tools (the workbook's formulas run as Python; the stored data never changes):
- model_cells {{"workbook": str?, "query": str?}}: the workbook's labelled inputs and key outputs with their cell ids
  and current values. Use it to find the cells a what-if question is about. Workbooks: {workbooks}.
- what_if {{"workbook": str?, "changes": {{"Sheet!B3": value, ...}}, "outputs": [cell]?}}: recompute with some
  values changed; rows are the changed cells (base, value, delta, status).
- sensitivity {{"workbook": str?, "target": cell, "inputs": [cell]?, "step": 0.1}}: move each input ±step (a fraction)
  and see how far the target swings, largest first.
- goal_seek {{"workbook": str?, "target": cell, "goal": number, "changing": cell}}: the value of `changing` that makes
  `target` equal `goal`.
- propose_correction {{"workbook": str?, "edits": {{"Sheet!B3": "contents as typed in Excel", ...}}, "reason": str}}:
  only when the user asks to change or correct the stored data itself ("change B3 to 120", "the rate should be 3%,
  fix it"), not for a what-if. It changes nothing yet: it records a pending proposal (C13) with the old and new
  contents and the effect on the key outputs. Tell the user the proposal's id, what it changes and that it is applied
  only after they approve it in the history. You cannot approve it; never say the data was changed.
Rules for these tools: use them for every question about changing values ("what if", "how much would", "what is
needed to"), never compute the effect yourself or in SQL. A formula cell cannot be changed: change the cells it reads.
Their result_id (S12) is cited in final_answer like a query result, and the answer names it ("scenario S12") so the
user can find it in the history. If a row's status is not_recomputed, failed or depends_on_failed, say that this
value cannot be trusted and why (the formula could not be translated, or failed the check against Excel)."""


class WhatIfTools:
    """`workbooks`: [(id, file name, db path)] the agent may compute, the one in view first. `history`: where the
    analyses are recorded (None: not recorded)."""

    def __init__(self, workbooks: list[tuple[str, str, object]], history: History | None, author: str,
                 question: str = ""):
        self.workbooks = workbooks
        self.history = history
        self.author = author
        self.question = question
        self.records: list[str] = []
        self.cells = 0  # model_cells calls (their results are kept as m1, m2, …)

    def prompt(self) -> str:
        names = ", ".join(f"{name} (id {wid})" for wid, name, _ in self.workbooks)
        return PROMPT.format(workbooks=names or "none")

    def _pick(self, which) -> tuple[str, str, Engine]:
        if not self.workbooks:
            raise WhatIfError("no workbook with a Python twin in this project")
        if which in (None, ""):
            wid, name, db = self.workbooks[0]
        else:
            w = str(which).strip().lower()
            hit = [x for x in self.workbooks if x[0] == w or x[1].lower() == w or x[1].lower().rsplit(".", 1)[0] == w]
            if not hit:
                raise WhatIfError(f"no workbook {which!r}: use one of " +
                                  ", ".join(f"{n} ({i})" for i, n, _ in self.workbooks))
            wid, name, db = hit[0]
        return wid, name, Engine.cached(db)

    def dispatch(self, tool: str, args: dict) -> dict:
        try:
            if tool == "model_cells":
                return self.model_cells(args.get("workbook"), args.get("query"))
            if tool == "what_if":
                return self.what_if(args.get("workbook"), args.get("changes") or {}, args.get("outputs"))
            if tool == "sensitivity":
                return self.sensitivity(args.get("workbook"), args.get("target"), args.get("inputs"),
                                        args.get("step", 0.1))
            if tool == "propose_correction":
                return self.propose_correction(args.get("workbook"), args.get("edits"), args.get("reason"))
            if tool == "goal_seek":
                return self.goal_seek(args.get("workbook"), args.get("target"), args.get("goal"),
                                      args.get("changing"))
        except (WhatIfError, HistoryError) as e:
            return {"error": str(e)}
        return {"error": f"unknown tool {tool!r}"}

    # ---- tools ------------------------------------------------------------------------------------------------------

    def model_cells(self, which=None, query=None) -> dict:
        wid, name, eng = self._pick(which)
        q = str(query or "").strip().lower()

        def keep(ck) -> bool:
            if not q:
                return True
            return q in eng.cell_id(ck).lower() or q in str(eng.label(ck) or "").lower()

        roles = eng.twin.roles
        inputs = [ck for ck in eng.labels if ck not in eng.twin.group_of] + \
            [ck for ck, r in roles.items() if r == "input" and ck not in eng.labels and ck in eng.twin.constants]
        inputs = [ck for ck in dict.fromkeys(inputs) if keep(ck)]
        outputs = [ck for ck in eng.outputs() if keep(ck)]
        row = lambda ck, kind: [kind, eng.cell_id(ck), eng.label(ck), show(eng.value(ck)),  # noqa: E731
                                eng.status.get(ck, "input")]
        rows = [row(ck, "input") for ck in inputs[:MAX_ROWS]] + [row(ck, "output") for ck in outputs[:MAX_ROWS]]
        self.cells += 1
        return self._kept(f"m{self.cells}", ["kind", "cell", "label", "value", "status"], rows,
                          {"workbook": f"{name} ({wid})", "n_inputs": len(inputs), "n_outputs": len(outputs),
                           "check": eng.check["status"]})

    def _record(self, kind, wid, name, eng, changes, params, result, fp) -> str | None:
        if self.history is None:
            return None
        from ..web.provenance import read  # the workbook's full SHA-256, from its source record
        try:
            sha = read(eng.db.parent)["sha256"]
        except Exception:  # noqa: BLE001
            sha = None
        rec = self.history.add(kind, {"id": wid, "name": name, "sha256": sha}, self.author, "agent", changes,
                               note=self.question[:500] or None, params=params, result=result, fingerprint=fp,
                               twin_status=eng.check["status"])
        self.records.append(rec["ref"])
        return rec["ref"]

    def _kept(self, ref, columns, rows, extra: dict) -> dict:
        """The tool's answer, with `result_id` and rows the loop keeps like a query result."""
        rid = ref or f"w{len(self.records) + 1}"
        return {"result_id": rid, "sql": "", "tables": [], "columns": columns, "rows": rows,
                "row_count": len(rows), "truncated": False, **extra}

    def what_if(self, which, changes, outputs=None) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise WhatIfError('changes must be an object such as {"Inputs!B1": 3}')
        wid, name, eng = self._pick(which)
        canon = {eng.cell_id(eng.key(c)): v for c, v in changes.items()}
        r = eng.run(canon, outputs if isinstance(outputs, list) and outputs else None)
        ref = self._record("scenario", wid, name, eng, canon, {"outputs": outputs}, r, r["fingerprint"])
        seen, rows = set(), []
        for x in r["outputs"] + r["changed"]:
            if x["cell"] in seen:
                continue
            seen.add(x["cell"])
            if x["delta"] in (None, 0) and xl.same(x["base"], x["value"]):
                continue
            rows.append([x["cell"], x["label"], x["base"], x["value"], x["delta"], x["status"]])
        return self._kept(ref, ["cell", "label", "base", "value", "delta", "status"], rows[:MAX_ROWS],
                          {"changes": r["changes"], "n_changed": r["n_changed"], "warnings": r["warnings"],
                           "untrusted": sum(1 for x in rows if x[5] in UNTRUSTED)})

    def sensitivity(self, which, target, inputs=None, step=0.1) -> dict:
        wid, name, eng = self._pick(which)
        r = eng.sensitivity(str(target or ""), inputs if isinstance(inputs, list) and inputs else None, step)
        params = {"target": r["target"]["cell"], "inputs": inputs, "step": r["step"]}
        ref = self._record("sensitivity", wid, name, eng, {}, params, r, result_fingerprint(r))
        rows = [[x["cell"], x["label"], x["value"], x["low_input"], x["high_input"], x["target_low"],
                 x["target_high"], x["swing"]] for x in r["inputs"][:MAX_ROWS]]
        return self._kept(ref, ["cell", "label", "value", "low_input", "high_input", "target_low", "target_high",
                                "swing"], rows, {"target": r["target"], "step": r["step"], "skipped": r["skipped"][:10]})

    def goal_seek(self, which, target, goal, changing) -> dict:
        wid, name, eng = self._pick(which)
        r = eng.goal_seek(str(target or ""), goal, str(changing or ""))
        changes = {r["changing"]["cell"]: r["changing"]["value"]}
        params = {"target": r["target"]["cell"], "goal": r["target"]["goal"], "changing": r["changing"]["cell"]}
        ref = self._record("goal_seek", wid, name, eng, changes, params, r, result_fingerprint(r))
        t, c = r["target"], r["changing"]
        rows = [[t["cell"], t["label"], t["before"], t["goal"], t["achieved"], c["cell"], c["label"], c["before"],
                 c["value"], r["found"], t["status"]]]
        return self._kept(ref, ["target", "target_label", "target_before", "goal", "achieved", "changing",
                                "changing_label", "changing_before", "changing_value", "found", "status"], rows,
                          {"note": r["note"]})

    def propose_correction(self, which, edits, reason) -> dict:
        if self.history is None:
            raise WhatIfError("corrections are not recorded here, so none can be proposed")
        if not isinstance(edits, dict) or not edits:
            raise WhatIfError('edits must be an object such as {"Inputs!B1": "3"}')
        if not str(reason or "").strip():
            raise WhatIfError("give the reason for the change (reason), in the user's words")
        wid, name, eng = self._pick(which)
        from ..web.history_api import make_proposal  # noqa: PLC0415 - the web layer imports this module
        from ..web.provenance import read  # noqa: PLC0415

        sha = read(eng.db.parent)["sha256"]
        rec = make_proposal(self.history, "correction", {"id": wid, "name": name, "sha256": sha}, edits,
                            {"reason": str(reason).strip()[:2000], "question": self.question[:500] or None},
                            self.author, "agent", eng.db.parent)
        self.records.append(rec["ref"])
        old = rec["params"]["old"]
        rows = [["edit", c, eng.label(eng.key(c)), show(typed_or_text(old.get(c, ""))), show(typed_or_text(t))]
                for c, t in rec["changes"].items()]
        impact = (rec["result"] or {}).get("impact") or {}
        for x in impact.get("outputs", []) + impact.get("changed", []):
            if len(rows) >= MAX_ROWS:
                break
            if x["delta"] not in (None, 0) and not any(r[1] == x["cell"] for r in rows):
                rows.append(["effect", x["cell"], x["label"], x["base"], x["value"]])
        return self._kept(rec["ref"], ["kind", "cell", "label", "before", "after"], rows,
                          {"status": "pending: applied only after the user approves it",
                           "warnings": impact.get("warnings", [])})


def typed_or_text(text: str):
    """A cell's contents as typed (from a correction) to a value, formulas kept as text."""
    from ..whatif import typed  # noqa: PLC0415

    t = text or ""
    return t if t.startswith("=") else typed(t)
