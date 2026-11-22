"""Write a World as a messy Excel workbook (design §11.1), recording where each table is (gold for detection).

Perturbations, each on or off:

    title_rows    1–3 caption rows (company, report title with the period, unit) above each table
    multi_header  two-level header on the revenue tables, with merged group labels
    multi_table   a second table (yearly targets) under the branch list on the same sheet
    total_rows    subtotal per region and a grand total (SUM formulas with stored results) in the revenue tables
    notes         a long note row right under each revenue table
    split_period  revenue as one sheet per month (DT_T1 … DT_T12) instead of one long sheet
    rename_cols   months 4–6 call the columns 'Doanh số' and 'SL đơn'
    unit_shift    months 1–3 in thousands of VND (nghìn đồng) instead of millions
    hidden        a hidden column in the staff list and a hidden scratch sheet
    formats       number formats (#,##0.0, dd/mm/yyyy, 0.0%) and a cost-ratio column computed by formula
"""

from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from sheetir.convert import _write_cached

from .base_data import World

PERTURBATIONS = ("title_rows", "multi_header", "multi_table", "total_rows", "notes", "split_period", "rename_cols",
                 "unit_shift", "hidden", "formats")
BOLD = Font(bold=True)
COMPANY = "CÔNG TY CỔ PHẦN ĐIỆN MÁY SAO VIỆT"


class Writer:
    def __init__(self, wb: Workbook, perturb: set[str], rng: random.Random):
        self.wb, self.p, self.rng = wb, perturb, rng
        self.cached: dict[int, dict[tuple[int, int], object]] = {}
        self.gold: list[dict] = []

    def sheet(self, name: str):
        ws = self.wb.create_sheet(name)
        return ws, self.wb.sheetnames.index(name)

    def titles(self, ws, row: int, lines: list[str]) -> int:
        """Caption rows from `row` when title_rows is on; returns the first row after them (and a blank row)."""
        if "title_rows" not in self.p:
            return row
        lines = lines[: self.rng.randint(1, len(lines))] if len(lines) > 1 else lines
        for i, text in enumerate(lines):
            ws.cell(row + i, 1, text).font = BOLD if i == 0 else Font()
        return row + len(lines) + 1

    def table(self, ws, idx: int, top: int, headers: list[str], rows: list[list], *, name: str,
              groups: list[tuple[str, int, int]] | None = None, formats: dict[int, str] | None = None,
              subtotals: list[tuple[str, int, int]] | None = None, total: str | None = None,
              sum_cols: list[int] | None = None, ratio: tuple[int, int, int] | None = None) -> int:
        """Write a table at `top` (column A). groups: (label, first col, last col) above the header, merged; a column
        no group covers has its header merged down over both rows. subtotals: (label, first data index, last data
        index) after which a subtotal row is inserted. ratio: (col, numerator col, denominator col) of a formula
        column. Returns the row after the table."""
        formats = formats or {}
        header_rows = []
        r = top
        if groups:
            covered = set()
            for label, c1, c2 in groups:
                ws.cell(r, c1, label).font = BOLD
                ws.cell(r, c1).alignment = Alignment(horizontal="center")
                if c2 > c1:
                    ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
                covered |= set(range(c1, c2 + 1))
            for c, h in enumerate(headers, start=1):
                if c in covered:
                    ws.cell(r + 1, c, h).font = BOLD
                else:
                    ws.cell(r, c, h).font = BOLD
                    ws.merge_cells(start_row=r, start_column=c, end_row=r + 1, end_column=c)
            header_rows = [r, r + 1]
            r += 2
        else:
            for c, h in enumerate(headers, start=1):
                ws.cell(r, c, h).font = BOLD
            header_rows = [r]
            r += 1
        first_data = r
        total_rows, sub_rows = [], []
        bounds = {s[2]: s for s in (subtotals or [])}
        group_start = r
        for i, values in enumerate(rows):
            for c, v in enumerate(values, start=1):
                cell = ws.cell(r, c, v)
                if c in formats:
                    cell.number_format = formats[c]
            if ratio:
                self._ratio(ws, idx, r, ratio, formats)
            r += 1
            if i in bounds:
                label = bounds[i][0]
                ws.cell(r, 1, label).font = BOLD
                for c in sum_cols or []:
                    col = get_column_letter(c)
                    ws.cell(r, c, f"=SUM({col}{group_start}:{col}{r - 1})").font = BOLD
                    self.cached.setdefault(idx, {})[(r, c)] = round(sum(
                        ws.cell(k, c).value for k in range(group_start, r) if isinstance(ws.cell(k, c).value, (int, float))), 6)
                    if c in formats:
                        ws.cell(r, c).number_format = formats[c]
                if ratio:
                    self._ratio(ws, idx, r, ratio, formats)
                sub_rows.append(r)
                r += 1
                group_start = r
        if total:
            ws.cell(r, 1, total).font = BOLD
            for c in sum_cols or []:
                col = get_column_letter(c)
                if sub_rows:
                    ws.cell(r, c, "=" + "+".join(f"{col}{k}" for k in sub_rows)).font = BOLD
                    value = sum(self.cached[idx][(k, c)] for k in sub_rows)
                else:
                    ws.cell(r, c, f"=SUM({col}{first_data}:{col}{r - 1})").font = BOLD
                    value = sum(ws.cell(k, c).value for k in range(first_data, r)
                                if isinstance(ws.cell(k, c).value, (int, float)))
                self.cached.setdefault(idx, {})[(r, c)] = round(value, 6)
                if c in formats:
                    ws.cell(r, c).number_format = formats[c]
            if ratio:
                self._ratio(ws, idx, r, ratio, formats)
            total_rows.append(r)
            r += 1
        last = r - 1
        ncols = len(headers)
        self.gold.append({"name": name, "sheet": ws.title,
                          "range": f"A{header_rows[0]}:{get_column_letter(ncols)}{last}",
                          "header_rows": header_rows, "total_rows": sub_rows + total_rows})
        return r

    def _ratio(self, ws, idx, r, ratio, formats):
        c, num, den = ratio
        a, b = get_column_letter(num), get_column_letter(den)
        ws.cell(r, c, f"=IF({b}{r}=0,0,{a}{r}/{b}{r})")
        ws.cell(r, c).number_format = "0.0%"
        nv = self.cached.get(idx, {}).get((r, num), ws.cell(r, num).value)
        dv = self.cached.get(idx, {}).get((r, den), ws.cell(r, den).value)
        if isinstance(nv, (int, float)) and isinstance(dv, (int, float)):
            self.cached.setdefault(idx, {})[(r, c)] = round(nv / dv, 6) if dv else 0


def _revenue_sheet(w: Writer, world: World, month: int | None):
    p = w.p
    split = month is not None
    thousands = split and "unit_shift" in p and month <= 3
    renamed = split and "rename_cols" in p and 4 <= month <= 6
    unit = "nghìn đồng" if thousands else "triệu đồng"
    factor = 1000 if thousands else 1
    name = f"DT_T{month}" if split else "DoanhThu"
    ws, idx = w.sheet(name)
    period = f"THÁNG {month}/{world.year}" if split else f"NĂM {world.year}"
    top = w.titles(ws, 1, [COMPANY, f"BÁO CÁO DOANH THU {period}", f"Đơn vị tính: {unit}"])
    money = "#,##0.0" if "formats" in p else None
    headers = ([] if split else ["Tháng"]) + [
        "Mã CN", f"{'Doanh số' if renamed else 'Doanh thu'} ({unit})", "SL đơn" if renamed else "Số đơn",
        f"Chi phí ({unit})"]
    off = 0 if split else 1
    with_ratio = "formats" in p
    if with_ratio:
        headers.append("Tỷ lệ chi phí")
    formats = {2 + off: money, 4 + off: money} if money else {}
    formats = {k: v for k, v in formats.items() if v}
    months = [month] if split else list(range(1, 13))
    by_code = {b["ma_cn"]: b for b in world.branches}
    rows, region_ends = [], []
    order = [b["ma_cn"] for b in world.branches]  # already grouped Bắc, Trung, Nam
    for m in months:
        rev = {x["ma_cn"]: x for x in world.revenue[m]}
        for code in order:
            x = rev[code]
            vals = ([] if split else [m]) + [code, round(x["doanh_thu"] * factor, 1), x["so_don"],
                                             round(x["chi_phi"] * factor, 1)]
            rows.append(vals + ([None] if with_ratio else []))
    subtotals = None
    if split and "total_rows" in p:
        subtotals = []
        for i, code in enumerate(order):
            nxt = order[i + 1] if i + 1 < len(order) else None
            region = by_code[code]["mien"]
            if nxt is None or by_code[nxt]["mien"] != region:
                subtotals.append((f"Cộng miền {region}", None, i))
    groups = None
    if "multi_header" in p:
        groups = [("Kết quả kinh doanh", 2 + off, len(headers))]
    sum_cols = [2 + off, 3 + off, 4 + off]
    end = w.table(ws, idx, top, headers, rows, name=f"revenue_{month or 'all'}", groups=groups, formats=formats,
                  subtotals=subtotals, total="Tổng cộng" if "total_rows" in p else None, sum_cols=sum_cols,
                  ratio=(len(headers), 4 + off, 2 + off) if with_ratio else None)
    if "notes" in p:
        ws.cell(end, 1, "Ghi chú: số liệu chưa bao gồm thuế VAT, được tổng hợp tự động từ hệ thống bán hàng của "
                        "các chi nhánh.")
    ws.column_dimensions["A"].width = 14
    return {"sheet": name, "unit": "nghin_vnd" if thousands else "trieu_vnd", "renamed": renamed}


def write_workbook(world: World, path: str | Path, perturb: set[str], seed: int = 0) -> dict:
    """Write the workbook; returns {"tables": gold tables, "sheets": {...}, "perturbations": [...]}."""
    rng = random.Random(seed)
    wb = Workbook()
    wb.remove(wb.active)
    w = Writer(wb, set(perturb), rng)
    p = w.p

    ws, idx = w.sheet("ChiNhanh")
    top = w.titles(ws, 1, [COMPANY, "DANH SÁCH CHI NHÁNH"])
    end = w.table(ws, idx, top, ["Mã CN", "Tên chi nhánh", "Miền", "Thành phố"],
                  [[b["ma_cn"], b["ten_cn"], b["mien"], b["thanh_pho"]] for b in world.branches], name="branches")
    targets_sheet = "ChiNhanh"
    if "multi_table" in p:
        r = end + 2
        ws.cell(r, 1, f"MỤC TIÊU DOANH THU NĂM {world.year} (triệu đồng)").font = BOLD
        w.table(ws, idx, r + 1, ["Mã CN", "Mục tiêu (triệu đồng)"],
                [[t["ma_cn"], t["muc_tieu"]] for t in world.targets], name="targets")
    else:
        ws2, idx2 = w.sheet("MucTieu")
        top = w.titles(ws2, 1, [f"MỤC TIÊU DOANH THU NĂM {world.year}"])
        w.table(ws2, idx2, top, ["Mã CN", "Mục tiêu (triệu đồng)"], [[t["ma_cn"], t["muc_tieu"]] for t in world.targets],
                name="targets")
        targets_sheet = "MucTieu"

    ws, idx = w.sheet("SanPham")
    top = w.titles(ws, 1, [COMPANY, "DANH MỤC SẢN PHẨM", "Đơn giá: nghìn đồng"])
    w.table(ws, idx, top, ["Mã SP", "Tên sản phẩm", "Nhóm hàng", "Đơn giá (nghìn đồng)"],
            [[x["ma_sp"], x["ten_sp"], x["nhom"], x["don_gia"]] for x in world.products], name="products",
            formats={4: "#,##0"} if "formats" in p else None)

    revenue = [_revenue_sheet(w, world, m) for m in range(1, 13)] if "split_period" in p \
        else [_revenue_sheet(w, world, None)]

    ws, idx = w.sheet("NhanSu")
    top = w.titles(ws, 1, [COMPANY, "DANH SÁCH NHÂN SỰ"])
    headers = ["Mã NV", "Họ và tên", "Mã CN", "Phòng ban", "Lương (triệu đồng)", "Ngày vào làm"]
    rows = [[e["ma_nv"], e["ho_ten"], e["ma_cn"], e["phong_ban"], e["luong"], e["ngay_vao_lam"]] for e in world.employees]
    if "hidden" in p:
        headers.append("Ghi chú nội bộ")
        rows = [r + [rng.choice(["", "", "thử việc", "chuyển CN"]) or None] for r in rows]
    formats = {6: "dd/mm/yyyy", 5: "#,##0.0"} if "formats" in p else {6: "yyyy-mm-dd"}
    w.table(ws, idx, top, headers, rows, name="employees", formats=formats)
    if "hidden" in p:
        ws.column_dimensions["G"].hidden = True
        scratch, _ = w.sheet("Nhap")
        for r in range(1, 4):
            for c in range(1, 4):
                scratch.cell(r, c, rng.randint(1, 99))
        scratch.sheet_state = "hidden"

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    if w.cached:
        _write_cached(path, w.cached)
    return {"tables": w.gold, "revenue_sheets": revenue, "targets_sheet": targets_sheet,
            "perturbations": sorted(p)}
