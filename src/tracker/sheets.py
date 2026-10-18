"""Google Sheets export: append every observation, rewrite a "Latest" tab (F9).

Access is through a service account whose key file path is in GOOGLE_SERVICE_ACCOUNT_JSON;
share only the one spreadsheet with the service account's email address.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from tracker.alerts import Delivery
from tracker.config import Config
from tracker.store import Observation, ProductRow

OBSERVATION_HEADER = [
    "run_id",
    "fetched_at",
    "site",
    "url",
    "my_sku",
    "status",
    "title",
    "price",
    "currency",
    "stock_status",
    "stock_qty",
    "error",
]
LATEST_HEADER = [
    "site",
    "url",
    "my_sku",
    "my_price",
    "title",
    "price",
    "currency",
    "stock_status",
    "stock_qty",
    "undercuts_me",
    "status",
    "fetched_at",
    "error",
]
NEW_TAB_ROWS = 1000
MONEY_COLUMNS = ("my_price", "price")
TWO_DECIMALS = {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}

_SHEET_URL_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


class Worksheet(Protocol):
    def row_values(self, row: int) -> list[str]: ...
    def append_rows(self, values: list[list[Any]], value_input_option: str = ...) -> Any: ...
    def clear(self) -> Any: ...
    def update(self, values: list[list[Any]], range_name: str | None = ..., **kw: Any) -> Any: ...
    def freeze(self, rows: int | None = ..., cols: int | None = ...) -> Any: ...
    def format(self, ranges: list[str] | str, format: dict[str, Any]) -> Any: ...


class Spreadsheet(Protocol):
    def worksheet(self, title: str) -> Worksheet: ...
    def add_worksheet(self, title: str, rows: int, cols: int) -> Worksheet: ...


def spreadsheet_key(value: str) -> str:
    """Accept either the spreadsheet ID or its full URL."""
    match = _SHEET_URL_ID.search(value)
    return match.group(1) if match else value.strip()


def service_account_path(env: Mapping[str, str]) -> Path:
    value = env.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not value:
        raise ValueError(
            "sheets is enabled but GOOGLE_SERVICE_ACCOUNT_JSON is not set (see .env.example)"
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON points to {path}, which doesn't exist")
    return path


def open_spreadsheet(key_file: Path, spreadsheet_id: str) -> Spreadsheet:  # pragma: no cover
    """Open the spreadsheet with gspread. Needs real Google credentials; not run in tests."""
    client = gspread.service_account(filename=key_file)
    return client.open_by_key(spreadsheet_key(spreadsheet_id))


def _number(value: Decimal | None) -> float | str:
    # Sheets stores numbers as doubles anyway; a number cell sorts and sums properly.
    return float(value) if value is not None else ""


def _text(value: object) -> str | int:
    if value is None:
        return ""
    return value if isinstance(value, int) else str(value)


def observation_rows(observations: list[tuple[ProductRow, Observation]]) -> list[list[Any]]:
    return [
        [
            o.run_id,
            o.fetched_at,
            p.site,
            p.url,
            _text(p.my_sku),
            o.status,
            _text(o.title),
            _number(o.price),
            _text(o.currency),
            _text(o.stock_status),
            _text(o.stock_qty),
            _text(o.error),
        ]
        for p, o in observations
    ]


def latest_rows(latest: list[tuple[ProductRow, Observation]], config: Config) -> list[list[Any]]:
    rows = []
    for p, o in latest:
        same_currency = o.currency == config.site(p.site).currency
        undercuts = (
            p.my_price is not None
            and o.price is not None
            and same_currency
            and o.price < p.my_price
        )
        rows.append(
            [
                p.site,
                p.url,
                _text(p.my_sku),
                _number(p.my_price),
                _text(o.title),
                _number(o.price),
                _text(o.currency),
                _text(o.stock_status),
                _text(o.stock_qty),
                "yes" if undercuts else "",
                o.status,
                o.fetched_at,
                _text(o.error),
            ]
        )
    return rows


def _column(header: list[str], name: str) -> str:
    """Spreadsheet column letter for a header name: 0 -> A, 25 -> Z, 26 -> AA."""
    index, letters = header.index(name) + 1, ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _format_money(tab: Worksheet, header: list[str]) -> None:
    """Show prices as 24.00, not 24 (number cells have no decimals by default)."""
    ranges = [f"{c}2:{c}" for c in (_column(header, n) for n in MONEY_COLUMNS if n in header)]
    tab.format(ranges, TWO_DECIMALS)


def _tab(spreadsheet: Spreadsheet, title: str, header: list[str]) -> tuple[Worksheet, bool]:
    """The named tab, created (with a frozen header row) if missing. Returns (tab, created)."""
    try:
        return spreadsheet.worksheet(title), False
    except WorksheetNotFound:
        pass
    tab = spreadsheet.add_worksheet(title, rows=NEW_TAB_ROWS, cols=len(header))
    tab.freeze(rows=1)
    return tab, True


def write(
    spreadsheet: Spreadsheet,
    run_observations: list[tuple[ProductRow, Observation]],
    latest: list[tuple[ProductRow, Observation]],
    config: Config,
) -> None:
    history, created = _tab(spreadsheet, config.sheets.observations_tab, OBSERVATION_HEADER)
    rows = observation_rows(run_observations)
    if created or not history.row_values(1):
        rows = [OBSERVATION_HEADER, *rows]
    # RAW: a competitor title like "=IMPORTXML(...)" stays text and never runs as a formula.
    history.append_rows(rows, value_input_option="RAW")
    _format_money(history, OBSERVATION_HEADER)

    current, _ = _tab(spreadsheet, config.sheets.latest_tab, LATEST_HEADER)
    current.clear()
    current.update([LATEST_HEADER, *latest_rows(latest, config)], "A1", raw=True)
    _format_money(current, LATEST_HEADER)


OpenSpreadsheet = Callable[[Path, str], Spreadsheet]


def export(
    config: Config,
    env: Mapping[str, str],
    run_observations: list[tuple[ProductRow, Observation]],
    latest: list[tuple[ProductRow, Observation]],
    *,
    open_fn: OpenSpreadsheet = open_spreadsheet,
) -> Delivery:
    """Export to Sheets if enabled. Never raises; the result says what happened."""
    try:
        key_file = service_account_path(env)
        spreadsheet = open_fn(key_file, config.sheets.spreadsheet_id)
        write(spreadsheet, run_observations, latest, config)
    except ValueError as exc:
        return Delivery("sheets", False, str(exc))
    except Exception as exc:  # gspread/google-auth raise many types; report, don't crash
        not_shared = isinstance(exc, SpreadsheetNotFound) or (
            isinstance(exc, APIError) and exc.code == 403
        )
        hint = " (is the sheet shared with the service account's email?)" if not_shared else ""
        return Delivery("sheets", False, f"{type(exc).__name__}: {str(exc)[:200]}{hint}")
    n = len(run_observations)
    return Delivery(
        "sheets", True, f"{n} row{'' if n == 1 else 's'} added, {len(latest)} in Latest"
    )
