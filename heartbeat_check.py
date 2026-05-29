#!/usr/bin/env python3
"""
heartbeat_check.py — watchdog for Sentinel. Separate process, separate launchd
timer. Catches the failure the in-process crash-alert CANNOT: a run that never
happened (launchd no-show, machine asleep, job unloaded). Reads the last success
timestamp and alerts via Telegram if it's too old.
"""
import os, sqlite3, datetime
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "journal.db"

# Reuse the same .env loader as sentinel.py — single credential source.
env_path = Path.home() / ".config" / "guga" / ".env"
for line in env_path.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

JOB = "daily_brief"
MAX_AGE_HOURS = 26          # daily job; >26h since last success = problem

def _alert(msg):
    try:
        tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        requests.post(url, json={"chat_id": chat, "text": msg}, timeout=20)
    except Exception as e:
        print(f"[alert failed] {e}")

def check():
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT last_success_utc FROM heartbeat WHERE job=?", (JOB,)).fetchone()
        con.close()
    except Exception:
        row = None

    now = datetime.datetime.now(datetime.timezone.utc)
    if not row:
        _alert(f"\u26a0\ufe0f Sentinel WATCHDOG: no heartbeat ever recorded for '{JOB}'. "
               f"The daily brief may have never run successfully.")
        print("ALERT: no heartbeat row")
        return

    try:
        last = datetime.datetime.fromisoformat(row[0])
    except Exception:
        _alert(f"\u26a0\ufe0f Sentinel WATCHDOG: heartbeat timestamp unreadable ({row[0]!r}).")
        print("ALERT: bad timestamp")
        return

    age_h = (now - last).total_seconds() / 3600.0
    if age_h > MAX_AGE_HOURS:
        _alert(f"\u26a0\ufe0f Sentinel SILENT: no successful run in {age_h:.1f}h "
               f"(last success {last.isoformat()} UTC, threshold {MAX_AGE_HOURS}h). "
               f"Check launchd / logs.")
        print(f"ALERT: stale heartbeat, age {age_h:.1f}h")
    else:
        print(f"OK: last success {age_h:.1f}h ago (< {MAX_AGE_HOURS}h threshold)")

if __name__ == "__main__":
    check()
