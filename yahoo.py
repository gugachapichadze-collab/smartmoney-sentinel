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


class DataQualityError(ValueError):
    """Raised when a REQUIRED numeric field can't be recovered from raw API
    data. Subclasses ValueError so sentinel.run_engine's existing per-ticker
    `except Exception` treats it as a clean drop (logged under DATA ERRORS:),
    not a pipeline crash."""


_BAD_NUMERIC_STRINGS = {"", "n/a", "na", "none", "null", "-", "--", "nan"}


def _num(value, default=0.0):
    """Coerce an arbitrary API value to a FINITE float, else `default`.

    Never raises. Catches junk strings ('N/A','','-') that crash float()
    and NaN/inf that silently defeat downstream comparisons (a NaN margin
    otherwise passes `x < floor` as False and slips past hard_floors)."""
    if value is None:
        return default
    if isinstance(value, bool):            # bool is int subclass; don't map True->1.0
        return default
    if isinstance(value, (int, float)):
        v = float(value)
        return v if (v == v and v not in (float("inf"), float("-inf"))) else default
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in _BAD_NUMERIC_STRINGS:
            return default
        try:
            v = float(s.replace(",", ""))
        except (TypeError, ValueError):
            return default
        return v if (v == v and v not in (float("inf"), float("-inf"))) else default
    return default


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


def _computed_fcf(t, info) -> float:
    """Owner's FCF from the cash-flow statement, SBC-adjusted.

    info["freeCashflow"] is unreliable (e.g. ~$46B for NVDA vs the statement's
    ~$96.7B) and ignores stock-based comp. Compute it directly:
        Operating Cash Flow + Capital Expenditure (capex is negative) - SBC
    Fall back to info["freeCashflow"] only if the statement is missing.
    """
    try:
        cf = t.cashflow
        if cf is None or cf.empty:
            return _num(_safe(info, "freeCashflow"))

        def pick(df, names):
            for n in names:
                if n in df.index:
                    val = df.loc[n].iloc[0]
                    if val is not None:
                        try:
                            fv = float(val)
                        except (TypeError, ValueError):
                            continue
                        if fv == fv:  # NaN guard
                            return fv
            return None

        ocf = pick(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                        "OperatingCashFlow",
                        "Cash Flow From Continuing Operating Activities"])
        capex = pick(cf, ["Capital Expenditure", "Capital Expenditures",
                          "CapitalExpenditures"])
        sbc = pick(cf, ["Stock Based Compensation", "StockBasedCompensation"]) or 0.0

        if ocf is None or capex is None:
            return _num(_safe(info, "freeCashflow"))
        return ocf + capex - sbc
    except Exception:
        return _num(_safe(info, "freeCashflow"))


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

    # REQUIRED field: without a valid price we can value nothing -> clean drop.
    price = _num(_safe(info, "currentPrice"), default=None)
    if price is None or price <= 0:
        price = _num(_safe(info, "regularMarketPrice"), default=None)
    if price is None or price <= 0:
        raise DataQualityError(
            f"{ticker}: no valid price "
            f"(currentPrice={info.get('currentPrice')!r}, "
            f"regularMarketPrice={info.get('regularMarketPrice')!r})")

    if is_etf:
        return Fundamentals(ticker=ticker, price=price, fcf_per_share=0.0,
                            roic=0.0, gross_margin=0.0, fcf_margin=0.0,
                            rev_growth=0.0, net_debt_ebitda=0.0,
                            ath=(_num(ath) or _num(_safe(info,"fiftyTwoWeekHigh")) or price),
                            market_cap=_num(_safe(info,"marketCap")),
                            moat=moat, is_etf=True, raw_info=info)

    shares = _num(_safe(info, "sharesOutstanding"))
    fcf_total = _num(_computed_fcf(t, info))
    revenue = _num(_safe(info, "totalRevenue"))
    fcf_ps = (fcf_total / shares) if shares else 0.0
    fcf_margin = (fcf_total / revenue) if revenue else 0.0

    roic = _real_roic(t)
    roic_is_proxy = False
    if roic is None:
        roic = _num(_safe(info, "returnOnEquity"))
        roic_is_proxy = True
    else:
        roic = _num(roic)

    gross_margin = _num(_safe(info, "grossMargins"))
    rev_growth = _num(_safe(info, "revenueGrowth"))

    total_debt = _num(_safe(info, "totalDebt"))
    cash = _num(_safe(info, "totalCash"))
    ebitda = _num(_safe(info, "ebitda"))
    nde = ((total_debt - cash) / ebitda) if ebitda else 0.0
    if nde < 0:
        nde = 0.0

    ath = _num(ath)
    if not ath:
        ath = _num(_safe(info, "fiftyTwoWeekHigh")) or price

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
        ath=_num(ath),
        raw_info=info,
        forward_pe=_num(_safe(info, 'forwardPE'), default=None),
        enterprise_value=_num(_safe(info, 'enterpriseValue'), default=None),
        ebitda_val=_num(_safe(info, 'ebitda'), default=None),
        market_cap=_num(_safe(info, "marketCap")),
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
