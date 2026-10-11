"""Structured output the LLM must return, plus the code-side verification layer.

The LLM does the understanding; this module double-checks what it returned
before anything touches the sheet (date formats, amounts, status values,
colleague names, row references, and masking of bank/SSN numbers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

STATUSES = ("Note", "Follow-up", "Action", "Done")
Status = Literal["Note", "Follow-up", "Action", "Done"]

# Message/document types. Each has a playbook in prompts/playbooks.md.
DOC_TYPES = (
    "driver_contract", "pay_rate_change", "direct_deposit", "onboarding_docs", "termination",
    "rate_confirmation", "invoice", "settlement", "receipt_expense", "bol_pod",
    "inspection_violation", "accident_damage", "repair_maintenance", "load_issue",
    "payroll_payment", "general_note",
)
DocType = Literal[
    "driver_contract", "pay_rate_change", "direct_deposit", "onboarding_docs", "termination",
    "rate_confirmation", "invoice", "settlement", "receipt_expense", "bol_pod",
    "inspection_violation", "accident_damage", "repair_maintenance", "load_issue",
    "payroll_payment", "general_note",
]


@dataclass(frozen=True)
class Colleague:
    name: str
    username: str = ""  # Telegram @username, without the @
    role: str = ""

    def describe(self) -> str:
        extra = ", ".join(x for x in (f"@{self.username}" if self.username else "", self.role) if x)
        return f"{self.name} ({extra})" if extra else self.name


class Entry(BaseModel):
    company: Optional[str] = Field(None, description="Carrier/broker/shipper/vendor, or null")
    driver_name: Optional[str] = Field(None, description="Driver name as written, or null")
    amount: Optional[float] = Field(None, description="Plain number, no $ or commas, or null")
    action: str = Field(description="Short imperative starting with a verb")
    deadline: Optional[str] = Field(None, description="MM/DD/YYYY or MM/DD/YYYY hh:mm AM/PM, or null")
    assigned_to: Optional[str] = Field(None, description="Colleague name(s), comma-separated, or null")
    problem: str = Field(description="1-2 sentences of context")
    status: Status

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, v):
        if v is None or v == "":
            return None
        if isinstance(v, str):
            cleaned = re.sub(r"[^0-9.\-]", "", v)
            return float(cleaned) if cleaned not in ("", "-", ".") else None
        return v

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        if isinstance(v, str):
            key = v.strip().lower().replace(" ", "-").replace("_", "-")
            for s in STATUSES:
                if s.lower() == key or (key == "followup" and s == "Follow-up"):
                    return s
        return v


class Analysis(BaseModel):
    intent: Literal["log", "question", "other"]
    doc_type: DocType = Field("general_note", description="Main type of the batch; picks the playbook")
    entries: list[Entry] = Field(default_factory=list)
    closes_rows: list[int] = Field(default_factory=list, description="Open row numbers now finished")
    needs_clarification: bool = False
    clarification_question: Optional[str] = None
    transcript: Optional[str] = Field(None, description="Verbatim-ish transcript of any voice/audio")
    source_notes: Optional[str] = Field(None, description="One line per image/PDF describing it")
    checks: list[str] = Field(default_factory=list,
                              description="Verification findings: mismatches, missing signatures/attachments, confirmations")
    reply: Optional[str] = Field(None, description="Short reply for intent=other")


# ---------------------------------------------------------------- dates

_DATE_FORMATS = (
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M%p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%y",
    "%m-%d-%Y",
)


def parse_deadline(value: str | None) -> datetime | None:
    """Parse a deadline string as written in the sheet or by the LLM (naive local time)."""
    if not value:
        return None
    text = re.sub(r"\s+", " ", str(value).strip()).upper().replace(" EST", "").replace(" EDT", "").replace(" ET", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def has_time(value: str | None) -> bool:
    return bool(value) and ":" in str(value)


def format_deadline(dt: datetime, with_time: bool) -> str:
    if with_time:
        return dt.strftime("%m/%d/%Y %I:%M %p")
    return dt.strftime("%m/%d/%Y")


# ---------------------------------------------------------------- verification

def match_colleague(name: str | None, colleagues: list[Colleague]) -> str | None:
    """Map 'bob' / '@bob_acc' / 'Bob M' to 'Bob Miller' when unambiguous; otherwise keep as written."""
    if not name:
        return None
    name = name.strip()
    low = name.lower().lstrip("@")
    if low in {"me", "i", "myself", "owner"}:
        return None
    for c in colleagues:
        if c.username and c.username.lower().lstrip("@") == low:
            return c.name
    exact = [c.name for c in colleagues if c.name.lower() == low]
    if exact:
        return exact[0]
    first = low.split()[0]
    starts = [c.name for c in colleagues if c.name.lower().split()[0] == first]
    if len(starts) == 1:
        return starts[0]
    return name


def match_colleagues(value: str | None, colleagues: list[Colleague]) -> str | None:
    """Same as match_colleague for a comma/space-separated list, de-duplicated, order kept."""
    if not value:
        return None
    parts = re.split(r"[,;]+|\s+(?=@)", value)
    out: list[str] = []
    for p in parts:
        m = match_colleague(p, colleagues)
        if m and m not in out:
            out.append(m)
    return ", ".join(out) or None


# Digits after these words are masked to the last 4 (bank, SSN, card numbers).
_SENSITIVE = re.compile(
    r"(?i)\b(account|acct|a/c|routing|aba|transit|ssn|social security|card|iban|swift)"
    r"(\s*(?:no\.?|number|num|#)?\s*[:#]?\s*)([0-9][0-9 \-]{5,}[0-9])"
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str | None) -> str | None:
    """Mask bank account/routing/SSN/card numbers so they never land in the sheet."""
    if not text:
        return text

    def mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(3))
        return f"{m.group(1)}{m.group(2)}****{digits[-4:]}"

    return _SSN.sub("***-**-****", _SENSITIVE.sub(mask, text))


def verify(analysis: Analysis, colleagues: list[Colleague], open_rows: set[int]) -> tuple[Analysis, list[str]]:
    """Normalize fields and return (fixed analysis, warnings for the owner)."""
    warnings: list[str] = []
    for i, e in enumerate(analysis.entries, 1):
        label = f"Row {i}" if len(analysis.entries) > 1 else "Entry"
        e.company = redact((e.company or "").strip()) or None
        e.driver_name = redact((e.driver_name or "").strip()) or None
        e.action = redact(e.action.strip())
        e.problem = redact(e.problem.strip())
        e.assigned_to = match_colleagues(e.assigned_to, colleagues)

        if e.deadline:
            dt = parse_deadline(e.deadline)
            if dt is None:
                warnings.append(f"{label}: couldn't read deadline '{e.deadline}', left it blank.")
                e.deadline = None
            else:
                e.deadline = format_deadline(dt, has_time(e.deadline))

        if e.amount is not None:
            e.amount = round(e.amount, 2)
            if abs(e.amount) >= 100_000:
                warnings.append(f"{label}: amount ${e.amount:,.2f} looks unusually large, please double-check.")

        if e.status in ("Action", "Follow-up") and not e.action:
            analysis.needs_clarification = True
            analysis.clarification_question = analysis.clarification_question or "What needs to be done here?"

    analysis.transcript = redact(analysis.transcript)
    analysis.source_notes = redact(analysis.source_notes)
    analysis.checks = [redact(c) for c in analysis.checks]
    # Only rows that really are open can be closed.
    analysis.closes_rows = [r for r in analysis.closes_rows if r in open_rows]
    if analysis.needs_clarification and not analysis.clarification_question:
        analysis.clarification_question = "Can you clarify what you'd like me to log?"
    return analysis, warnings
