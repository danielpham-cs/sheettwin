"""Join keys between catalog objects (design §6.5.2).

For a pair of columns (A.x, B.y) in different objects, B.y being key-like (nearly one row per value):

    containment   share of A.x's distinct values found in B.y (values compared unaccented, trimmed, lowercase)
    key_like      distinct values / rows of B.y
    name_sim      1 for the same name, 0.9 when one abbreviates the other (ma_cn ~ ma_chi_nhanh) or they are synonyms
                  (branch_id ~ ma_chi_nhanh), else the share of shared words
    formula       a lookup in the workbook (VLOOKUP, INDEX/MATCH, SUMIF…) already joins the two columns

score = 0.55·containment + 0.2·key_like + 0.25·name_sim, at least 0.9 with formula evidence. A score of at least
relations.accept_score is accepted; between relations.llm_confirm_low and that, the language model is asked when one is
available; below, the pair is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from unidecode import unidecode

from .models import Relation

_SYNONYMS = [
    {"branch", "chi_nhanh", "cn"}, {"product", "san_pham", "sp", "item"}, {"employee", "nhan_vien", "nv", "staff"},
    {"customer", "khach_hang", "kh", "client"}, {"supplier", "nha_cung_cap", "ncc", "vendor"},
    {"id", "ma", "code", "key", "so"}, {"name", "ten"}, {"region", "mien", "vung", "khu_vuc"},
    {"department", "phong_ban", "pb", "dept"}, {"store", "cua_hang", "ch", "shop"}, {"city", "thanh_pho", "tinh"},
    {"category", "nhom", "loai", "group"},
]
_CANON = {w: min(g) for g in _SYNONYMS for w in g}


@dataclass
class ColumnInfo:
    obj: str            # sql name of the table or view
    name: str
    dtype: str
    n_rows: int
    values: set[str]    # normalized distinct values (a sample of at most relations.sample_distinct)
    distinct: int


def norm_value(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return unidecode(str(v)).strip().lower()


def _tokens(name: str) -> list[str]:
    return [t for t in name.split("_") if t]


def _canon_tokens(name: str) -> list[str]:
    toks = _tokens(name)
    out, i = [], 0
    while i < len(toks):
        two = "_".join(toks[i:i + 2]) if i + 1 < len(toks) else None
        if two in _CANON:
            out.append(_CANON[two])
            i += 2
        else:
            out.append(_CANON.get(toks[i], toks[i]))
            i += 1
    return out


def _abbreviates(short: list[str], long: list[str]) -> bool:
    """Whether each token of `short` is a whole token of `long` or the initials of a run of them, in order and
    covering all of `long`: ['ma', 'cn'] ~ ['ma', 'chi', 'nhanh']."""
    if not short:
        return not long
    if not long:
        return False
    head = short[0]
    for k in range(1, len(long) + 1):
        run = long[:k]
        if (k == 1 and run[0] == head) or (k > 1 and "".join(w[0] for w in run) == head):
            if _abbreviates(short[1:], long[k:]):
                return True
    return False


def name_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    if _abbreviates(ta, tb) or _abbreviates(tb, ta):
        return 0.9
    ca, cb = _canon_tokens(a), _canon_tokens(b)
    if ca == cb:
        return 0.9
    sa, sb = set(ca), set(cb)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def column_infos(con, objects: list[tuple[str, list[tuple[str, str]], int]], sample: int) -> list[ColumnInfo]:
    """Distinct values of every text or integer column of every object. `objects`: (sql name, [(column, dtype)],
    rows). `con` is a DuckDB connection."""
    out = []
    for obj, cols, n_rows in objects:
        for name, dtype in cols:
            if dtype not in ("text", "int") or name in ("_row", "period", "part", "_source_sheet"):
                continue
            rows = con.execute(f'SELECT DISTINCT "{name}" FROM "{obj}" WHERE "{name}" IS NOT NULL LIMIT {int(sample)}'
                               ).fetchall()
            distinct = con.execute(f'SELECT COUNT(DISTINCT "{name}") FROM "{obj}"').fetchone()[0]
            vals = {norm_value(r[0]) for r in rows if str(r[0]).strip()}
            if len(vals) >= 2:
                out.append(ColumnInfo(obj, name, dtype, n_rows, vals, distinct))
    return out


def score_pair(a: ColumnInfo, b: ColumnInfo, formula: bool = False) -> tuple[float, float, float, float]:
    """(score, containment, key_like, name_sim) of A.x referencing B.y."""
    containment = len(a.values & b.values) / len(a.values) if a.values else 0.0
    key_like = min(1.0, b.distinct / b.n_rows) if b.n_rows else 0.0
    ns = name_sim(a.name, b.name)
    score = 0.55 * containment + 0.2 * key_like + 0.25 * ns
    if formula:
        score = max(score, 0.9)
    return round(score, 3), containment, key_like, ns


def find_relations(infos: list[ColumnInfo], low: float, accept: float, formula_pairs: set = frozenset(),
                   confirm=None) -> list[Relation]:
    """Accepted relations, best first; at most one per pair of objects and direction. `formula_pairs` holds
    ((obj, col), (obj, col)) pairs a workbook lookup joins; `confirm(a, b, containment)` asks a model about a
    borderline pair and returns True/False (None: no model, the pair is dropped)."""
    found: dict[tuple[str, str], Relation] = {}
    for a in infos:
        for b in infos:
            if a.obj == b.obj or a.dtype != b.dtype:
                continue
            formula = ((a.obj, a.name), (b.obj, b.name)) in formula_pairs
            if b.distinct < 0.8 * b.n_rows and not formula:
                continue  # B is no key
            if a.distinct == a.n_rows and a.n_rows > b.n_rows and not formula:
                continue  # A is a key over more rows than B has: B cannot be its dimension
            score, cont, _, _ = score_pair(a, b, formula)
            if cont < 0.5 and not formula:
                continue
            if score < low:
                continue
            by = "rule"
            if score < accept:
                if confirm is None or not confirm(a, b, cont):
                    continue
                by = "llm"
            kind = "formula_ref" if formula else "fk_candidate"
            rel = Relation(left=f"{a.obj}.{a.name}", right=f"{b.obj}.{b.name}", kind=kind, score=score, confirmed_by=by)
            key = (a.obj, b.obj)
            if key not in found or found[key].score < score:
                found[key] = rel
    # a pair found both ways (two keys of the same values): keep the direction with the higher score
    out = []
    for (x, y), r in found.items():
        back = found.get((y, x))
        if back is not None and (back.score > r.score or (back.score == r.score and y < x)):
            continue
        out.append(r)
    return sorted(out, key=lambda r: -r.score)
