"""Daily follow-up summary (built in code, so it costs no LLM quota)."""

from __future__ import annotations

import html
import re
from datetime import datetime, time, timedelta

from .schema import has_time, parse_deadline
from .sheets import LogRow

# Sunday first: python-telegram-bot's run_daily numbers days 0-6 as Sunday-Saturday.
DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
MAX_PER_SECTION = 15


def parse_times(value: str) -> list[time]:
    """'09:00, 4pm, 16:30' -> [09:00, 16:00, 16:30]; invalid parts are ignored."""
    times = set()
    for part in re.split(r"[,;]", value or ""):
        part = part.strip().lower().replace(" ", "")
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", part)
        if not m:
            continue
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            times.add(time(hour, minute))
    return sorted(times)


def parse_days(value: str) -> tuple[int, ...]:
    days = {DAY_NAMES.index(p.strip().lower()[:3]) for p in re.split(r"[,; ]+", value or "")
            if p.strip().lower()[:3] in DAY_NAMES}
    return tuple(sorted(days)) or tuple(range(7))


def _line(r: LogRow, show_due: bool) -> str:
    bits = [f"<b>{html.escape(r['Action'] or r['Problem'][:60] or '(no action)')}</b>"]
    for key in ("Driver Name", "Company"):
        if r[key]:
            bits.append(html.escape(r[key]))
    if r["Amount"]:
        bits.append(html.escape(r["Amount"]))
    if show_due and r["Deadline"]:
        bits.append(f"due {html.escape(r['Deadline'])}")
    if r["Assigned To"]:
        bits.append(f"→ {html.escape(r['Assigned To'])}")
    bits.append(f"<i>{html.escape(r['Status'])}, row {r.row}</i>")
    return "• " + " · ".join(bits)


def is_overdue(r: LogRow, now: datetime) -> bool:
    due = parse_deadline(r["Deadline"])
    if due is None:
        return False
    return due < now if has_time(r["Deadline"]) else due.date() < now.date()


def categorize(rows: list[LogRow], now: datetime) -> dict[str, list[LogRow]]:
    """Sort open items by urgency. Every open item lands in exactly one bucket;
    'logged_today' holds today's notes and done items (nothing to chase)."""
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    buckets: dict[str, list] = {k: [] for k in ("overdue", "today", "tomorrow", "upcoming", "no_deadline")}
    logged_today: list[LogRow] = []
    for r in rows:
        if r["Status"] in ("Done", "Note"):
            added = parse_deadline(r["Date"])
            if added and added.date() == today:
                logged_today.append(r)
            continue
        due = parse_deadline(r["Deadline"])
        if due is None:
            buckets["no_deadline"].append((datetime.max, r))
        elif is_overdue(r, now):
            buckets["overdue"].append((due, r))
        elif due.date() == today:
            buckets["today"].append((due, r))
        elif due.date() == tomorrow:
            buckets["tomorrow"].append((due, r))
        else:
            buckets["upcoming"].append((due, r))
    out = {k: [r for _, r in sorted(v, key=lambda p: (p[0], p[1].row))] for k, v in buckets.items()}
    out["logged_today"] = logged_today
    return out


def build_digest(rows: list[LogRow], now: datetime) -> str:
    """now must be naive local (Eastern) time."""
    c = categorize(rows, now)

    def section(title: str, items: list, show_due: bool) -> list[str]:
        if not items:
            return []
        out = [f"\n{title} ({len(items)})"]
        out += [_line(r, show_due) for r in items[:MAX_PER_SECTION]]
        if len(items) > MAX_PER_SECTION:
            out.append(f"…and {len(items) - MAX_PER_SECTION} more in the sheet")
        return out

    lines = [f"<b>📋 Follow-ups for {now.strftime('%A, %m/%d/%Y %I:%M %p')}</b>"]
    lines += section("🔴 <b>Overdue</b>", c["overdue"], True)
    lines += section("🟠 <b>Due today</b>", c["today"], True)
    lines += section("🟡 <b>Due tomorrow</b>", c["tomorrow"], True)
    lines += section("📌 <b>Open, no deadline</b>", c["no_deadline"], False)
    if len(lines) == 1:
        lines.append("\nNothing urgent. You're all caught up ✅")
    return "\n".join(lines)


# ---------------------------------------------------------------- "Today" tab

TODAY_COLUMNS = ["Row", "Deadline", "Status", "Action", "Driver Name", "Company", "Amount", "Assigned To", "Problem"]
TODAY_SECTIONS = [  # key, title, background color (hex)
    ("overdue", "OVERDUE", "#F4C7C3"),
    ("today", "DUE TODAY", "#FCE8B2"),
    ("tomorrow", "DUE TOMORROW", "#FFF4C2"),
    ("upcoming", "UPCOMING", "#E1ECFD"),
    ("no_deadline", "OPEN, NO DEADLINE", "#EEEEEE"),
    ("logged_today", "NOTES & DONE TODAY", "#DFF3E4"),
]


def today_grid(rows: list[LogRow], now: datetime) -> tuple[list[list], list[tuple[int, str]]]:
    """Values for the Today tab plus (row index, color) of each section header row (0-based)."""
    c = categorize(rows, now)
    grid: list[list] = [
        [f"Follow-ups for {now.strftime('%A, %m/%d/%Y')}  ·  updated {now.strftime('%I:%M %p').lstrip('0')}"],
        TODAY_COLUMNS,
    ]
    headers: list[tuple[int, str]] = []
    for key, title, color in TODAY_SECTIONS:
        items = c[key]
        if not items:
            continue
        grid.append([""])
        headers.append((len(grid), color))
        grid.append([f"{title} ({len(items)})"])
        for r in items:
            grid.append([r.row, r["Deadline"], r["Status"], r["Action"], r["Driver Name"], r["Company"],
                         r["Amount"], r["Assigned To"], r["Problem"]])
    if not headers:
        grid += [[""], ["Nothing open. All caught up ✅"]]
    return grid, headers
