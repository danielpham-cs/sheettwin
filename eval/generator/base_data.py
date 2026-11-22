"""Clean business data for one synthetic company (Vietnamese retail chain): branches, products, monthly revenue per
branch, employees, yearly targets. Deterministic for a seed. Money is in millions of VND (triệu đồng)."""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

CITIES = [  # (city, region, code prefix)
    ("Hà Nội", "Bắc", "HN"), ("Hải Phòng", "Bắc", "HP"), ("Quảng Ninh", "Bắc", "QN"), ("Bắc Ninh", "Bắc", "BN"),
    ("Nam Định", "Bắc", "ND"), ("Thái Nguyên", "Bắc", "TN"), ("Thanh Hóa", "Trung", "TH"), ("Nghệ An", "Trung", "NA"),
    ("Huế", "Trung", "HU"), ("Đà Nẵng", "Trung", "DN"), ("Quảng Nam", "Trung", "QM"), ("Khánh Hòa", "Trung", "KH"),
    ("TP. Hồ Chí Minh", "Nam", "SG"), ("Cần Thơ", "Nam", "CT"), ("Đồng Nai", "Nam", "DO"), ("Bình Dương", "Nam", "BD"),
    ("Vũng Tàu", "Nam", "VT"), ("Long An", "Nam", "LA"),
]
FAMILY = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô"]
MIDDLE = ["Văn", "Thị", "Hữu", "Minh", "Thanh", "Quốc", "Ngọc", "Đức", "Thu", "Gia"]
GIVEN = ["An", "Bình", "Châu", "Dũng", "Giang", "Hà", "Hải", "Hùng", "Khánh", "Lan", "Linh", "Long", "Mai", "Nam",
         "Ngân", "Phong", "Phương", "Quân", "Sơn", "Tâm", "Thảo", "Trang", "Tú", "Tuấn", "Vy", "Yến"]
DEPARTMENTS = ["Bán hàng", "Kho vận", "Kế toán", "Chăm sóc khách hàng"]
PRODUCTS = [("Nồi cơm điện", "Gia dụng"), ("Quạt đứng", "Gia dụng"), ("Máy xay sinh tố", "Gia dụng"),
            ("Ấm siêu tốc", "Gia dụng"), ("Tivi 43 inch", "Điện tử"), ("Loa bluetooth", "Điện tử"),
            ("Tai nghe", "Điện tử"), ("Bàn ủi hơi nước", "Gia dụng"), ("Máy lọc nước", "Điện máy"),
            ("Tủ lạnh 180L", "Điện máy"), ("Máy giặt 8kg", "Điện máy"), ("Điều hòa 1HP", "Điện máy")]
SEASON = [0.92, 0.85, 0.95, 0.97, 1.0, 1.03, 1.08, 1.05, 1.0, 1.02, 1.07, 1.25]


@dataclass
class World:
    year: int
    branches: list[dict]
    products: list[dict]
    revenue: dict[int, list[dict]]          # month -> rows {ma_cn, doanh_thu, so_don, chi_phi}
    employees: list[dict]
    targets: list[dict]                     # {ma_cn, muc_tieu}
    meta: dict = field(default_factory=dict)

    def branch(self, code: str) -> dict:
        return next(b for b in self.branches if b["ma_cn"] == code)


def make_world(seed: int, year: int = 2026) -> World:
    rng = random.Random(seed)
    cities = rng.sample(CITIES, rng.randint(8, 14))
    cities.sort(key=lambda c: ("Bắc", "Trung", "Nam").index(c[1]))
    branches = []
    for i, (city, region, code) in enumerate(cities, start=1):
        branches.append({"ma_cn": f"{code}{rng.randint(1, 3):02d}", "ten_cn": f"CN {city}", "mien": region,
                         "thanh_pho": city, "_base": rng.uniform(600, 2400)})
    products = []
    for i, (name, group) in enumerate(rng.sample(PRODUCTS, rng.randint(6, 10)), start=1):
        products.append({"ma_sp": f"SP{i:03d}", "ten_sp": name, "nhom": group,
                         "don_gia": round(rng.uniform(250, 15000) / 10) * 10})  # nghìn đồng
    revenue = {}
    for m in range(1, 13):
        rows = []
        for b in branches:
            dt_ = round(b["_base"] * SEASON[m - 1] * rng.uniform(0.85, 1.15), 1)
            rows.append({"ma_cn": b["ma_cn"], "doanh_thu": dt_, "so_don": int(dt_ * rng.uniform(0.8, 1.6)),
                         "chi_phi": round(dt_ * rng.uniform(0.55, 0.8), 1)})
        revenue[m] = rows
    employees, used = [], set()
    n = 0
    for b in branches:
        for _ in range(rng.randint(2, 5)):
            while True:
                name = f"{rng.choice(FAMILY)} {rng.choice(MIDDLE)} {rng.choice(GIVEN)}"
                if name not in used:
                    used.add(name)
                    break
            n += 1
            start = dt.date(rng.randint(2015, 2025), rng.randint(1, 12), rng.randint(1, 28))
            employees.append({"ma_nv": f"NV{n:04d}", "ho_ten": name, "ma_cn": b["ma_cn"],
                              "phong_ban": rng.choice(DEPARTMENTS), "luong": round(rng.uniform(8, 45), 1),
                              "ngay_vao_lam": start})
    targets = [{"ma_cn": b["ma_cn"], "muc_tieu": round(b["_base"] * 12 * rng.uniform(0.95, 1.2), -1)} for b in branches]
    for b in branches:
        del b["_base"]
    return World(year, branches, products, revenue, employees, targets, {"seed": seed})
