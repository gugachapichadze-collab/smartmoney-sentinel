#!/usr/bin/env python3
"""Yahoo Finance data layer v3 for SmartMoney Sentinel.

No API key, no symbol restrictions, free. Supplies engine v3's richer inputs:
real ROIC (computed from financials, ROE only as flagged fallback), market cap,
forward analyst growth, next-earnings distance, plus a sanity guard so garbage
data never reaches the engine.
"""
from __future__ import annotations
from typing import Optional
import datetime

import yfinance as yf

from engine import Fundamentals


def _safe(d, key, default=None):
    v = d.get(key, default)
    return v if v is not None else default


def _real_roic(t) -> Optional[float]:
    """NOPAT / (total debt + equity - cash). Leverage-blind, unlike ROE.
    Returns None if statements incomplete -> caller falls back to ROE."""
    try:
        fin = t.financials
        bs = t.balance_sheet
        if fin is None or fin.empty or bs is None or bs.empty:
            return None
        def pick(df, names):
            for n in names:
                if n in df.index:
                    val = df.loc[n].iloc[0]
                    if val is not None:
                        return float(val)
            return None
        ebit = pick(fin, ["EBIT", "Operating Income", "OperatingIncome"])
        tax_rate = 0.21
        pretax = pick(fin, ["Pretax Income", "PretaxIncome"])
        tax = pick(fin, ["Tax Provision", "TaxProvision", "Income Tax Expense"])
        if ebit is None:
            return None
        if pretax and tax and pretax != 0:
            tax_rate = max(0.0, min(0.40, tax / pretax))
        nopat = ebit * (1 - tax_rate)
        debt = pick(bs, ["Total Debt", "TotalDebt"]) or 0.0
        equity = pick(bs, ["Stockholders Equity", "Total Stockholder Equity",
                           "StockholdersEquity", "Common Stock Equity"]) or 0.0
        cash = pick(bs, ["Cash And Cash Equivalents", "CashAndCashEquivalents",
                         "Cash Cash Equivalents And Short Term Investments"]) or 0.0
        invested = debt + equity - cash
        if invested <= 0:
            return None
        return nopat / invested
    except Exception:
        return None


def _hist_fcf_cagr(t) -> Optional[float]:
    try:
        cf = t.cashflow
        if cf is None or cf.empty:
            return None
        row = None
        for label in ("Free Cash Flow", "FreeCashFlow"):
            if label in cf.index:
                row = cf.loc[label]; break
        if row is None:
            return None
        vals = [float(x) for x in row.values if x is not None and float(x) > 0]
        if len(vals) < 3:
            return None
        years = len(vals) - 1
        if vals[-1] <= 0:
            return None
        return (vals[0] / vals[-1]) ** (1 / years) - 1
    except Exception:
        return None


def _analyst_growth(t, info) -> Optional[float]:
    """Forward growth estimate. Prefer earnings growth, fall back to revenue."""
    g = _safe(info, "earningsGrowth") or _safe(info, "revenueGrowth")
    if g is not None:
        try:
            g = float(g)
            if -0.5 < g < 1.5:
                return g
        except Exception:
            pass
    return None


def _next_earnings_days(t) -> Optional[int]:
    try:
        cal = t.calendar
        d = None
        if isinstance(cal, dict):
            ev = cal.get("Earnings Date")
            if ev:
                d = ev[0] if isinstance(ev, (list, tuple)) else ev
        if d is None:
            return None
        if hasattr(d, "date"):
            d = d.date()
        return (d - datetime.date.today()).days
    except Exception:
        return None


def _post_earnings_context(t, price):
    """Return (days_since_earnings, last_surprise_pct, post_earnings_drift).

    Uses t.earnings_dates, which carries the ACTUAL announcement timestamp
    (not the fiscal quarter-end). days_since is measured from the most recent
    PAST announcement; surprise is its EPS surprise as a fraction; drift is
    price now vs the close just before that announcement.
    """
    import datetime as _dt
    import pandas as _pd
    days_since = surprise = drift = None
    last_dt = None
    try:
        ed = t.earnings_dates
        if ed is not None and len(ed):
            today = _pd.Timestamp.now(tz=ed.index.tz) if ed.index.tz else _pd.Timestamp.now()
            past = ed[ed.index <= today]
            if len(past):
                last_dt = past.index.max()
                days_since = (today - last_dt).days
                sp = past.loc[last_dt, "Surprise(%)"]
                if sp is not None and not _pd.isna(sp):
                    surprise = float(sp) / 100.0   # 24.07 -> 0.2407
    except Exception:
        pass
    # price reaction: close just before the announcement vs now
    try:
        if last_dt is not None and price:
            start = (last_dt - _pd.Timedelta(days=5)).strftime("%Y-%m-%d")
            hist = t.history(start=start)
            if hist is not None and len(hist):
                pre = float(hist["Close"].iloc[0])
                if pre:
                    drift = price / pre - 1.0
    except Exception:
        pass
    return days_since, surprise, drift

def _trend_context(t, price, info):
    """Return (chg_90d, above_52w_low) as fractions. Best-effort; None on failure.

    chg_90d        : price now vs ~90 calendar days ago (-0.34 = down 34%)
    above_52w_low  : how far above the 52-week low (0.05 = 5% above the low)
    A deeply negative chg_90d sitting just above the 52w low is the classic
    falling-knife signature — cheap because something is wrong, not cheap-and-safe.
    """
    import pandas as _pd
    chg90 = above_low = None
    try:
        hist = t.history(period="3mo")
        if hist is not None and len(hist):
            old = float(hist["Close"].iloc[0])
            if old:
                chg90 = price / old - 1.0
    except Exception:
        pass
    try:
        low52 = info.get("fiftyTwoWeekLow")
        if low52 and float(low52) > 0:
            above_low = price / float(low52) - 1.0
    except Exception:
        pass
    return chg90, above_low


def fetch(ticker: str, api_key: str = "", ath: float = 0.0,
          moat: str = "", is_etf: bool = False) -> Fundamentals:
    t = yf.Ticker(ticker)
    info = t.info or {}

    price = _safe(info, "currentPrice") or _safe(info, "regularMarketPrice") or 0.0
    price = float(price or 0)
    if price <= 0:
        raise ValueError(f"{ticker}: no valid price (got {price})")

    if is_etf:
        return Fundamentals(ticker=ticker, price=price, fcf_per_share=0.0,
                            roic=0.0, gross_margin=0.0, fcf_margin=0.0,
                            rev_growth=0.0, net_debt_ebitda=0.0,
                            ath=float(ath or _safe(info,"fiftyTwoWeekHigh",0) or price),
                            market_cap=float(_safe(info,"marketCap",0) or 0),
                            moat=moat, is_etf=True, raw_info=info)

    shares = _safe(info, "sharesOutstanding", 0) or 0
    fcf_total = _safe(info, "freeCashflow", 0) or 0
    revenue = _safe(info, "totalRevenue", 0) or 0
    fcf_ps = (fcf_total / shares) if shares else 0.0
    fcf_margin = (fcf_total / revenue) if revenue else 0.0

    roic = _real_roic(t)
    roic_is_proxy = False
    if roic is None:
        roic = float(_safe(info, "returnOnEquity", 0) or 0)
        roic_is_proxy = True

    gross_margin = float(_safe(info, "grossMargins", 0) or 0)
    rev_growth = float(_safe(info, "revenueGrowth", 0) or 0)

    total_debt = _safe(info, "totalDebt", 0) or 0
    cash = _safe(info, "totalCash", 0) or 0
    ebitda = _safe(info, "ebitda", 0) or 0
    nde = ((total_debt - cash) / ebitda) if ebitda else 0.0
    if nde < 0:
        nde = 0.0

    if not ath:
        ath = _safe(info, "fiftyTwoWeekHigh", 0) or price

    _pe_days, _pe_surprise, _pe_drift = _post_earnings_context(t, price)
    _chg90, _above_low = _trend_context(t, price, info)

    return Fundamentals(
        ticker=ticker,
        price=price,
        fcf_per_share=float(fcf_ps),
        roic=float(roic),
        roic_is_proxy=roic_is_proxy,
        gross_margin=gross_margin,
        fcf_margin=float(fcf_margin),
        rev_growth=rev_growth,
        net_debt_ebitda=float(nde),
        ath=float(ath or 0),
        raw_info=info,
        forward_pe=_safe(info, 'forwardPE', None),
        enterprise_value=_safe(info, 'enterpriseValue', None),
        ebitda_val=_safe(info, 'ebitda', None),
        market_cap=float(_safe(info, "marketCap", 0) or 0),
        analyst_growth=_analyst_growth(t, info),
        hist_fcf_cagr=_hist_fcf_cagr(t),
        moat=moat,
        next_earnings_days=_next_earnings_days(t),
        days_since_earnings=_pe_days,
        last_surprise_pct=_pe_surprise,
        post_earnings_drift=_pe_drift,
        chg_90d=_chg90,
        above_52w_low=_above_low,
        is_etf=False,
    )
