"""Pure helpers that shape what goes to the LLM and what goes back to the owner."""

from __future__ import annotations

import csv
import html
import io
import re
from datetime import datetime

from .media import Item
from .schema import Analysis, Colleague, Entry, match_colleagues
from .sheets import HEADERS, LogRow


DOC_LABELS = {
    "driver_contract": "Driver contract", "pay_rate_change": "Pay rate change", "direct_deposit": "Direct deposit",
    "onboarding_docs": "Onboarding docs", "termination": "Termination", "rate_confirmation": "Rate confirmation",
    "invoice": "Invoice", "settlement": "Settlement", "receipt_expense": "Receipt / expense", "bol_pod": "BOL / POD",
    "inspection_violation": "Inspection / violation", "accident_damage": "Accident / damage",
    "repair_maintenance": "Repair / maintenance", "load_issue": "Load issue", "payroll_payment": "Payroll / payment",
    "general_note": "Note",
}


_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{4,32})")


def mentions(items: list[Item]) -> list[str]:
    """@usernames the owner typed in the batch's own text (not in forwarded messages)."""
    found: list[str] = []
    for item in items:
        if item.kind == "text" and item.text and "[Forwarded from" not in item.header:
            for m in _MENTION.findall(item.text):
                if f"@{m}" not in found:
                    found.append(f"@{m}")
    return found


def apply_mentions(analysis: Analysis, items: list[Item], colleagues: list[Colleague]) -> None:
    """People the owner @mentions are assignees of every Action/Follow-up row (the AI sometimes drops some)."""
    tagged = mentions(items)
    if not tagged:
        return
    for e in analysis.entries:
        if e.status in ("Action", "Follow-up"):
            e.assigned_to = match_colleagues(", ".join(filter(None, [e.assigned_to, *tagged])), colleagues)


def open_row_numbers(rows: list[LogRow]) -> set[int]:
    return {r.row for r in rows if r["Status"] != "Done"}


def build_context(now: datetime, colleagues: list[Colleague], recent_rows: list[LogRow],
                  question: str | None = None) -> str:
    lines = [
        f"NOW: {now.strftime('%A, %m/%d/%Y %I:%M %p')} (US Eastern)",
        "COLLEAGUES: " + ("; ".join(c.describe() for c in colleagues) if colleagues else "(none listed)"),
        "RECENT ROWS (row | company | driver | amount | action | deadline | status):",
    ]
    if recent_rows:
        for r in recent_rows:
            lines.append(" | ".join([str(r.row), r["Company"], r["Driver Name"], r["Amount"],
                                     r["Action"], r["Deadline"], r["Status"]]))
    else:
        lines.append("(none)")
    if question:
        lines += [
            "",
            f"EARLIER QUESTION YOU ASKED: {question}",
            "The owner's answer is in the items marked [ANSWER] below. Use it; don't ask again.",
        ]
    lines += ["", "MESSAGE BATCH:"]
    return "\n".join(lines)


def build_original(items: list[Item], analysis: Analysis) -> str:
    """Text for the 'Original Message' column so the owner can verify each row."""
    parts = [f"[{DOC_LABELS.get(analysis.doc_type, 'Note')}]"] + [i.describe() for i in items]
    if analysis.transcript:
        parts.append(f"Transcript: {analysis.transcript}")
    if analysis.source_notes:
        parts.append(f"Files: {analysis.source_notes}")
    return "\n".join(p for p in parts if p.strip())


def rows_csv(rows: list[LogRow]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Row", *HEADERS])
    for r in rows:
        w.writerow([r.row, *[r[h] for h in HEADERS]])
    return buf.getvalue()


def describe_entry(e: Entry) -> str:
    bits = [f"<b>{html.escape(e.status)}</b>", html.escape(e.action)]
    for v in (e.driver_name, e.company):
        if v:
            bits.append(html.escape(v))
    if e.amount is not None:
        bits.append(f"${e.amount:,.2f}")
    if e.deadline:
        bits.append(f"due {html.escape(e.deadline)}")
    if e.assigned_to:
        bits.append(f"→ {html.escape(e.assigned_to)}")
    line = " · ".join(bits)
    if e.problem:
        line += f"\n   <i>{html.escape(e.problem)}</i>"
    return line


def describe_entries(entries: list[Entry]) -> str:
    return "\n".join(f"{n}. {describe_entry(e)}" for n, e in enumerate(entries, 1))


def checks_block(analysis: Analysis) -> str:
    if not analysis.checks:
        return ""
    return "🔎 <b>Checks</b>\n" + "\n".join(f"• {html.escape(c)}" for c in analysis.checks)


def saved_reply(analysis: Analysis, row_numbers: list[int], closed: list[int],
                warnings: list[str], provider: str) -> str:
    entries = analysis.entries
    if row_numbers:
        span = f"row {row_numbers[0]}" if len(row_numbers) == 1 else f"rows {row_numbers[0]}–{row_numbers[-1]}"
        head = f"✅ Added {len(entries)} {'entry' if len(entries) == 1 else 'entries'} ({span}, highlighted in the sheet)"
    else:
        head = "✅ Saved"
    head += f"\n📄 {DOC_LABELS.get(analysis.doc_type, 'Note')}"
    out = [head, describe_entries(entries)]
    if analysis.checks:
        out.append(checks_block(analysis))
    if closed:
        out.append("☑️ Also marked " + ", ".join(f"row {r}" for r in closed) + " as Done")
    out += [f"⚠️ {html.escape(w)}" for w in warnings]
    if provider.startswith("groq:"):
        out.append("<i>(Gemini was unavailable, so the backup AI handled this one. Double-check the numbers.)</i>")
    return "\n".join(out)
