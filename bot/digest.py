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


def build_digest(rows: list[LogRow], now: datetime) -> str:
    """now must be naive local (Eastern) time."""
    today = now.date()
    tomorrow = today + timedelta(days=1)
    overdue, due_today, due_tomorrow, no_deadline = [], [], [], []

    for r in rows:
        if r["Status"] == "Done":
            continue
        due = parse_deadline(r["Deadline"])
        if due is None:
            if r["Status"] in ("Action", "Follow-up"):
                no_deadline.append(r)
        elif is_overdue(r, now):
            overdue.append((due, r))
        elif due.date() == today:
            due_today.append((due, r))
        elif due.date() == tomorrow:
            due_tomorrow.append((due, r))

    def section(title: str, items: list, show_due: bool) -> list[str]:
        if not items:
            return []
        out = [f"\n{title} ({len(items)})"]
        out += [_line(r, show_due) for r in items[:MAX_PER_SECTION]]
        if len(items) > MAX_PER_SECTION:
            out.append(f"…and {len(items) - MAX_PER_SECTION} more in the sheet")
        return out

    def by_due(pairs):
        return [r for _, r in sorted(pairs, key=lambda p: p[0])]

    lines = [f"<b>📋 Follow-ups for {now.strftime('%A, %m/%d/%Y %I:%M %p')}</b>"]
    lines += section("🔴 <b>Overdue</b>", by_due(overdue), True)
    lines += section("🟠 <b>Due today</b>", by_due(due_today), True)
    lines += section("🟡 <b>Due tomorrow</b>", by_due(due_tomorrow), True)
    lines += section("📌 <b>Open, no deadline</b>", no_deadline, False)
    if len(lines) == 1:
        lines.append("\nNothing open. You're all caught up ✅")
    return "\n".join(lines)
