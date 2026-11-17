"""Questions with gold answers computed from the clean World (design §11.2).

Categories: single_lookup, aggregate, multi_tab_join, fragmented_period, trap_units, trap_names, trap_totals. A question
is only asked when the workbook has what makes it a trap (trap_units needs unit_shift, and so on). Money answers are in
millions of VND (triệu đồng), as the questions say.

answer_type: number (with tolerance, relative), text (`accept`: any of these strings, compared unaccented and
lowercase), list (every item of `answer` must appear).
"""

from __future__ import annotations

import random

from .base_data import World

MONTH_WORDS = lambda m, y: [f"tháng {m}", f"thang {m}", f"t{m}", f"{y}-{m:02d}", f"{m:02d}/{y}", f"{m}/{y}",  # noqa: E731
                            f"tháng {m:02d}", f"month {m}"]


def _q(qid, wb, question, answer, atype, category, sources, **kw):
    return {"id": qid, "workbook": wb, "question": question, "answer": answer, "answer_type": atype,
            "category": category, "gold_sources": [{"sheet": s} for s in sources], **kw}


def make_questions(world: World, wb_name: str, gold: dict, rng: random.Random, n: int = 6) -> list[dict]:
    p = set(gold["perturbations"])
    split = "split_period" in p
    rev_sheets = [r["sheet"] for r in gold["revenue_sheets"]]
    month_sheet = (lambda m: f"DT_T{m}") if split else (lambda m: "DoanhThu")
    br = world.branches
    by_code = {b["ma_cn"]: b for b in br}
    emp_branch = {e["ma_nv"]: e["ma_cn"] for e in world.employees}
    total = lambda rows, k: round(sum(x[k] for x in rows), 1)  # noqa: E731
    pool: list[tuple[str, dict]] = []
    stem = wb_name.rsplit(".", 1)[0]

    # ---- single_lookup ---------------------------------------------------------------------------------------------
    b = rng.choice(br)
    pool.append(("single_lookup", dict(question=f"Chi nhánh có mã {b['ma_cn']} nằm ở thành phố nào?",
                                       answer=b["thanh_pho"], atype="text", accept=[b["thanh_pho"]],
                                       sources=["ChiNhanh"])))
    e = rng.choice(world.employees)
    pool.append(("single_lookup", dict(question=f"Lương của nhân viên {e['ho_ten']} là bao nhiêu (triệu đồng)?",
                                       answer=e["luong"], atype="number", sources=["NhanSu"])))
    x = rng.choice(world.products)
    pool.append(("single_lookup", dict(question=f"Đơn giá của sản phẩm {x['ten_sp']} là bao nhiêu (nghìn đồng)?",
                                       answer=x["don_gia"], atype="number", sources=["SanPham"])))

    # ---- aggregate -------------------------------------------------------------------------------------------------
    dept = rng.choice(sorted({e["phong_ban"] for e in world.employees}))
    pool.append(("aggregate", dict(question=f"Có bao nhiêu nhân viên thuộc phòng {dept}?",
                                   answer=sum(e["phong_ban"] == dept for e in world.employees), atype="number",
                                   sources=["NhanSu"])))
    m = rng.choice([m for m in range(4, 13)])
    pool.append(("aggregate", dict(question=f"Tổng doanh thu tháng {m} của toàn bộ chi nhánh là bao nhiêu (triệu đồng)?",
                                   answer=total(world.revenue[m], "doanh_thu"), atype="number",
                                   sources=[month_sheet(m)])))

    # ---- multi_tab_join --------------------------------------------------------------------------------------------
    region = rng.choice(sorted({b["mien"] for b in br}))
    m2 = rng.choice(range(4, 13))
    rows = [r for r in world.revenue[m2] if by_code[r["ma_cn"]]["mien"] == region]
    pool.append(("multi_tab_join", dict(
        question=f"Tổng doanh thu tháng {m2} của các chi nhánh miền {region} là bao nhiêu (triệu đồng)?",
        answer=total(rows, "doanh_thu"), atype="number", sources=["ChiNhanh", month_sheet(m2)])))
    top_pay = max(e["luong"] for e in world.employees)
    regions = sorted({by_code[emp_branch[e["ma_nv"]]]["mien"] for e in world.employees if e["luong"] == top_pay})
    pool.append(("multi_tab_join", dict(
        question="Nhân viên có lương cao nhất làm việc ở chi nhánh thuộc miền nào?", answer=regions[0], atype="text",
        accept=[f"miền {r}" for r in regions] + regions, sources=["NhanSu", "ChiNhanh"])))
    yearly = {b["ma_cn"]: sum(r["doanh_thu"] for m in range(1, 13) for r in world.revenue[m] if r["ma_cn"] == b["ma_cn"])
              for b in br}
    t = {x["ma_cn"]: x["muc_tieu"] for x in world.targets}
    hit = sorted(by_code[c]["ten_cn"] for c in yearly if yearly[c] >= t[c])
    if 0 < len(hit) <= 6:
        pool.append(("multi_tab_join", dict(
            question=f"Những chi nhánh nào đạt hoặc vượt mục tiêu doanh thu năm {world.year}?",
            answer=hit, atype="list", sources=["ChiNhanh", gold["targets_sheet"], *rev_sheets])))

    # ---- fragmented_period -----------------------------------------------------------------------------------------
    b2 = rng.choice(br)
    pool.append(("fragmented_period", dict(
        question=f"Tổng doanh thu cả năm {world.year} của {b2['ten_cn']} là bao nhiêu (triệu đồng)?",
        answer=round(yearly[b2["ma_cn"]], 1), atype="number", sources=rev_sheets)))
    month_totals = {m: sum(r["doanh_thu"] for r in world.revenue[m]) for m in range(1, 13)}
    best = max(month_totals, key=month_totals.get)
    pool.append(("fragmented_period", dict(
        question=f"Tháng nào trong năm {world.year} có tổng doanh thu toàn hệ thống cao nhất?",
        answer=f"tháng {best}", atype="text", accept=MONTH_WORDS(best, world.year), sources=rev_sheets)))
    q3 = [7, 8, 9]
    pool.append(("fragmented_period", dict(
        question=f"Tổng số đơn hàng quý 3 năm {world.year} của {b2['ten_cn']} là bao nhiêu?",
        answer=sum(r["so_don"] for m in q3 for r in world.revenue[m] if r["ma_cn"] == b2["ma_cn"]), atype="number",
        sources=[month_sheet(m) for m in q3])))

    # ---- traps -----------------------------------------------------------------------------------------------------
    if "unit_shift" in p and split:
        b3 = rng.choice(br)
        pool.append(("trap_units", dict(
            question=f"Tổng doanh thu quý 1 năm {world.year} của {b3['ten_cn']} là bao nhiêu (triệu đồng)?",
            answer=round(sum(r["doanh_thu"] for m in (1, 2, 3) for r in world.revenue[m] if r["ma_cn"] == b3["ma_cn"]), 1),
            atype="number", sources=["DT_T1", "DT_T2", "DT_T3"])))
        pool.append(("trap_units", dict(
            question=f"Tổng chi phí tháng 2 và tháng 5 của toàn bộ chi nhánh là bao nhiêu (triệu đồng)?",
            answer=round(total(world.revenue[2], "chi_phi") + total(world.revenue[5], "chi_phi"), 1), atype="number",
            sources=["DT_T2", "DT_T5"])))
    if "rename_cols" in p and split:
        pool.append(("trap_names", dict(
            question=f"Tổng doanh thu 6 tháng đầu năm {world.year} của toàn bộ chi nhánh là bao nhiêu (triệu đồng)?",
            answer=round(sum(month_totals[m] for m in range(1, 7)), 1), atype="number",
            sources=[f"DT_T{m}" for m in range(1, 7)])))
    if "total_rows" in p:
        m3 = rng.choice(range(4, 13))
        pool.append(("trap_totals", dict(
            question=f"Tổng số đơn hàng tháng {m3} của toàn bộ chi nhánh là bao nhiêu?",
            answer=sum(r["so_don"] for r in world.revenue[m3]), atype="number", sources=[month_sheet(m3)])))
        pool.append(("trap_totals", dict(
            question=f"Tháng {m3} có bao nhiêu chi nhánh có doanh thu trên "
                     f"{round(sorted(r['doanh_thu'] for r in world.revenue[m3])[len(br) // 2])} triệu đồng?",
            answer=sum(r["doanh_thu"] > round(sorted(x["doanh_thu"] for x in world.revenue[m3])[len(br) // 2])
                       for r in world.revenue[m3]),
            atype="number", sources=[month_sheet(m3)])))

    # pick n: every trap present, then the others, covering categories evenly
    traps = [x for x in pool if x[0].startswith("trap")]
    rest = [x for x in pool if not x[0].startswith("trap")]
    rng.shuffle(rest)
    by_cat: dict[str, list] = {}
    for c, q in rest:
        by_cat.setdefault(c, []).append((c, q))
    chosen = traps[: max(0, n - 3)]
    while len(chosen) < n and any(by_cat.values()):
        for c in list(by_cat):
            if by_cat[c] and len(chosen) < n:
                chosen.append(by_cat[c].pop())
    out = []
    for i, (cat, q) in enumerate(chosen, start=1):
        sources = q.pop("sources")
        atype = q.pop("atype")
        out.append(_q(f"{stem}_q{i:02d}", wb_name, q.pop("question"), q.pop("answer"), atype, cat, sources,
                      **({"tolerance": 1e-3} if atype == "number" else {}), **q))
    return out
