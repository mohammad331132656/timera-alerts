#!/usr/bin/env python3
"""Notify a Telegram channel ONCE per newly opened Timera booking day.

Source evidence: public browser JavaScript saved by the user (Timera v4.3.1).
A live response from Timera has not been observed in this environment.
Only reads public booking data. Does not log in, reserve, or pay.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

API_URL = "https://timera.ir/api2/api/API"
BOOKING_URL = "https://timera.ir/b/10499"
BOOKABLE_ID = 10499
VERSION = "4.3.1"
STATE_PATH = Path(os.environ.get("STATE_PATH", ".timera_state.json"))
TEHRAN = ZoneInfo("Asia/Tehran")
PERSIAN_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
ARABIC_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_persian(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Civil Persian conversion for modern dates, with standard 33-year algorithm."""
    month_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + month_days[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """Inverse conversion; rejects invalid Persian calendar days on round-trip."""
    if jy < 1000 or not 1 <= jm <= 12 or not 1 <= jd <= 31:
        raise ValueError("Invalid Persian date")
    j = jy + 1595
    days = -355668 + 365 * j + (j // 33) * 8 + (j % 33 + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else 186 + (jm - 7) * 30
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    mdays = [0, 31, 28 + (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)),
             31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and gd > mdays[gm]:
        gd -= mdays[gm]
        gm += 1
    result = date(gy, gm, gd)
    if gregorian_to_jalali(result.year, result.month, result.day) != (jy, jm, jd):
        raise ValueError("Invalid Persian calendar date")
    return result


def parse_date(value: object) -> date:
    """Timera's Date/DateEnd are normally Solar Hijri YYYY/MM/DD.

    Accepts Gregorian ISO dates too, defensively, if the site changes its API.
    """
    text = str(value or "").translate(ARABIC_PERSIAN_DIGITS).strip()
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T\s].*)?", text)
    if not match:
        raise ValueError(f"Unknown Timera date format: {text!r}")
    y, m, d = map(int, match.groups())
    if y >= 1700:
        return date(y, m, d)
    return jalali_to_gregorian(y, m, d)


def day_key(gregorian_day: date) -> str:
    jy, jm, jd = gregorian_to_jalali(gregorian_day.year, gregorian_day.month, gregorian_day.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def format_day(key: str) -> str:
    y, m, d = (int(s) for s in key.split("/"))
    return f"{to_persian(d)} {PERSIAN_MONTHS[m - 1]} {to_persian(y)}"


def api_post(session: requests.Session, name: str, extra: dict | None = None) -> dict:
    payload = {"FN": name, **(extra or {}), "version": VERSION, "LocaleCode": "fa-IR", "Token": ""}
    response = session.post(
        API_URL,
        files={"Input": (None, json.dumps(payload, ensure_ascii=False))},
        headers={"Referer": BOOKING_URL, "Origin": "https://timera.ir", "User-Agent": "Mozilla/5.0 TimeraDaysNotifier/2.0"},
        timeout=25,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Timera returned non-JSON for {name}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected Timera response for {name}: not an object")
    return result


def fetch_calendar(session: requests.Session) -> list[dict]:
    # The site's saved JS queues 'start' before bookableLoad2 when not initialized.
    start = api_post(session, "start")
    if str(start.get("Result")) not in ("0", "None"):
        print(f"Note: start returned {start.get('Result')!r}")
    result = api_post(session, "bookableLoad2", {"ID": BOOKABLE_ID})
    if str(result.get("Result")) != "0":
        raise RuntimeError(f"Timera bookableLoad2 Result={result.get('Result')!r}")
    calendar = result.get("calendarList")
    if not isinstance(calendar, list) or not all(isinstance(item, dict) for item in calendar):
        raise RuntimeError("Timera response lacks a valid calendarList; site/API may have changed")
    return calendar


def extract_open_days(calendar: list[dict], today: date) -> list[str]:
    """Discover day-specific availability, not individual time slots.

    The saved Timera JS uses ID == -2 to mark reserveAble events.
    A fully booked/closed day is not treated as newly open.
    Expand Date..DateEnd if the response represents a multi-day event.
    """
    dates: set[str] = set()
    for event in calendar:
        try:
            if int(event.get("ID", -999)) != -2:
                continue
        except (ValueError, TypeError):
            continue
        start = parse_date(event.get("Date"))
        stop = parse_date(event.get("DateEnd") or event.get("Date"))
        if stop < start:
            raise ValueError("Timera event DateEnd precedes Date")
        if (stop - start).days > 366:
            raise ValueError("Unusually long Timera date range; requires manual inspection")
        current = max(start, today)
        while current <= stop:
            dates.add(day_key(current))
            current += timedelta(days=1)
    return sorted(dates)


def message_for_day(key: str) -> str:
    return f"🔔 رزرو روز {format_day(key)} باز شد.\n\n🔗 {BOOKING_URL}"


def telegram_send(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions Secrets")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    try:
        obj = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram returned a non-JSON response") from exc
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError("Telegram did not confirm message delivery")


def load_state(path: Path | None = None) -> set[str]:
    path = path or STATE_PATH
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != 2
            or data.get("booking_id") != BOOKABLE_ID
            or not isinstance(data.get("announced_dates"), list)
            or not all(isinstance(d, str) and re.fullmatch(r"\d{4}/\d{2}/\d{2}", d) for d in data["announced_dates"])):
        raise RuntimeError("Invalid or old state: back it up, then initialize a clean state")
    return set(data["announced_dates"])


def save_state(announced: set[str], path: Path | None = None) -> None:
    """Write after each successful send to support partial batches and retries."""
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 2, "booking_id": BOOKABLE_ID, "announced_dates": sorted(announced)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def process_days(open_days: list[str], announced: set[str], send, persist, pause: float = 0.0) -> list[str]:
    """Announce only never-seen dates, in chronological order, individually."""
    newly_sent = []
    for key in sorted(set(open_days) - announced):
        send(message_for_day(key))  # Do not mark a message delivered until successful.
        announced.add(key)
        persist(announced)
        newly_sent.append(key)
        if pause:
            time.sleep(pause)  # Respect Telegram rate limits if many days open at once.
    return newly_sent


def run(dry_run: bool = False) -> list[str]:
    with requests.Session() as session:
        calendar = fetch_calendar(session)
    today = datetime.now(TEHRAN).date()
    open_days = extract_open_days(calendar, today)
    announced = load_state()
    new = sorted(set(open_days) - announced)
    print(f"Timera {BOOKABLE_ID}: {len(calendar)} events, {len(open_days)} distinct open future/today days; {len(new)} new")
    if dry_run:
        for d in new:
            print("--- MESSAGE (DRY RUN; not sent) ---\n" + message_for_day(d))
        return new
    if new:
        # Save on EVERY successful Telegram delivery (a later send might fail).
        sent = process_days(open_days, announced, telegram_send, save_state, pause=1.1)
        print(f"Sent {len(sent)} separate Telegram channel messages")
    else:
        # First run still creates a baseline file. All historically sent dates are retained.
        if not STATE_PATH.exists():
            save_state(announced)
        print("No new day. Nothing sent.")
    return new


def demo() -> None:
    """Offline proof-of-logic; it never accesses Timera or Telegram."""
    from unittest.mock import Mock
    today = date(2026, 12, 5)
    d1, d2, d3, d4 = (day_key(today + timedelta(days=i)) for i in range(4))
    samples = [
        [{"ID": -2, "Date": d1, "DateEnd": d1}, {"ID": -2, "Date": d2}, {"ID": -2, "Date": d2}],
        [{"ID": -2, "Date": d3}, {"ID": -2, "Date": d1}, {"ID": -2, "Date": d4}, {"ID": -2, "Date": d2}],
        [{"ID": -2, "Date": d4}, {"ID": -2, "Date": d2}],
        [{"ID": -2, "Date": d4}, {"ID": -2, "Date": d2}, {"ID": -2, "Date": d3}],
    ]
    state = set()
    sent = Mock()
    for i, sample in enumerate(samples, start=1):
        new = process_days(extract_open_days(sample, today), state, sent, lambda *_: None)
        print(f"Offline round {i}: {', '.join(format_day(d) for d in new) or 'no message'}")
    assert [m.args[0] for m in sent.call_args_list] == [message_for_day(d) for d in (d1, d2, d3, d4)]
    print("PASS: 4 distinct dates, each announced once, in order; no channel messages sent.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Timera day-opening notifier")
    parser.add_argument("--dry-run", action="store_true", help="read live API but only print proposed channel alerts")
    parser.add_argument("--demo", action="store_true", help="offline mocked demonstration (no network or bot credentials)")
    args = parser.parse_args()
    if args.demo:
        demo()
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
