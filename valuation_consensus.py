"""
valuation_consensus.py — three-method valuation consensus for Sentinel.

WHY
---
The engine rested on ONE method (reverse-DCF edge). One bad yfinance FCF number
poisons the whole verdict, with nothing to catch it. Both reference projects
(asafravid/sss, xang1234) triangulate across multiple fundamental dimensions.
This adds that: three INDEPENDENT cheap/expensive reads, combined by CONSENSUS.

THE THREE METHODS (all from data already in the yfinance info dict)
-------------------------------------------------------------------
M1. Reverse-DCF edge  : achievable_growth - implied_growth   (the existing one)
M2. PEG               : forward P/E divided by achievable growth%. <1 cheap,
                        >2 expensive. Classic Lynch screen.
M3. EV/EBITDA vs norm : current EV/EBITDA against a sane sector-agnostic norm.
                        Below norm = cheap, above = expensive.

CONSENSUS RULE (Guga's choice)
------------------------------
Each method votes CHEAP / NEUTRAL / EXPENSIVE.
  - 2 of 3 CHEAP  -> BUY        (real margin of safety: >1 method agrees)
  - 3 of 3 CHEAP  -> BUY + CONVICTION tag (premium signal)
  - else          -> not a consensus BUY (FAIR/WAIT per the edge sign)
A single method calling something cheap is treated as noise, not signal —
the same logic that justified the 8% edge gate.

Never raises. Any method lacking data abstains (votes None), and abstention
is handled: 2/3 means 2 of the methods that COULD vote.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# EV/EBITDA norm: broad-market mature-company midpoint. Below = cheap-ish.
# Deliberately conservative; this is a triangulation input, not a precise target.
EV_EBITDA_CHEAP = 12.0     # at/below this -> CHEAP vote
EV_EBITDA_RICH = 20.0      # at/above this -> EXPENSIVE vote
PEG_CHEAP = 1.0            # at/below -> CHEAP
PEG_RICH = 2.0             # at/above -> EXPENSIVE

# --- FIX #2: sector-relative EV/EBITDA + de-correlated PEG ---
# CHEAP = below 0.8x sector median, EXPENSIVE = above 1.25x. Fixes the bias where
# fixed 12x/20x cutoffs voted EXPENSIVE on every quality compounder and CHEAP on
# every structurally-cheap value trap.
SECTOR_EV_EBITDA = {
    "SOFTWARE": 22.0, "PAYMENTS": 18.0, "HEALTHCARE": 12.0,
    "SEMICONDUCTORS": 16.0, "DEFAULT": 13.0,
}
SECTOR = {
    "AMD": "SEMICONDUCTORS", "META": "SOFTWARE", "AMZN": "SOFTWARE", "ORCL": "SOFTWARE",
    "CRM": "SOFTWARE", "ASML": "SEMICONDUCTORS", "V": "PAYMENTS", "MA": "PAYMENTS",
    "COST": "DEFAULT", "LLY": "HEALTHCARE", "NVO": "HEALTHCARE", "TSM": "SEMICONDUCTORS",
    "ADBE": "SOFTWARE", "NU": "PAYMENTS", "CLBT": "SOFTWARE", "OPRA": "SOFTWARE",
    "CRWD": "SOFTWARE", "NOW": "SOFTWARE", "INTU": "SOFTWARE", "ISRG": "HEALTHCARE",
    "NVDA": "SEMICONDUCTORS", "PLTR": "SOFTWARE", "SPOT": "SOFTWARE", "NFLX": "SOFTWARE",
    "MSFT": "SOFTWARE", "UBER": "SOFTWARE", "CI": "HEALTHCARE", "BMY": "HEALTHCARE",
    "MRK": "HEALTHCARE", "CVS": "HEALTHCARE", "GOOGL": "SOFTWARE", "QQQ": "DEFAULT",
    "MU": "SEMICONDUCTORS", "AVGO": "SEMICONDUCTORS",
}
def _sector_of(ticker):
    return SECTOR.get((ticker or "").upper(), "DEFAULT")


@dataclass
class MethodVote:
    name: str
    vote: Optional[str] = None     # "CHEAP" | "NEUTRAL" | "EXPENSIVE" | None(abstain)
    detail: str = ""


@dataclass
class ConsensusResult:
    votes: list = field(default_factory=list)
    cheap_count: int = 0
    voting_count: int = 0          # methods that did NOT abstain
    is_buy: bool = False
    is_conviction: bool = False    # 3/3 cheap
    summary: str = ""


def _vote_dcf(edge: Optional[float], buy_edge: float) -> MethodVote:
    if edge is None:
        return MethodVote("DCF", None, "no FCF — abstain")
    if edge >= buy_edge:
        return MethodVote("DCF", "CHEAP", f"edge {edge:+.1%} >= {buy_edge:.0%}")
    if edge <= -0.05:
        return MethodVote("DCF", "EXPENSIVE", f"edge {edge:+.1%}")
    return MethodVote("DCF", "NEUTRAL", f"edge {edge:+.1%}")


def _vote_peg(forward_pe: Optional[float], rev_growth: Optional[float]) -> MethodVote:
    # FIX #2: PEG now uses REV_GROWTH (reported revenue growth), NOT achievable_growth.
    # M1 (DCF) uses achievable_growth; using a different growth input here de-correlates
    # the two methods so a single bad growth estimate can't flip both votes at once.
    if not forward_pe or forward_pe <= 0 or rev_growth is None or rev_growth <= 0:
        return MethodVote("PEG", None, "no fwd P/E or non-positive rev-growth — abstain")
    peg = forward_pe / (rev_growth * 100.0)
    if peg <= PEG_CHEAP:
        return MethodVote("PEG", "CHEAP", f"PEG(rev) {peg:.2f} <= {PEG_CHEAP}")
    if peg >= PEG_RICH:
        return MethodVote("PEG", "EXPENSIVE", f"PEG(rev) {peg:.2f} >= {PEG_RICH}")
    return MethodVote("PEG", "NEUTRAL", f"PEG(rev) {peg:.2f}")


def _vote_ev_ebitda(enterprise_value: Optional[float], ebitda: Optional[float],
                    sector: str = "DEFAULT") -> MethodVote:
    # FIX #2: sector-relative bounds instead of fixed 12x/20x.
    if not enterprise_value or not ebitda or ebitda <= 0:
        return MethodVote("EV/EBITDA", None, "no EV or non-positive EBITDA — abstain")
    ratio = enterprise_value / ebitda
    median = SECTOR_EV_EBITDA.get(sector, SECTOR_EV_EBITDA["DEFAULT"])
    cheap_b, rich_b = median * 0.8, median * 1.25
    if ratio <= cheap_b:
        return MethodVote("EV/EBITDA", "CHEAP", f"{ratio:.1f}x <= {cheap_b:.1f} ({sector})")
    if ratio >= rich_b:
        return MethodVote("EV/EBITDA", "EXPENSIVE", f"{ratio:.1f}x >= {rich_b:.1f} ({sector})")
    return MethodVote("EV/EBITDA", "NEUTRAL", f"{ratio:.1f}x ({sector} med {median:.0f})")


def consensus(edge, buy_edge, forward_pe, rev_growth,
              enterprise_value, ebitda, ticker="") -> ConsensusResult:
    sector = _sector_of(ticker)
    votes = [
        _vote_dcf(edge, buy_edge),
        _vote_peg(forward_pe, rev_growth),
        _vote_ev_ebitda(enterprise_value, ebitda, sector),
    ]
    voting = [v for v in votes if v.vote is not None]
    cheap = [v for v in voting if v.vote == "CHEAP"]
    r = ConsensusResult(votes=votes, cheap_count=len(cheap),
                        voting_count=len(voting))
    # BUY needs >=2 cheap votes AND a majority of voting methods cheap.
    r.is_buy = len(cheap) >= 2
    r.is_conviction = (len(cheap) == 3 and r.voting_count == 3)
    tags = "/".join(f"{v.name}:{v.vote or 'abstain'}" for v in votes)
    r.summary = (f"consensus {len(cheap)}/{r.voting_count} cheap "
                 f"[{tags}]" + (" — CONVICTION" if r.is_conviction else ""))
    return r
