"""An exact bootstrap through FIA, one bond per maturity bucket.

``FIA.curve.build_from_bonds`` adds one knot per bond at the bond's maturity, solving for the
zero rate that reprices it exactly, and interpolates piecewise linearly. Two bonds with the same
maturity would put two knots at one abscissa and divide by zero, and two a few days apart make
the forward curve whipsaw between them. So the maturity axis is cut into buckets and each
bucket contributes one bond:

- under a year, bills only: they are zero-coupon, so each knot is a discount factor read
  straight off a price;
- from a year out, fixed-coupon notes and bonds, never callable, preferring off-the-run issues
  (rank 2 or more, or unranked) to the on-the-run and first off-the-run, whose prices carry a
  liquidity premium; within a preference, the longest maturity in the bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import FIA

_SHORT_EDGES = [7 / 365.25, 1 / 12, 2 / 12, 3 / 12, 4.5 / 12, 6 / 12, 9 / 12, 1.0]
_LONG_EDGES = [1.0 + 0.5 * i for i in range(1, 19)] + [float(y) for y in range(11, 31)] + [31.0]
BUCKET_EDGES: Sequence[float] = tuple(_SHORT_EDGES + _LONG_EDGES)


@dataclass
class Candidate:
    cusip: str
    kind: str
    maturity_years: float
    run_rank: Optional[int]
    callable: bool
    bond: "FIA.relative_bond"


def _preference(c: Candidate) -> tuple:
    off_the_run = c.run_rank is None or c.run_rank >= 2
    return (0 if off_the_run else 1, -c.maturity_years, c.cusip)


def select(candidates: List[Candidate], edges: Sequence[float] = BUCKET_EDGES) -> List[Candidate]:
    """One candidate per bucket ``[edges[i], edges[i+1])``, in maturity order."""
    chosen: Dict[int, Candidate] = {}
    for c in candidates:
        if c.callable or c.bond.get_price() is None:
            continue
        t = c.maturity_years
        if t < edges[0] or t >= edges[-1]:
            continue
        short = t < 1.0
        if short != (c.kind == "bill") or c.kind not in ("bill", "note", "bond"):
            continue
        i = max(j for j in range(len(edges) - 1) if edges[j] <= t)
        cur = chosen.get(i)
        if cur is None or _preference(c) < _preference(cur):
            chosen[i] = c
    picked = [chosen[i] for i in sorted(chosen)]
    maturities = [c.maturity_years for c in picked]
    if len(set(maturities)) != len(maturities):
        raise ValueError("two bootstrap bonds share a maturity")
    return picked


def build(picked: List[Candidate]) -> "FIA.curve":
    """FIA's exact bootstrap over the chosen bonds, unchanged."""
    return FIA.curve_factory(bondlist=[c.bond for c in picked])
