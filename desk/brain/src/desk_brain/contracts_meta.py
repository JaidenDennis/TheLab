"""Product metadata — mirror of desk-web/src/lib/ingest/contracts.ts.

Only the point-value map and root parsing are duplicated (static data);
trade reconstruction itself is NOT — that lives solely in desk-web.
"""

from __future__ import annotations

import re

POINT_VALUES: dict[str, float] = {
    "NQ": 20, "MNQ": 2, "ES": 50, "MES": 5, "YM": 5, "MYM": 0.5,
    "RTY": 50, "M2K": 5, "CL": 1000, "MCL": 100, "GC": 100, "MGC": 10,
}

_CONTRACT_RE = re.compile(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])\d{1,2}$")


def product_root(contract: str) -> str:
    code = contract.strip().upper()
    m = _CONTRACT_RE.match(code)
    if m:
        return m.group(1)
    alpha = re.match(r"^[A-Z]+", code)
    return alpha.group(0) if alpha else code


def point_value(product: str) -> float | None:
    return POINT_VALUES.get(product.upper())
