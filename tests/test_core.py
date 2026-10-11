from datetime import datetime, time

from bot import digest, pipeline
from bot.main import split_message
from bot.media import Item
from bot.schema import Analysis, Colleague, Entry, match_colleague, match_colleagues, parse_deadline, redact, verify
from bot.sheets import LogRow, entry_to_row, parse_row_range, parse_settings, safe_text, setting_to_rows

NOW = datetime(2026, 10, 12, 10, 30)  # Monday


def row(n, **kw) -> LogRow:
    base = {"Date": "10/01/2026", "Company": "", "Driver Name": "", "Amount": "", "Action": "Do thing",
            "Deadline": "", "Assigned To": "", "Problem": "", "Status": "Action", "Original Message": ""}
    base.update(kw)
    return LogRow(row=n, values=base)


# ---------------------------------------------------------------- schema / verification

def test_entry_coerces_amount_and_status():
    e = Entry(action="Pay", problem="x", status="follow up", amount="$1,250.50")
    assert e.amount == 1250.5
    assert e.status == "Follow-up"


def test_parse_deadline_formats():
    assert parse_deadline("10/15/2026") == datetime(2026, 10, 15)
    assert parse_deadline("10/15/2026 3:00 PM") == datetime(2026, 10, 15, 15, 0)
    assert parse_deadline("10/15/2026 15:00:00") == datetime(2026, 10, 15, 15, 0)
    assert parse_deadline("2026-10-15") == datetime(2026, 10, 15)
    assert parse_deadline("next friday") is None


def test_match_colleague():
    team = [Colleague("Bob Miller", "bob_acc"), Colleague("Sara Khan"), Colleague("Bobby Ray")]
    assert match_colleague("sara", team) == "Sara Khan"
    assert match_colleague("Bob Miller", team) == "Bob Miller"
    assert match_colleague("bob", team) == "Bob Miller"
    assert match_colleague("me", team) is None
    assert match_colleague("Jim", team) == "Jim"
    assert match_colleague("@Bob_ACC", team) == "Bob Miller"


def test_match_colleagues_from_mentions():
    team = [Colleague("Anna Cruz", "anna_acc", "Accounting"), Colleague("Sam Lee", "sam_safety", "Safety"),
            Colleague("Tom")]
    got = match_colleagues("@anna_acc @kate_acc, Tom @sam_safety, @ANNA_acc", team)
    assert got == "Anna Cruz, @kate_acc, Tom, Sam Lee"


def test_redact_sensitive_numbers():
    text = "Bank of America checking, Routing Number: 111000025 Account Number: 1234 5678 9012, SSN 123-45-6789"
    out = redact(text)
    assert "111000025" not in out and "123456789012" not in out.replace(" ", "")
    assert "****0025" in out and "****9012" in out and "SSN ****6789" in out
    assert redact("his number is 123-45-6789") == "his number is ***-**-****"
    # phone, load and money stay untouched
    assert redact("Load #12345678, call 5551234567, $1,250") == "Load #12345678, call 5551234567, $1,250"


def test_verify_normalizes_and_drops_bad_rows():
    a = Analysis(intent="log", closes_rows=[5, 99], entries=[
        Entry(action=" Reimburse lumper ", problem="p", status="Action", deadline="2026-10-15 14:00",
              assigned_to="sara", amount=185.004),
        Entry(action="Call", problem="p", status="Follow-up", deadline="whenever"),
    ])
    a, warnings = verify(a, [Colleague("Sara Khan")], open_rows={5, 6})
    e1, e2 = a.entries
    assert e1.action == "Reimburse lumper"
    assert e1.deadline == "10/15/2026 02:00 PM"
    assert e1.assigned_to == "Sara Khan"
    assert e1.amount == 185.0
    assert e2.deadline is None and any("whenever" in w for w in warnings)
    assert a.closes_rows == [5]


def test_verify_asks_when_action_missing():
    a = Analysis(intent="log", entries=[Entry(action="", problem="p", status="Action")])
    a, _ = verify(a, [], set())
    assert a.needs_clarification and a.clarification_question


# ---------------------------------------------------------------- sheets helpers

def test_parse_settings_colleagues():
    grid = [["Setting", "Value", "Notes", "", "Colleague Name", "Telegram @username", "Role"],
            ["reminder_times", "09:00, 16:00", "", "", "Anna Cruz", "@anna_acc", "Accounting"],
            ["auto_close_rows", "TRUE", "", "", "Sam Lee", "sam_safety"],
            ["", "", "", "", "Tom"]]
    st = parse_settings(grid)
    assert st.get("reminder_times") == "09:00, 16:00" and st.auto_close
    assert st.colleagues == [Colleague("Anna Cruz", "anna_acc", "Accounting"),
                             Colleague("Sam Lee", "sam_safety"), Colleague("Tom")]


def test_safe_text_blocks_formulas():
    assert safe_text("+1 555 123") == "'+1 555 123"
    assert safe_text("=HYPERLINK()") == "'=HYPERLINK()"
    assert safe_text("ABC") == "ABC"
    assert safe_text(None) == ""


def test_parse_row_range():
    assert parse_row_range("Log!A12:J13") == [12, 13]
    assert parse_row_range("'Log'!A7:J7") == [7]
    assert parse_row_range("") == []
    assert setting_to_rows("12,13") == [12, 13]


def test_entry_to_row_column_order():
    e = Entry(company="ABC", driver_name="John", amount=300, action="Pay", deadline="10/15/2026",
              assigned_to="Bob", problem="Lumper", status="Action")
    r = entry_to_row(e, NOW, "orig")
    assert r == ["10/12/2026", "ABC", "John", 300, "Pay", "10/15/2026", "Bob", "Lumper", "Action", "orig"]


# ---------------------------------------------------------------- digest

def test_parse_times_and_days():
    assert digest.parse_times("09:00, 4pm, 16:30, junk") == [time(9), time(16), time(16, 30)]
    assert digest.parse_times("12am, 12pm") == [time(0), time(12)]
    assert digest.parse_days("Mon, Tue, Wed, Thu, Fri") == (1, 2, 3, 4, 5)  # PTB: 0 = Sunday
    assert digest.parse_days("") == tuple(range(7))


def test_build_digest_sections():
    rows = [
        row(2, Action="Overdue task", Deadline="10/10/2026"),
        row(3, Action="Earlier today", Deadline="10/12/2026 09:00 AM"),   # time passed -> overdue
        row(4, Action="Later today", Deadline="10/12/2026 03:00 PM"),
        row(5, Action="Date only today", Deadline="10/12/2026"),         # not overdue until tomorrow
        row(6, Action="Tomorrow task", Deadline="10/13/2026"),
        row(7, Action="No deadline follow", Status="Follow-up"),
        row(8, Action="Just a note", Status="Note"),
        row(9, Action="Finished", Deadline="10/01/2026", Status="Done"),
    ]
    text = digest.build_digest(rows, NOW)
    overdue = text.split("Due today")[0]
    assert "Overdue task" in overdue and "Earlier today" in overdue
    today = text.split("Due today")[1].split("Due tomorrow")[0]
    assert "Later today" in today and "Date only today" in today
    assert "Tomorrow task" in text and "No deadline follow" in text
    assert "Just a note" not in text and "Finished" not in text


def test_build_digest_empty():
    assert "caught up" in digest.build_digest([row(2, Status="Done")], NOW)


# ---------------------------------------------------------------- pipeline

def test_build_context_includes_open_rows_and_question():
    ctx = pipeline.build_context(NOW, [Colleague("Bob", "bob_acc", "Accounting")],
                                 [row(4, Company="ABC", Amount="$10.00")], question="Which John?")
    assert "Monday, 10/12/2026" in ctx
    assert "Bob (@bob_acc, Accounting)" in ctx
    assert "4 | ABC |  | $10.00 | Do thing |  | Action" in ctx
    assert "EARLIER QUESTION YOU ASKED: Which John?" in ctx


def test_build_original_and_reply():
    items = [Item(kind="text", header="[Forwarded from Mike] ", text="lumper 185"),
             Item(kind="image", data=b"x", mime="image/jpeg")]
    a = Analysis(intent="log", doc_type="receipt_expense", transcript=None, source_notes="Lumper receipt $185",
                 checks=["✓ Amount matches caption"])
    orig = pipeline.build_original(items, a)
    assert orig == "[Receipt / expense]\n[Forwarded from Mike] lumper 185\n[image]\nFiles: Lumper receipt $185"
    a.entries = [Entry(action="Reimburse <lumper>", problem="p", status="Action", amount=185)]
    reply = pipeline.saved_reply(a, [14], [9], ["check"], "groq:x")
    assert "row 14" in reply and "&lt;lumper&gt;" in reply and "row 9 as Done" in reply and "backup AI" in reply
    assert "Receipt / expense" in reply and "✓ Amount matches caption" in reply


def test_split_message_keeps_lines():
    text = "\n".join(f"<b>line {i}</b>" for i in range(1000))
    chunks = split_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks) == text


def test_apply_mentions_adds_every_tagged_person():
    team = [Colleague("Anna Cruz", "anna_acc", "Accounting"), Colleague("Sam Lee", "sam_safety", "Safety")]
    items = [Item(kind="text", text="New contract for John @anna_acc @kate_acc @sam_safety"),
             Item(kind="text", header="[Forwarded from Broker] ", text="ping @broker_guy")]
    a = Analysis(intent="log", entries=[
        Entry(action="Update rates", problem="p", status="Action", assigned_to="Anna Cruz"),
        Entry(action="Contract details", problem="p", status="Note"),
    ])
    pipeline.apply_mentions(a, items, team)
    assert a.entries[0].assigned_to == "Anna Cruz, @kate_acc, Sam Lee"
    assert a.entries[1].assigned_to is None  # notes aren't assigned
    assert pipeline.mentions(items) == ["@anna_acc", "@kate_acc", "@sam_safety"]


def test_today_grid_sections_each_open_item_once():
    rows = [
        row(2, Action="Late", Deadline="10/10/2026"),
        row(3, Action="Today", Deadline="10/12/2026 03:00 PM"),
        row(4, Action="Tomorrow", Deadline="10/13/2026"),
        row(5, Action="Later", Deadline="10/20/2026"),
        row(6, Action="No date", Status="Follow-up"),
        row(7, Action="Note today", Status="Note", Date="10/12/2026"),
        row(8, Action="Old note", Status="Note", Date="10/01/2026"),
        row(9, Action="Done today", Status="Done", Date="10/12/2026"),
    ]
    grid, headers = digest.today_grid(rows, NOW)
    titles = [grid[i][0] for i, _ in headers]
    assert titles == ["OVERDUE (1)", "DUE TODAY (1)", "DUE TOMORROW (1)", "UPCOMING (1)",
                      "OPEN, NO DEADLINE (1)", "NOTES & DONE TODAY (2)"]
    listed = [r[0] for r in grid if r and isinstance(r[0], int)]
    assert listed == [2, 3, 4, 5, 6, 7, 9]  # each once, old note left out
    assert grid[1] == digest.TODAY_COLUMNS


def test_today_grid_empty():
    grid, headers = digest.today_grid([], NOW)
    assert headers == [] and "All caught up" in grid[-1][0]
