#!/usr/bin/env python3
"""Cross-vendor data firewall for SmartMoney Sentinel.

Yahoo is PRIMARY (free, unlimited, richer: ROIC, earnings, trend context).
FMP is a best-effort VALIDATOR. We compare only the fields both vendors
measure the same way, with per-field tolerances.

Philosophy = the rest of the system: FAIL OPEN, FLAG NEVER BLOCK.
  - FMP down / rate-limited / missing field  -> proceed Yahoo-only, note it.
  - Vendors disagree beyond tolerance         -> Telegram alert + stash a
    discordance report in raw_info, but STILL return Yahoo data so the brief
    runs. A vendor outage must never dark the whole pipeline.

Limits (be honest): this is a cross-vendor SANITY check, not truth detection.
If both vendors carry the same upstream error they agree and this stays silent.
It catches stale prices, unapplied splits, sign flips, sudden nulls — the
common corruption modes — not errors shared by both feeds.
"""
from __future__ import annotations
import os
from typing import Optional

import yahoo
import fmp

def _ensure_env():
    """Load ~/.config/guga/.env if FMP key not already set, so the
    firewall validates no matter who calls it, not only via sentinel."""
    if os.environ.get("FMP_API_KEY"):
        return
    try:
        from pathlib import Path
        p = Path.home() / ".config" / "guga" / ".env"
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


# Only fields both vendors compute comparably. ROIC excluded (Yahoo computes
# from statements w/ tax estimate; FMP reads an endpoint — structurally differ).
# analyst_growth excluded (FMP free-tier returns None). Per-field tolerance:
# price is a hard quote (tight); margins/growth differ by TTM-window + defn
# between vendors (loose). Tune here.
_TOLERANCE = {
    # CROSS-VENDOR CORRUPTION TRIPWIRE — only fields where disagreement
    # genuinely means something is broken (stale price, unapplied split,
    # sign flip, null). Price is the real catch. gross_margin is a clean,
    # stable ratio. Everything else (rev_growth, net_debt_ebitda, fcf_*)
    # is DEFINITIONALLY divergent between vendors — Yahoo reports MRQ-YoY
    # growth, FMP reports annual; EBITDA windows differ — so they always
    # disagree and make terrible tripwires. They are intentionally omitted:
    # cross-checking them produces false alarms, not safety.
    "price":        0.03,   # 3% — a stale/split-broken quote is real corruption
    "gross_margin": 0.10,   # 10% — clean ratio, should be close across vendors
}


def _rel_gap(a: float, b: float) -> Optional[float]:
    """Relative gap |a-b| / max(|a|,|b|). None if either side is unusable."""
    if a is None or b is None:
        return None
    if a == 0 or b == 0:          # 'X% of zero' is meaningless — skip the field
        return None
    return abs(a - b) / max(abs(a), abs(b))


def _skip_gross_margin(y, fmp_f) -> bool:
    """Gross margin is not a clean cross-vendor check for asset-light
    businesses (payment networks, some financials): near-100% or near-0%
    margins are computed differently by each vendor and always 'disagree'.
    Skip the comparison there to avoid daily false alarms; keep it for
    normal-margin businesses where a gap is a real signal."""
    for v in (getattr(y, 'gross_margin', None), getattr(fmp_f, 'gross_margin', None)):
        if v is not None and (v > 0.90 or v < 0.05):
            return True
    return False


def _alert(text: str) -> None:
    """Best-effort Telegram. Never raises — an alert failure can't break a run."""
    try:
        tok = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        if not tok or not chat:
            return
        import requests
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    except Exception:
        pass


def _compare(y, fmp_f) -> dict:
    """Return {field: (yahoo, fmp, gap)} for fields exceeding tolerance."""
    flags = {}
    for field, tol in _TOLERANCE.items():
        if field == "gross_margin" and _skip_gross_margin(y, fmp_f):
            continue
        gap = _rel_gap(getattr(y, field, None), getattr(fmp_f, field, None))
        if gap is not None and gap > tol:
            flags[field] = (getattr(y, field), getattr(fmp_f, field), gap)
    return flags


def fetch(ticker: str, api_key: str = "", ath: float = 0.0,
          moat: str = "", is_etf: bool = False):
    """Drop-in for yahoo.fetch. Yahoo primary; FMP validates non-ETFs."""
    # 1. PRIMARY — Yahoo. If this fails, behave exactly as before (raise).
    y = yahoo.fetch(ticker, api_key, ath=ath, moat=moat, is_etf=is_etf)

    # 2. ETFs: nothing to cross-check (no FCF/margins/ROIC). Return as-is.
    if is_etf:
        return y

    # 3. VALIDATOR — FMP, best effort. Any failure -> Yahoo-only, noted.
    _ensure_env()
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        y.raw_info["data_source_note"] = "single-source (no FMP key)"
        return y
    try:
        fmp_f = fmp.fetch(ticker, fmp_key, ath=ath, moat=moat)
    except Exception as e:
        y.raw_info["data_source_note"] = f"single-source (FMP failed: {e})"
        return y

    # 4. COMPARE the apples-to-apples fields.
    flags = _compare(y, fmp_f)
    if not flags:
        y.raw_info["data_source_note"] = "dual-source agree"
        return y

    # 5. DISCORDANCE — flag + alert, but return Yahoo data (fail open).
    lines = [f"{fld}: Y={yv:.4g} vs FMP={fv:.4g} (gap {g:.0%})"
             for fld, (yv, fv, g) in flags.items()]
    report = "; ".join(lines)
    y.raw_info["data_discordance"] = report
    _alert(f"⚠️ DATA DISCORDANCE — {ticker}\n{report}\n"
           f"(using Yahoo; verify before trusting today's read on {ticker})")
    return y
