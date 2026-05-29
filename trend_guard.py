"""
trend_guard.py — value-trap / momentum-context guard for Sentinel.

PROBLEM THIS SOLVES
-------------------
The valuation engine measures whether a stock is CHEAP (price below fair value).
It does NOT measure WHY it is cheap. A stock 38% below its high can be either:
  - temporarily out of favour (a contrarian BUY), or
  - structurally re-rating downward (a value trap / falling knife).
The engine treats both identically. This module adds the missing context.

DESIGN (option C: flag, don't suppress)
---------------------------------------
We never delete a BUY signal. We attach a trend tag so Guga decides:
  - VALUE-TRAP-RISK : cheap AND still in a downtrend (price < both 50d and 200d MA)
  - BASING          : cheap but reclaiming the 50d MA (the good kind of cheap)
  - UPTREND         : price above both MAs
  - NEUTRAL         : mixed / insufficient data

DATA SOURCE
-----------
Uses yfinance `info` fields `fiftyDayAverage` and `twoHundredDayAverage`,
which are ALREADY fetched by yahoo.py's `t.info` call. Zero extra API calls.
If those fields are missing (some ETFs / thin names), we fall back to a
1y history pull ONLY for that ticker, and degrade gracefully to NEUTRAL if
even that fails. Never raises — a guard that crashes the brief is worse than
no guard.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrendContext:
    ticker: str
    price: float
    ma50: Optional[float] = None
    ma200: Optional[float] = None
    tag: str = "NEUTRAL"          # VALUE-TRAP-RISK | BASING | UPTREND | NEUTRAL
    pct_vs_50: Optional[float] = None
    pct_vs_200: Optional[float] = None
    note: str = ""

    def label(self) -> str:
        """One-line human-readable summary for the brief."""
        if self.tag == "NEUTRAL":
            return f"{self.ticker}: trend NEUTRAL ({self.note})" if self.note \
                else f"{self.ticker}: trend NEUTRAL (insufficient MA data)"
        bits = []
        if self.pct_vs_50 is not None:
            bits.append(f"{self.pct_vs_50:+.0%} vs 50d")
        if self.pct_vs_200 is not None:
            bits.append(f"{self.pct_vs_200:+.0%} vs 200d")
        ctx = ", ".join(bits)
        return f"{self.ticker}: {self.tag} ({ctx})"


def _extract_mas_from_info(info: dict) -> tuple[Optional[float], Optional[float]]:
    """Pull 50d and 200d moving averages from a yfinance info dict."""
    def g(k):
        v = info.get(k)
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
        return v if (v and v > 0) else None
    return g("fiftyDayAverage"), g("twoHundredDayAverage")


def _mas_from_history(ticker: str) -> tuple[Optional[float], Optional[float]]:
    """Fallback: compute MAs from 1y daily history. Only called if info lacks them."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
        if hist is None or hist.empty or "Close" not in hist:
            return None, None
        closes = hist["Close"].dropna()
        ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
        ma200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None
        return ma50, ma200
    except Exception:
        return None, None


def classify(ticker: str, price: float, info: Optional[dict] = None,
             allow_history_fallback: bool = True) -> TrendContext:
    """
    Build a TrendContext for a ticker.

    Parameters
    ----------
    ticker : str
    price  : float                current price (from the engine's Fundamentals)
    info   : dict | None          the yfinance info dict, if already on hand
    allow_history_fallback : bool if info lacks MAs, do one history pull

    Never raises. Returns NEUTRAL on any data gap.
    """
    ctx = TrendContext(ticker=ticker, price=price)

    if not price or price <= 0:
        ctx.note = "no price"
        return ctx

    ma50 = ma200 = None
    if info:
        ma50, ma200 = _extract_mas_from_info(info)

    if (ma50 is None or ma200 is None) and allow_history_fallback:
        h50, h200 = _mas_from_history(ticker)
        ma50 = ma50 if ma50 is not None else h50
        ma200 = ma200 if ma200 is not None else h200

    ctx.ma50, ctx.ma200 = ma50, ma200
    if ma50:
        ctx.pct_vs_50 = price / ma50 - 1.0
    if ma200:
        ctx.pct_vs_200 = price / ma200 - 1.0

    # Need at least the 200d to make a real call.
    if ma200 is None:
        ctx.note = "no 200d MA"
        ctx.tag = "NEUTRAL"
        return ctx

    below_200 = price < ma200
    below_50 = (ma50 is not None) and (price < ma50)
    above_50 = (ma50 is not None) and (price >= ma50)

    # Severity matters: 1% below the 200d is noise; 20% below is a real downtrend.
    # Only call it a TRAP when the stock is materially below the long-term trend.
    DEEP = -0.10   # >10% below the 200d MA = materially broken, not noise
    deep_below_200 = ctx.pct_vs_200 is not None and ctx.pct_vs_200 <= DEEP

    if below_200 and below_50:
        # Cheap AND no recovery base yet — falling-knife pattern.
        # Grade it: HARD trap only if materially below the long trend.
        ctx.tag = "VALUE-TRAP-RISK" if deep_below_200 else "WEAK-TREND"
    elif below_200 and above_50:
        # Below long trend but reclaiming short trend — basing/recovering.
        ctx.tag = "BASING"
    elif not below_200:
        ctx.tag = "UPTREND"
    else:
        ctx.tag = "NEUTRAL"

    return ctx


def trap_caveat(ctx: TrendContext) -> str:
    """Return a sentence to append to a BUY's caveat, or '' if none warranted."""
    if ctx.tag == "VALUE-TRAP-RISK":
        return (f"⚠ VALUE-TRAP-RISK: {ctx.ticker} is {ctx.pct_vs_200:+.0%} vs its 200d MA "
                f"and below its 50d — materially broken trend. The 'achievable growth' "
                f"assumption is exactly what the market is discounting. "
                f"Confirm the thesis before catching the knife.")
    if ctx.tag == "WEAK-TREND":
        return (f"• WEAK-TREND: {ctx.ticker} is modestly below its MAs "
                f"({ctx.pct_vs_200:+.0%} vs 200d) — soft, not a broken trend. "
                f"Watch for a 50d reclaim to confirm.")
    if ctx.tag == "BASING":
        return (f"✓ BASING: {ctx.ticker} is below its 200d MA but has reclaimed its "
                f"50d MA — the constructive kind of cheap (recovering, not still falling).")
    return ""
