"""SheetIR: Excel workbook -> layered, queryable intermediate representation.

Layers:
    L0 raw        cells, constants, formulas, cached values      (loader)
    L1 formula    tokens, normalized references, dependency edges (formula, refs)
    L2 structure  R1C1 formula groups, tables, cell roles         (groups, structure)
    L3 semantic   names for tables/fields/params                  (structure; LLM hook later)
"""

__version__ = "0.1.0"

from .build import build_ir  # noqa: E402  (build reads __version__)

__all__ = ["build_ir"]
