#!/usr/bin/env python3
"""FMP data client for SmartMoney Sentinel.

Free-tier aware. Pulls only what engine.Fundamentals needs.
Flip TIER to "premium" later to enable analyst-estimate endpoints; the rest
of the code is unchanged.

Endpoints used (all available on FMP free/stable):
  /stable/quote                  -> price
  /stable/key-metrics-ttm        -> roic, fcf/share, margins
  /stable/ratios-ttm             -> gross margin, net debt/ebitda
  /stable/income-statement-growth-> revenue growth
  /stable/cash-flow-statement    -> historical FCF for CAGR
  /stable/analyst-estimates      -> forward FCF growth (PREMIUM only)
"""
from __future__ import annotations
import time
import urllib.request
import urllib.parse
import json
from typing import Optional

from engine import Fundamentals

TIER = "free"   # change to "premium" when you upgrade
BASE = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    pass


def _get(path: str, api_key: str, **params) -> list | dict:
    params["apikey"] = api_key
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode())
            if isinstance(data, dict) and data.get("Error Message"):
                raise FMPError(data["Error Message"])
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:           # rate limited -> back off
                time.sleep(2 * (attempt + 1))
                continue
            raise FMPError(f"HTTP {e.code} on {path}") from e
        except Exception as e:
            if attempt == 2:
                raise FMPError(f"{path}: {e}") from e
            time.sleep(1)
    raise FMPError(f"{path}: exhausted retries")


def _first(data, key, default=None):
    if isinstance(data, list) and data:
        return data[0].get(key, default)
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def _hist_fcf_cagr(api_key: str, ticker: str) -> Optional[float]:
    """5yr FCF CAGR from annual cash-flow statements (free tier: ~5yr)."""
    try:
        cf = _get("cash-flow-statement", api_key, symbol=ticker,
                  period="annual", limit=6)
    except FMPError:
        return None
    fcfs = [row.get("freeCashFlow") for row in cf if row.get("freeCashFlow")]
    fcfs = [x for x in fcfs if x and x > 0]
    if len(fcfs) < 4:
        return None
    newest, oldest = fcfs[0], fcfs[-1]   # FMP returns newest-first
    years = len(fcfs) - 1
    if oldest <= 0:
        return None
    return (newest / oldest) ** (1 / years) - 1


def _analyst_fcf_growth(api_key: str, ticker: str) -> Optional[float]:
    """Forward FCF growth from analyst estimates. PREMIUM only."""
    if TIER != "premium":
        return None
    try:
        est = _get("analyst-estimates", api_key, symbol=ticker, limit=2)
    except FMPError:
        return None
    if not isinstance(est, list) or len(est) < 2:
        return None
    # crude: use revenue-estimate growth as FCF proxy if FCF est absent
    try:
        this_y = est[0].get("estimatedRevenueAvg")
        next_y = est[1].get("estimatedRevenueAvg")
        if this_y and next_y and this_y > 0:
            return next_y / this_y - 1
    except Exception:
        return None
    return None


def fetch(ticker: str, api_key: str, ath: float = 0.0,
          moat: str = "") -> Fundamentals:
    """Assemble a Fundamentals object for one ticker.

    `ath` and `moat` come from your config (watchlist/holdings json), because
    all-time-high and a one-sentence moat are judgment inputs, not API fields.
    """
    quote = _get("quote", api_key, symbol=ticker)
    km = _get("key-metrics-ttm", api_key, symbol=ticker)
    ratios = _get("ratios-ttm", api_key, symbol=ticker)
    growth = _get("income-statement-growth", api_key, symbol=ticker, limit=1)

    price = _first(quote, "price", 0.0)
    # ath: prefer config value; else fall back to 52-week high from quote
    if not ath:
        ath = _first(quote, "yearHigh", 0.0) or price

    return Fundamentals(
        ticker=ticker,
        price=float(price or 0),
        fcf_per_share=float(_first(km, "freeCashFlowPerShareTTM", 0) or 0),
        roic=float(_first(km, "returnOnInvestedCapitalTTM", 0) or 0),
        gross_margin=float(_first(ratios, "grossProfitMarginTTM", 0) or 0),
        fcf_margin=float(_first(ratios, "freeCashFlowMarginTTM", 0) or 0),
        rev_growth=float(_first(growth, "growthRevenue", 0) or 0),
        net_debt_ebitda=float(_first(km, "netDebtToEBITDATTM", 0) or 0),
        ath=float(ath or 0),
        analyst_fcf_growth=_analyst_fcf_growth(api_key, ticker),
        hist_fcf_cagr=_hist_fcf_cagr(api_key, ticker),
        moat=moat,
    )
