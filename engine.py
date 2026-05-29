#!/usr/bin/env python3
"""SmartMoney Sentinel — analytics core v3.

Deterministic, testable, no network/LLM. The daily script feeds live data in;
this decides. Claude only narrates the output.

v3 changes that improve recommendation quality:
  1. REAL ROIC accepted (NOPAT / invested capital) instead of ROE proxy.
  2. GROWTH-FADE by market cap + analyst cross-check + caps. Kills the
     CRM-style "past growth = future growth" false BUY.
  3. COMPOSITE SCORE (0-100) with HARD FLOORS instead of pure binary gates.
  4. RANKING + SIZING: turns a list of BUYs into "deploy here, this much".
  5. EARNINGS GUARD: never a fresh BUY into a coin-flip print.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

HURDLE = 0.10
TERMINAL_GROWTH = 0.025
DCF_YEARS = 10

FLOOR_ROIC = 0.10
FLOOR_FCF_MARGIN = 0.05
FLOOR_REV_GROWTH = 0.05
FLOOR_NET_DEBT_EBITDA = 3.5
REQUIRE_MOAT = True

GREAT_ROIC = 0.25
GREAT_GROSS_MARGIN = 0.60
GREAT_FCF_MARGIN = 0.25
GREAT_REV_GROWTH = 0.20

BUY_EDGE = 0.08   # was 0.04 — require a real margin of safety, not model noise
WAIT_EDGE = -0.01
_FRESH_EARNINGS_DAYS = 7   # window after a report during which we re-check the reaction


def _fresh_earnings(f) -> bool:
    """True if earnings were reported within the last _FRESH_EARNINGS_DAYS days."""
    d = getattr(f, "days_since_earnings", None)
    return d is not None and 0 <= d <= _FRESH_EARNINGS_DAYS

ATH_BUFFER = 0.05
NVDA_MAX_WEIGHT = 0.45
CORE_TICKERS = {"NVDA", "MSFT", "AVGO", "GOOGL"}


def growth_ceiling(market_cap: float) -> float:
    if market_cap >= 1_000e9:  return 0.12
    if market_cap >= 300e9:    return 0.16
    if market_cap >= 100e9:    return 0.20
    if market_cap >= 30e9:     return 0.28
    return 0.40


@dataclass
class Fundamentals:
    ticker: str
    price: float
    fcf_per_share: float
    roic: float
    roic_is_proxy: bool = False
    gross_margin: float = 0.0
    fcf_margin: float = 0.0
    rev_growth: float = 0.0
    net_debt_ebitda: float = 0.0
    ath: float = 0.0
    market_cap: float = 0.0
    analyst_growth: Optional[float] = None
    hist_fcf_cagr: Optional[float] = None
    moat: str = ""
    next_earnings_days: Optional[int] = None
    days_since_earnings: Optional[int] = None
    last_surprise_pct: Optional[float] = None
    post_earnings_drift: Optional[float] = None
    chg_90d: Optional[float] = None
    above_52w_low: Optional[float] = None
    is_etf: bool = False
    raw_info: dict = field(default_factory=dict)
    forward_pe: Optional[float] = None
    enterprise_value: Optional[float] = None
    ebitda_val: Optional[float] = None


@dataclass
class Verdict:
    ticker: str
    rejected: bool = False
    reject_reasons: list = field(default_factory=list)
    quality_score: float = 0.0
    implied_growth: Optional[float] = None
    achievable_growth: Optional[float] = None
    edge: Optional[float] = None
    action: str = "REJECT"
    final_score: float = 0.0
    near_ath: bool = False
    earnings_soon: bool = False
    post_earnings_flag: str = ""
    trend_context: str = ""
    confidence: str = "MEDIUM"
    consensus_summary: str = ""
    conviction: bool = False
    note: str = ""


def hard_floors(f: Fundamentals) -> list:
    fails = []
    if f.roic < FLOOR_ROIC:
        fails.append(f"ROIC {f.roic:.0%}<{FLOOR_ROIC:.0%}")
    if f.fcf_margin < FLOOR_FCF_MARGIN:
        fails.append(f"FCF-margin {f.fcf_margin:.0%}<{FLOOR_FCF_MARGIN:.0%}")
    if f.rev_growth < FLOOR_REV_GROWTH:
        fails.append(f"rev-growth {f.rev_growth:.0%}<{FLOOR_REV_GROWTH:.0%}")
    if f.net_debt_ebitda > FLOOR_NET_DEBT_EBITDA:
        fails.append(f"netDebt/EBITDA {f.net_debt_ebitda:.1f}>{FLOOR_NET_DEBT_EBITDA}")
    if REQUIRE_MOAT and not f.moat.strip():
        fails.append("no moat stated")
    return fails


def quality_score(f: Fundamentals) -> float:
    def ratio(val, great):
        return max(0.0, min(1.0, val / great)) if great else 0.0
    s = (35 * ratio(f.roic, GREAT_ROIC)
         + 20 * ratio(f.gross_margin, GREAT_GROSS_MARGIN)
         + 25 * ratio(f.fcf_margin, GREAT_FCF_MARGIN)
         + 20 * ratio(f.rev_growth, GREAT_REV_GROWTH))
    return round(s, 1)


def _dcf_value(fcf0, g, r=HURDLE, years=DCF_YEARS, gt=TERMINAL_GROWTH):
    pv, fcf = 0.0, fcf0
    for t in range(1, years + 1):
        fcf = fcf0 * (1 + g) ** t
        pv += fcf / (1 + r) ** t
    pv += fcf * (1 + gt) / (r - gt) / (1 + r) ** years
    return pv


def reverse_dcf_implied_growth(price, fcf0, r=HURDLE):
    if fcf0 <= 0 or price <= 0:
        return None
    lo, hi = -0.20, 0.60
    if _dcf_value(fcf0, lo, r) > price: return lo
    if _dcf_value(fcf0, hi, r) < price: return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if _dcf_value(fcf0, mid, r) < price: lo = mid
        else: hi = mid
    return (lo + hi) / 2


def achievable_growth(f: Fundamentals) -> Optional[float]:
    parts = []
    if f.analyst_growth is not None:
        parts.append((f.analyst_growth, 0.6))
    if f.hist_fcf_cagr is not None:
        parts.append((f.hist_fcf_cagr, 0.4))
    if not parts:
        base = f.rev_growth
    else:
        wsum = sum(w for _, w in parts)
        base = sum(v * w for v, w in parts) / wsum
    ceiling = growth_ceiling(f.market_cap) if f.market_cap else 0.20
    faded = min(base, ceiling)
    if f.rev_growth > 0:
        faded = min(faded, f.rev_growth * 1.3)
    return max(faded, -0.10)


import valuation_consensus as _vc


def evaluate(f: Fundamentals) -> Verdict:
    v = Verdict(ticker=f.ticker)
    v.near_ath = f.ath > 0 and f.price >= f.ath * (1 - ATH_BUFFER)
    v.earnings_soon = f.next_earnings_days is not None and 0 <= f.next_earnings_days <= 5

    fails = hard_floors(f)
    if fails:
        v.rejected = True
        v.reject_reasons = fails
        v.action = "REJECT"
        v.note = "Floor fail: " + ", ".join(fails)
        return v

    v.quality_score = quality_score(f)
    implied = reverse_dcf_implied_growth(f.price, f.fcf_per_share)
    achiev = achievable_growth(f)
    v.implied_growth = implied
    v.achievable_growth = achiev

    if implied is None:
        v.action = "WAIT"
        v.confidence = "LOW"
        v.note = "Negative/zero FCF — reverse-DCF n/a; quality OK but unvaluable."
        return v

    edge = achiev - implied
    v.edge = edge
    _con = _vc.consensus(edge, BUY_EDGE, f.forward_pe, f.rev_growth,
                         f.enterprise_value, f.ebitda_val, ticker=f.ticker)
    v.consensus_summary = _con.summary
    v.conviction = _con.is_conviction
    if _con.is_buy:         v.action = "BUY"
    elif edge <= WAIT_EDGE: v.action = "WAIT"
    else:                   v.action = "FAIR"

    have = sum(x is not None for x in (f.analyst_growth, f.hist_fcf_cagr))
    base_conf = "HIGH" if have == 2 else "MEDIUM" if have == 1 else "LOW"
    if f.roic_is_proxy and base_conf == "HIGH":
        base_conf = "MEDIUM"
    v.confidence = base_conf

    # Trend context: makes a slow falling-knife visible even when no rule fires.
    # A deeply negative 90d move sitting just above the 52w low = cheap because
    # something is wrong, not cheap-and-safe. Shown, not acted on (your call).
    _bits = []
    if f.chg_90d is not None:
        _bits.append(f"{f.chg_90d:+.0%}/90d")
    if f.above_52w_low is not None:
        _bits.append(f"{f.above_52w_low:+.0%} above 52w-low")
    if _bits:
        v.trend_context = " · ".join(_bits)
        _knife = (f.chg_90d is not None and f.chg_90d <= -0.20 and
                  f.above_52w_low is not None and f.above_52w_low <= 0.10)
        if _knife:
            v.trend_context += "  ⚠ falling-knife: deep decline near 52w-low — confirm thesis"

    if v.action == "BUY" and v.near_ath:
        v.action = "WAIT"
        v.note = "Value-buy zone but within 5% of ATH — wait for a dip."
    elif v.action == "BUY" and v.earnings_soon:
        v.action = "WAIT"
        v.note = f"BUY on value, but earnings in {f.next_earnings_days}d — wait for the print."
    elif v.action == "BUY" and _fresh_earnings(f):
        # Earnings landed in the last 7 days. The valuation data the engine just
        # used may pre-date the print. Read the market's reaction before trusting
        # the "cheap = buy" signal. This is a flag for the human, not an analysis
        # of the guidance (which is text we deliberately do not try to parse).
        s = f.last_surprise_pct
        d = f.post_earnings_drift
        if d is not None and d <= -0.05 and (s is None or s >= 0):
            # beat (or unknown) BUT the market sold it off — the CRM pattern
            v.action = "WATCH"
            v.post_earnings_flag = "BEAT-BUT-SOLD"
            v.note = (f"Cheap, but earnings {f.days_since_earnings}d ago and the "
                      f"market sold it {d:+.0%} despite "
                      f"{'a beat' if s and s>0 else 'the print'}. "
                      f"Verify guidance before buying — engine can't read it.")
        elif s is not None and s < 0:
            # outright EPS miss
            v.action = "WATCH"
            v.post_earnings_flag = "MISS"
            v.note = (f"Cheap, but missed earnings {f.days_since_earnings}d ago "
                      f"(surprise {s:+.0%}). Thesis check before buying.")
        else:
            # earnings just passed, reaction not negative — let BUY stand, but note it
            v.post_earnings_flag = "FRESH-OK"
            proxy = " [ROE proxy]" if f.roic_is_proxy else ""
            v.note = (f"implied {implied:+.1%} vs achievable {achiev:+.1%}{proxy}. "
                      f"Earnings {f.days_since_earnings}d ago, reaction not negative.")
    else:
        proxy = " [ROE proxy]" if f.roic_is_proxy else ""
        v.note = (f"implied {implied:+.1%} vs achievable {achiev:+.1%} "
                  f"-> edge {edge:+.1%}; quality {v.quality_score:.0f}/100{proxy}")

    conf_mult = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}[v.confidence]
    if v.action == "BUY" and edge is not None:
        v.final_score = round(edge * 100 * conf_mult * (v.quality_score / 100), 1)
    return v


def rank_buys(verdicts: list) -> list:
    buys = [v for v in verdicts if v.action == "BUY"]
    return sorted(buys, key=lambda v: v.final_score, reverse=True)


def suggest_size(v: Verdict, monthly_budget: float) -> float:
    if v.final_score >= 15:   frac = 1.0
    elif v.final_score >= 8:  frac = 0.6
    elif v.final_score >= 4:  frac = 0.35
    else:                     frac = 0.2
    return round(monthly_budget * frac, 0)


def position_health(f: Fundamentals, weight: float) -> dict:
    fails = [] if f.is_etf else hard_floors(f)
    flags = []
    if fails:
        flags.append("THESIS-WATCH: " + ", ".join(fails))
    if f.ticker == "NVDA":
        cap = NVDA_MAX_WEIGHT
    elif f.ticker in CORE_TICKERS:
        cap = 1.0
    else:
        cap = 0.20
    if weight > cap:
        flags.append(f"CONCENTRATION: {weight:.0%}>{cap:.0%} cap")
    return {"ticker": f.ticker, "quality_ok": not fails,
            "weight": weight, "flags": flags}
