#!/usr/bin/env python3
"""Shared SQLite connection for SmartMoney Sentinel.

One place that owns DB behavior. WAL mode lets the Monday-morning jobs
(track_outcomes + post_mortem) read while another writes, instead of
throwing 'database is locked'. A busy timeout absorbs brief write contention.

Named dbconn (not db) on purpose: post_mortem.py and track_outcomes.py
already use a local variable called `db`, so `import db` would shadow-clash.
"""
from __future__ import annotations
import sqlite3


def connect(db_path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open journal.db with WAL + sane pragmas. Drop-in for sqlite3.connect."""
    con = sqlite3.connect(db_path, timeout=timeout)
    # journal_mode=WAL persists in the file, but re-set every connect so a
    # fresh journal.db (new machine, CI) is correct from first touch. Idempotent.
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")   # crash-safe under WAL, much faster
    con.execute("PRAGMA busy_timeout=30000;")   # 30s: 2nd writer waits, not errors
    return con
