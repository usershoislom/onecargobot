"""Pure helpers that shape what goes to the LLM and what goes back to the owner."""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime

from .media import Item
from .schema import Analysis, Entry
from .sheets import HEADERS, LogRow


def open_rows(rows: list[LogRow], limit: int) -> list[LogRow]:
    return [r for r in rows if r["Status"] != "Done"][-limit:]


def build_context(now: datetime, colleagues: list[str], open_rows_: list[LogRow],
                  question: str | None = None) -> str:
    lines = [
        f"NOW: {now.strftime('%A, %m/%d/%Y %I:%M %p')} (US Eastern)",
        f"COLLEAGUES: {', '.join(colleagues) if colleagues else '(none listed)'}",
        "OPEN ROWS:",
    ]
    if open_rows_:
        for r in open_rows_:
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
    parts = [i.describe() for i in items]
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


def saved_reply(entries: list[Entry], row_numbers: list[int], closed: list[int],
                warnings: list[str], provider: str) -> str:
    if row_numbers:
        span = f"row {row_numbers[0]}" if len(row_numbers) == 1 else f"rows {row_numbers[0]}–{row_numbers[-1]}"
        head = f"✅ Added {len(entries)} {'entry' if len(entries) == 1 else 'entries'} ({span}, highlighted cyan)"
    else:
        head = "✅ Saved"
    out = [head, describe_entries(entries)]
    if closed:
        out.append("☑️ Also marked " + ", ".join(f"row {r}" for r in closed) + " as Done")
    out += [f"⚠️ {html.escape(w)}" for w in warnings]
    if provider.startswith("groq:"):
        out.append("<i>(Gemini's free limit was reached, so the backup AI handled this one.)</i>")
    return "\n".join(out)
