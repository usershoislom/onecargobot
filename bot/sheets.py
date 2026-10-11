"""Google Sheets access: layout setup, appending rows, highlight of new rows, undo, settings, Today tab."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import gspread
from gspread.utils import InsertDataOption, ValueInputOption, ValueRenderOption

from .schema import STATUSES, Colleague, Entry, redact

log = logging.getLogger(__name__)

LOG_TAB = "Log"
TODAY_TAB = "Today"
SETTINGS_TAB = "Settings"

HEADERS = [
    "Date", "Company", "Driver Name", "Amount", "Action", "Deadline",
    "Assigned To", "Problem", "Status", "Original Message",
]
COL = {name: i for i, name in enumerate(HEADERS)}  # 0-based
LAST_COL = "J"
STATUS_COL = "I"

# Bump when the Log formatting changes; the bot re-applies it on the next start.
LAYOUT_VERSION = "2"


def rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


WHITE = rgb("#FFFFFF")
TEXT = rgb("#202124")          # near-black body text
HEADER_BG = rgb("#1F3A5F")     # dark navy header, white text
NEW_ROW_BG = rgb("#FFF4C2")    # soft yellow: rows from the latest message
STATUS_COLORS = {              # (background, text) for the Status cell
    "Action": ("#FDE2E1", "#A50E0E"),
    "Follow-up": ("#E1ECFD", "#174EA6"),
    "Note": ("#EEEEEE", "#5F6368"),
    "Done": ("#DFF3E4", "#137333"),
}
OVERDUE_BG, DUE_TODAY_BG = rgb("#F4C7C3"), rgb("#FCE8B2")
# Default first tab names in various Google Sheets languages (removed if left empty).
DEFAULT_TAB_NAME = re.compile(r"^(Sheet|Лист|Hoja|Feuille|Tabelle|Foglio|Planilha|Arkusz|Sayfa)\s?\d+$")

DEFAULT_SETTINGS = [
    ("reminder_times", "09:00, 16:00", "Daily summary times (24h, US Eastern). Comma-separated, any number of times."),
    ("reminder_days", "Mon, Tue, Wed, Thu, Fri, Sat, Sun", "Days to send summaries."),
    ("auto_close_rows", "TRUE", "When you report something done, also set the matching open row to Done."),
    ("owner", "", "Owner's Telegram ID or @username. Gets the daily summaries. Set with /setowner."),
    ("users", "", "Other people allowed to use the bot (IDs or @usernames). Set with /adduser."),
    ("last_added_rows", "", "Managed by the bot (rows currently highlighted). Don't edit."),
    ("layout_version", "", "Managed by the bot. Don't edit."),
]
# Colleagues table on the Settings tab: E = name, F = Telegram @username, G = role.
COLLEAGUE_HEADERS = ["Colleague Name", "Telegram @username", "Role (e.g. Accounting, Safety)"]
COLLEAGUES_COL = "E"
TODAY_WIDTHS = [55, 150, 95, 240, 140, 160, 90, 150, 380]  # matches digest.TODAY_COLUMNS


@dataclass
class Settings:
    values: dict[str, str] = field(default_factory=dict)
    colleagues: list[Colleague] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    @property
    def auto_close(self) -> bool:
        return self.get("auto_close_rows", "TRUE").strip().upper() in {"TRUE", "YES", "1", "ON"}


@dataclass
class LogRow:
    row: int
    values: dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self.values.get(key, "")


def safe_text(value: str | None) -> str:
    """Stop Sheets from treating text like '+1 555...' or '=...' as a formula."""
    if value is None:
        return ""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def entry_to_row(entry: Entry, logged_on: datetime, original: str) -> list:
    return [
        logged_on.strftime("%m/%d/%Y"),
        safe_text(entry.company),
        safe_text(entry.driver_name),
        entry.amount if entry.amount is not None else "",
        safe_text(entry.action),
        entry.deadline or "",
        safe_text(entry.assigned_to),
        safe_text(entry.problem),
        entry.status,
        safe_text(redact(original)[:45000]),  # Sheets cell limit is 50k chars
    ]


def parse_row_range(a1: str) -> list[int]:
    """'Log!A12:J13' -> [12, 13]."""
    m = re.search(r"![A-Z]+(\d+)(?::[A-Z]+(\d+))?$", a1)
    if not m:
        return []
    start = int(m.group(1))
    end = int(m.group(2) or start)
    return list(range(start, end + 1))


def rows_to_setting(rows: list[int]) -> str:
    return ",".join(str(r) for r in rows)


def setting_to_rows(value: str) -> list[int]:
    return [int(v) for v in re.findall(r"\d+", value or "")]


def parse_settings(grid: list[list[str]]) -> Settings:
    values: dict[str, str] = {}
    colleagues: list[Colleague] = []
    c = ord(COLLEAGUES_COL) - ord("A")
    for r in grid[1:]:
        r = r + [""] * (c + 3 - len(r))
        if r[0].strip():
            values[r[0].strip()] = r[1].strip()
        if r[c].strip():
            colleagues.append(Colleague(name=r[c].strip(), username=r[c + 1].strip().lstrip("@"), role=r[c + 2].strip()))
    return Settings(values=values, colleagues=colleagues)


class Sheet:
    def __init__(self, credentials_file: str, spreadsheet_id: str, timezone: str):
        self.gc = gspread.service_account(filename=credentials_file)
        self.ss = self.gc.open_by_key(spreadsheet_id)
        self.timezone = timezone
        self.log_ws: gspread.Worksheet | None = None
        self.settings_ws: gspread.Worksheet | None = None
        self.today_ws: gspread.Worksheet | None = None

    # ------------------------------------------------------------ setup

    def ensure_layout(self) -> None:
        """Create tabs, headers, dropdowns and formatting. Safe to run on every start."""
        self.ss.batch_update({"requests": [{
            "updateSpreadsheetProperties": {
                "properties": {"locale": "en_US", "timeZone": self.timezone},
                "fields": "locale,timeZone",
            }
        }]})
        titles = {ws.title: ws for ws in self.ss.worksheets()}

        if LOG_TAB in titles:
            self.log_ws = titles[LOG_TAB]
        else:
            # Reuse the default empty "Sheet1" if it's the only tab, else add a new one.
            only = list(titles.values())
            if len(only) == 1 and not only[0].get_all_values():
                self.log_ws = only[0]
                self.log_ws.update_title(LOG_TAB)
            else:
                self.log_ws = self.ss.add_worksheet(LOG_TAB, rows=1000, cols=len(HEADERS), index=0)
        new_log = self.log_ws.row_values(1) != HEADERS
        if new_log:
            self.log_ws.update([HEADERS], "A1")

        if SETTINGS_TAB in titles:
            self.settings_ws = titles[SETTINGS_TAB]
        else:
            self.settings_ws = self.ss.add_worksheet(SETTINGS_TAB, rows=100, cols=8)
        self._ensure_settings()

        settings = self.read_settings()
        if new_log or settings.get("layout_version") != LAYOUT_VERSION:
            self._format_log()
            # Re-highlight the latest rows (the reset above cleared all row colors).
            self.highlight(setting_to_rows(settings.get("last_added_rows")), [])
            self.write_setting("layout_version", LAYOUT_VERSION)

        if TODAY_TAB in titles:
            self.today_ws = titles[TODAY_TAB]
        else:
            self.today_ws = self.ss.add_worksheet(TODAY_TAB, rows=300, cols=len(TODAY_WIDTHS), index=1)
        self._format_today_columns()

        # Remove an empty default tab ("Sheet1" / "Лист1") so only the bot's tabs remain.
        for title, ws in titles.items():
            if DEFAULT_TAB_NAME.match(title) and ws.id != self.log_ws.id and not ws.get_all_values():
                self.ss.del_worksheet(ws)

    def _format_log(self) -> None:
        """Header, column formats, status colors, deadline warnings. Also repairs data rows
        (dark text, no fill) so earlier formatting mistakes can't hide data."""
        ws = self.log_ws
        sid = ws.id
        meta = self.ss.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId),conditionalFormats)"})
        old_rules = next((len(sh.get("conditionalFormats", [])) for sh in meta.get("sheets", [])
                          if sh["properties"]["sheetId"] == sid), 0)
        widths = [95, 160, 140, 95, 240, 150, 130, 360, 100, 360]

        def cols(name: str) -> dict:
            return {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL[name], "endColumnIndex": COL[name] + 1}

        requests = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}} for _ in range(old_rules)]
        requests += [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "foregroundColor": WHITE},
                    "backgroundColor": HEADER_BG, "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)"}},
            # All data rows: dark regular text on white.
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": False, "foregroundColor": TEXT},
                                               "backgroundColor": WHITE, "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)"}},
            {"repeatCell": {
                "range": cols("Amount"),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {
                "range": cols("Date"),
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "mm/dd/yyyy"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Problem"], "endColumnIndex": len(HEADERS)},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy"}},
            {"setDataValidation": {
                "range": cols("Status"),
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": True}}},
            # Deadline cell: red when overdue, amber when due today (ignored once Done).
            self._rule(cols("Deadline"), 'AND(ISNUMBER($F2),$I2<>"Done",INT($F2)<TODAY())', OVERDUE_BG, rgb("#A50E0E")),
            self._rule(cols("Deadline"), 'AND(ISNUMBER($F2),$I2<>"Done",INT($F2)=TODAY())', DUE_TODAY_BG, None),
        ]
        for status, (bg, fg) in STATUS_COLORS.items():
            requests.append(self._rule(cols("Status"), f'$I2="{status}"', rgb(bg), rgb(fg)))
        for i, w in enumerate(widths):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        self.ss.batch_update({"requests": requests})

    @staticmethod
    def _rule(rng: dict, formula: str, bg: dict, fg: dict | None) -> dict:
        fmt: dict = {"backgroundColor": bg}
        if fg:
            fmt["textFormat"] = {"foregroundColor": fg, "bold": True}
        return {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [rng],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=" + formula}]},
                            "format": fmt}}}}

    def _format_today_columns(self) -> None:
        sid = self.today_ws.id
        requests = [{"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(TODAY_WIDTHS)]
        self.ss.batch_update({"requests": requests})

    def write_today(self, grid: list[list], section_rows: list[tuple[int, str]]) -> None:
        """Replace the Today tab with a freshly built follow-up list."""
        ws, sid = self.today_ws, self.today_ws.id
        width = len(TODAY_WIDTHS)
        if ws.row_count < len(grid) + 5:
            ws.add_rows(len(grid) + 50 - ws.row_count)
        ws.clear()
        ws.update([r + [""] * (width - len(r)) for r in grid], "A1", value_input_option=ValueInputOption.raw)

        def row_fmt(i: int, fmt: dict, fields: str) -> dict:
            return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                                             "startColumnIndex": 0, "endColumnIndex": width},
                                   "cell": {"userEnteredFormat": fmt}, "fields": f"userEnteredFormat({fields})"}}

        requests = [
            {"repeatCell": {"range": {"sheetId": sid},
                            "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": TEXT},
                                                           "backgroundColor": WHITE, "wrapStrategy": "WRAP",
                                                           "verticalAlignment": "TOP"}},
                            "fields": "userEnteredFormat"}},
            {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
                                       "fields": "gridProperties.frozenRowCount"}},
            row_fmt(0, {"textFormat": {"bold": True, "fontSize": 13, "foregroundColor": TEXT}}, "textFormat"),
            row_fmt(1, {"textFormat": {"bold": True, "foregroundColor": WHITE}, "backgroundColor": HEADER_BG},
                    "textFormat,backgroundColor"),
        ]
        requests += [row_fmt(i, {"textFormat": {"bold": True, "foregroundColor": TEXT}, "backgroundColor": rgb(color)},
                             "textFormat,backgroundColor") for i, color in section_rows]
        self.ss.batch_update({"requests": requests})

    def _ensure_settings(self) -> None:
        ws = self.settings_ws
        grid = ws.get_all_values()
        existing = {r[0].strip() for r in grid[1:] if r and r[0].strip()}
        if not grid or not grid[0] or grid[0][0] != "Setting":
            ws.update([["Setting", "Value", "Notes"]], "A1")
            existing = set()
        if not grid or len(grid[0]) < 5 or grid[0][4] != COLLEAGUE_HEADERS[0]:
            ws.update([COLLEAGUE_HEADERS], f"{COLLEAGUES_COL}1")
        ws.format("A1:G1", {"textFormat": {"bold": True}})
        missing = [list(row) for row in DEFAULT_SETTINGS if row[0] not in existing]
        if missing:
            start = max(len([r for r in grid if r and r[0].strip()]), 1) + 1
            ws.update(missing, f"A{start}")

    # ------------------------------------------------------------ settings

    def read_settings(self) -> Settings:
        grid = self.settings_ws.get_all_values()
        return parse_settings(grid)

    def write_setting(self, key: str, value: str) -> None:
        cell = self.settings_ws.find(key, in_column=1)
        if cell:
            self.settings_ws.update_acell(f"B{cell.row}", value)
        else:
            self.settings_ws.append_row([key, value], value_input_option=ValueInputOption.raw)

    # ------------------------------------------------------------ log rows

    def read_rows(self) -> list[LogRow]:
        grid = self.log_ws.get_all_values(value_render_option=ValueRenderOption.formatted)
        rows = []
        for i, r in enumerate(grid[1:], start=2):
            if not any(c.strip() for c in r):
                continue
            r = r + [""] * (len(HEADERS) - len(r))
            rows.append(LogRow(row=i, values=dict(zip(HEADERS, r))))
        return rows

    def append(self, rows: list[list]) -> list[int]:
        # OVERWRITE fills the next empty rows. INSERT_ROWS would copy the formatting of the
        # row above, which made the first data row inherit the header's white text.
        resp = self.log_ws.append_rows(
            rows, value_input_option=ValueInputOption.user_entered,
            insert_data_option=InsertDataOption.overwrite, table_range="A1",
        )
        return parse_row_range(resp.get("updates", {}).get("updatedRange", ""))

    def highlight(self, new_rows: list[int], previous_rows: list[int]) -> None:
        """Paint the newest rows soft yellow and return the previous batch to white.
        Text color is always set too, so a row can never end up unreadable."""
        sid = self.log_ws.id

        def paint(row: int, color: dict) -> dict:
            return {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "cell": {"userEnteredFormat": {"backgroundColor": color,
                                               "textFormat": {"bold": False, "foregroundColor": TEXT}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}}

        requests = [paint(r, WHITE) for r in previous_rows if r not in new_rows and r > 1]
        requests += [paint(r, NEW_ROW_BG) for r in new_rows if r > 1]
        if requests:
            self.ss.batch_update({"requests": requests})

    def set_status(self, row: int, status: str) -> None:
        self.log_ws.update_acell(f"{STATUS_COL}{row}", status)

    def delete_rows_if_match(self, rows: list[int], originals: dict[int, str]) -> list[int]:
        """Delete rows (bottom-up) only if their Original Message still matches. Returns deleted rows."""
        deleted = []
        for row in sorted(rows, reverse=True):
            current = self.log_ws.acell(f"{LAST_COL}{row}").value or ""
            if current.lstrip("'") == originals.get(row, "").lstrip("'"):
                self.log_ws.delete_rows(row)
                deleted.append(row)
        return deleted
