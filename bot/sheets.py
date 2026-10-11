"""Google Sheets access: layout setup, appending rows, cyan highlight, undo, settings."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import gspread
from gspread.utils import ValueInputOption, ValueRenderOption

from .schema import STATUSES, Entry

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

CYAN = {"red": 0.0, "green": 1.0, "blue": 1.0}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

DEFAULT_SETTINGS = [
    ("reminder_times", "09:00, 16:00", "Daily summary times (24h, US Eastern). Comma-separated, any number of times."),
    ("reminder_days", "Mon, Tue, Wed, Thu, Fri, Sat, Sun", "Days to send summaries."),
    ("auto_close_rows", "TRUE", "When you report something done, also set the matching open row to Done."),
    ("last_added_rows", "", "Managed by the bot (rows currently highlighted cyan). Don't edit."),
]
COLLEAGUES_HEADER = "Colleagues"
COLLEAGUES_COL = "E"


@dataclass
class Settings:
    values: dict[str, str] = field(default_factory=dict)
    colleagues: list[str] = field(default_factory=list)

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
        safe_text(original[:45000]),  # Sheets cell limit is 50k chars
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


class Sheet:
    def __init__(self, credentials_file: str, spreadsheet_id: str, timezone: str):
        self.gc = gspread.service_account(filename=credentials_file)
        self.ss = self.gc.open_by_key(spreadsheet_id)
        self.timezone = timezone
        self.log_ws: gspread.Worksheet | None = None
        self.settings_ws: gspread.Worksheet | None = None

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
        if self.log_ws.row_values(1) != HEADERS:
            self.log_ws.update([HEADERS], "A1")
            self._format_log()

        if SETTINGS_TAB in titles:
            self.settings_ws = titles[SETTINGS_TAB]
        else:
            self.settings_ws = self.ss.add_worksheet(SETTINGS_TAB, rows=100, cols=6)
        self._ensure_settings()

        if TODAY_TAB not in titles:
            today = self.ss.add_worksheet(TODAY_TAB, rows=500, cols=len(HEADERS), index=1)
            self._format_today(today)

    def _format_log(self) -> None:
        ws = self.log_ws
        sid = ws.id
        widths = [95, 160, 140, 95, 240, 150, 130, 360, 100, 360]
        requests = [
            {"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "foregroundColor": WHITE},
                    "backgroundColor": {"red": 0.15, "green": 0.25, "blue": 0.4}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Amount"], "endColumnIndex": COL["Amount"] + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Date"], "endColumnIndex": COL["Date"] + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "mm/dd/yyyy"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Problem"], "endColumnIndex": len(HEADERS)},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            {"setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Status"], "endColumnIndex": COL["Status"] + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": True}}},
            {"setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Assigned To"], "endColumnIndex": COL["Assigned To"] + 1},
                "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [
                    {"userEnteredValue": f"={SETTINGS_TAB}!${COLLEAGUES_COL}$2:${COLLEAGUES_COL}"}]},
                    "showCustomUi": True, "strict": False}}},
            # Deadline cell: red when overdue, amber when due today (ignored once Done).
            self._deadline_rule(sid, 'AND(ISNUMBER($F2),$I2<>"Done",INT($F2)<TODAY())', {"red": 0.96, "green": 0.73, "blue": 0.73}),
            self._deadline_rule(sid, 'AND(ISNUMBER($F2),$I2<>"Done",INT($F2)=TODAY())', {"red": 1.0, "green": 0.87, "blue": 0.6}),
        ]
        for i, w in enumerate(widths):
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        self.ss.batch_update({"requests": requests})

    @staticmethod
    def _deadline_rule(sid: int, formula: str, color: dict) -> dict:
        return {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sid, "startRowIndex": 1, "startColumnIndex": COL["Deadline"], "endColumnIndex": COL["Deadline"] + 1}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=" + formula}]},
                            "format": {"backgroundColor": color}}}}}

    def _format_today(self, ws: gspread.Worksheet) -> None:
        ws.update([HEADERS], "A1")
        formula = (
            f'=IFERROR(SORT(FILTER({LOG_TAB}!A2:{LAST_COL}, {LOG_TAB}!I2:I<>"Done", '
            f'ISNUMBER({LOG_TAB}!F2:F), INT({LOG_TAB}!F2:F)<=TODAY()), 6, TRUE), "Nothing due today")'
        )
        ws.update([[formula]], "A2", value_input_option=ValueInputOption.user_entered)
        self.ss.batch_update({"requests": [
            {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": COL["Amount"], "endColumnIndex": COL["Amount"] + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": COL["Date"], "endColumnIndex": COL["Date"] + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "mm/dd/yyyy"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": COL["Deadline"], "endColumnIndex": COL["Deadline"] + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "mm/dd/yyyy h:mm AM/PM"}}},
                            "fields": "userEnteredFormat.numberFormat"}},
        ]})

    def _ensure_settings(self) -> None:
        ws = self.settings_ws
        grid = ws.get_all_values()
        existing = {r[0].strip() for r in grid[1:] if r and r[0].strip()}
        if not grid or not grid[0] or grid[0][0] != "Setting":
            ws.update([["Setting", "Value", "Notes", "", COLLEAGUES_HEADER]], "A1")
            ws.format("A1:E1", {"textFormat": {"bold": True}})
            existing = set()
        missing = [list(row) for row in DEFAULT_SETTINGS if row[0] not in existing]
        if missing:
            start = max(len([r for r in grid if r and r[0].strip()]), 1) + 1
            ws.update(missing, f"A{start}")

    # ------------------------------------------------------------ settings

    def read_settings(self) -> Settings:
        grid = self.settings_ws.get_all_values()
        values = {}
        colleagues = []
        col_idx = ord(COLLEAGUES_COL) - ord("A")
        for r in grid[1:]:
            if len(r) >= 2 and r[0].strip():
                values[r[0].strip()] = r[1].strip()
            if len(r) > col_idx and r[col_idx].strip():
                colleagues.append(r[col_idx].strip())
        return Settings(values=values, colleagues=colleagues)

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
        resp = self.log_ws.append_rows(
            rows, value_input_option=ValueInputOption.user_entered,
            insert_data_option="INSERT_ROWS", table_range="A1",
        )
        return parse_row_range(resp.get("updates", {}).get("updatedRange", ""))

    def highlight(self, new_rows: list[int], previous_rows: list[int]) -> None:
        """Paint the newest rows cyan and return the previous batch to white."""
        sid = self.log_ws.id

        def paint(row: int, color: dict) -> dict:
            return {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"}}

        requests = [paint(r, WHITE) for r in previous_rows if r not in new_rows and r > 1]
        requests += [paint(r, CYAN) for r in new_rows]
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
