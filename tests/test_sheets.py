"""Sheets export against a fake gspread spreadsheet. Not tested against real Google yet."""

import inspect
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import gspread
import pytest
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from tests.conftest import MOCK_CONFIG_FILE
from tracker import sheets
from tracker.config import Config, load_config
from tracker.pipeline import run_once
from tracker.sheets import LATEST_HEADER, OBSERVATION_HEADER, export, spreadsheet_key
from tracker.store import Store

BASE = load_config(MOCK_CONFIG_FILE)


def sheets_config(**overrides) -> Config:
    data = BASE.model_dump(mode="json")
    data["sheets"] = {"enabled": True, "spreadsheet_id": "abc123", **overrides}
    return Config.model_validate(data)


CONFIG = sheets_config()


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.rows: list[list] = []
        self.frozen_rows = 0
        self.calls: list[str] = []
        self.formats: list[tuple] = []

    def row_values(self, row):
        return [str(v) for v in self.rows[row - 1]] if len(self.rows) >= row else []

    def append_rows(self, values, value_input_option="RAW"):
        self.calls.append(f"append_rows:{value_input_option}")
        self.rows += [list(v) for v in values]

    def clear(self):
        self.calls.append("clear")
        self.rows = []

    def update(self, values, range_name=None, raw=True, **kwargs):
        self.calls.append(f"update:{range_name}:raw={raw}")
        assert range_name == "A1"
        self.rows = [list(v) for v in values]

    def freeze(self, rows=None, cols=None):
        self.frozen_rows = rows

    def format(self, ranges, format):
        self.calls.append("format")
        self.formats.append((ranges, format))


class FakeSpreadsheet:
    def __init__(self, tabs=()):
        self.tabs = {t: FakeWorksheet(t) for t in tabs}

    def worksheet(self, title):
        if title not in self.tabs:
            raise WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols, index=None):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


def test_fakes_match_gspread_signatures():
    """Keep the fakes honest: same method names and parameters as gspread 6."""
    pairs = [
        (
            FakeWorksheet,
            gspread.Worksheet,
            ["row_values", "append_rows", "clear", "freeze", "format"],
        ),
        (FakeSpreadsheet, gspread.Spreadsheet, ["worksheet", "add_worksheet"]),
    ]
    for fake, real, names in pairs:
        for name in names:
            fake_params = list(inspect.signature(getattr(fake, name)).parameters)
            real_params = list(inspect.signature(getattr(real, name)).parameters)
            assert fake_params == real_params[: len(fake_params)], name
    real_update = inspect.signature(gspread.Worksheet.update).parameters
    assert list(real_update)[:4] == ["self", "values", "range_name", "raw"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=5)
        return self.now


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "service-account.json"
    path.write_text("{}")
    return {"GOOGLE_SERVICE_ACCOUNT_JSON": str(path)}


@pytest.fixture
def two_runs(mock_shop):
    """Two mock-shop runs (blue mug cut to 19.50 in between); returns (store, run1, run2)."""
    store = Store(sqlite3.connect(":memory:"))
    with mock_shop.fetcher(CONFIG) as fetcher:
        first = run_once(CONFIG, store, fetcher, now=Clock())
    mock_shop.set("blue-mug", price="19.50")
    with mock_shop.fetcher(CONFIG) as fetcher:
        second = run_once(CONFIG, store, fetcher, now=Clock())
    yield store, first, second
    store.close()


def do_export(spreadsheet, store, result, env, config=CONFIG):
    opened = []

    def open_fn(path, sheet_id):
        opened.append((path, sheet_id))
        return spreadsheet

    delivery = export(
        config, env, result.observations, store.latest_observations(), open_fn=open_fn
    )
    return delivery, opened


def test_first_export_creates_tabs_with_headers(two_runs, key_file):
    store, first, _ = two_runs
    sheet = FakeSpreadsheet()
    delivery, opened = do_export(sheet, store, first, key_file)

    assert delivery.ok, delivery.detail
    assert delivery.detail == "4 rows added, 4 in Latest"
    assert opened[0][1] == "abc123"
    history, latest = sheet.tabs["Observations"], sheet.tabs["Latest"]
    assert history.rows[0] == OBSERVATION_HEADER
    assert len(history.rows) == 5
    assert history.frozen_rows == 1 and latest.frozen_rows == 1
    assert latest.rows[0] == LATEST_HEADER


def test_observations_are_appended_and_latest_is_rewritten(two_runs, key_file):
    store, first, second = two_runs
    sheet = FakeSpreadsheet()
    do_export(sheet, store, first, key_file)
    do_export(sheet, store, second, key_file)

    history, latest = sheet.tabs["Observations"], sheet.tabs["Latest"]
    assert len(history.rows) == 1 + 4 + 4  # one header, two runs
    assert history.rows.count(OBSERVATION_HEADER) == 1
    assert len(latest.rows) == 1 + 4
    assert latest.calls[-3:] == ["clear", "update:A1:raw=True", "format"]
    assert [c for c in history.calls if c != "format"] == ["append_rows:RAW"] * 2


def test_row_values_match_the_database(two_runs, key_file):
    store, _, second = two_runs
    sheet = FakeSpreadsheet()
    do_export(sheet, store, second, key_file)

    header = sheet.tabs["Latest"].rows[0]
    rows = [dict(zip(header, r, strict=True)) for r in sheet.tabs["Latest"].rows[1:]]
    blue = next(r for r in rows if r["my_sku"] == "MUG-BLUE")
    assert blue == {
        "site": "rival-x",
        "url": "http://127.0.0.1:8000/product/blue-mug",
        "my_sku": "MUG-BLUE",
        "my_price": 23.0,
        "title": "Blue Mug",
        "price": 19.5,  # a number cell, so the sheet can sort and sum
        "currency": "USD",
        "stock_status": "in_stock",
        "stock_qty": 12,
        "undercuts_me": "yes",
        "status": "ok",
        "fetched_at": blue["fetched_at"],
        "error": "",
    }
    red = next(r for r in rows if r["my_sku"] == "MUG-RED")
    assert red["undercuts_me"] == ""  # 22.00 vs my 21.50

    db = {p.url: o for p, o in store.latest_observations()}
    for row in rows:
        assert Decimal(str(row["price"])) == db[row["url"]].price


def test_existing_tabs_with_header_are_reused(two_runs, key_file):
    store, first, _ = two_runs
    sheet = FakeSpreadsheet(tabs=["Observations", "Latest"])
    sheet.tabs["Observations"].rows = [OBSERVATION_HEADER, ["old row"]]
    do_export(sheet, store, first, key_file)
    assert sheet.tabs["Observations"].rows[:2] == [OBSERVATION_HEADER, ["old row"]]
    assert len(sheet.tabs["Observations"].rows) == 6


def test_empty_existing_tab_gets_a_header(two_runs, key_file):
    store, first, _ = two_runs
    sheet = FakeSpreadsheet(tabs=["Observations"])
    do_export(sheet, store, first, key_file)
    assert sheet.tabs["Observations"].rows[0] == OBSERVATION_HEADER


def test_custom_tab_names(two_runs, key_file):
    store, first, _ = two_runs
    config = sheets_config(observations_tab="History", latest_tab="Now")
    sheet = FakeSpreadsheet()
    do_export(sheet, store, first, key_file, config=config)
    assert set(sheet.tabs) == {"History", "Now"}


def test_failed_observation_row(mock_shop, key_file):
    import httpx

    mock_shop.overrides["/product/red-mug"] = httpx.Response(404)
    store = Store(sqlite3.connect(":memory:"))
    with mock_shop.fetcher(CONFIG) as fetcher:
        result = run_once(CONFIG, store, fetcher, now=Clock())
    sheet = FakeSpreadsheet()
    do_export(sheet, store, result, key_file)

    header = sheet.tabs["Observations"].rows[0]
    rows = [dict(zip(header, r, strict=True)) for r in sheet.tabs["Observations"].rows[1:]]
    red = next(r for r in rows if r["my_sku"] == "MUG-RED")
    assert (red["status"], red["error"], red["price"], red["title"]) == (
        "http_error", "HTTP 404", "", "",
    )  # fmt: skip


def test_formula_looking_titles_stay_text(two_runs, key_file):
    _store, first, _ = two_runs
    evil = [(p, replace(o, title="=IMPORTXML(A1)")) for p, o in first.observations]
    sheet = FakeSpreadsheet()
    export(CONFIG, key_file, evil, [], open_fn=lambda path, sheet_id: sheet)

    history = sheet.tabs["Observations"]
    assert history.rows[1][6] == "=IMPORTXML(A1)"
    assert history.calls[0] == "append_rows:RAW"  # RAW: stored as text, never run as a formula


# --- configuration and failures ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1AbC_d-9", "1AbC_d-9"),
        ("https://docs.google.com/spreadsheets/d/1AbC_d-9/edit#gid=0", "1AbC_d-9"),
        ("  1AbC  ", "1AbC"),
    ],
)
def test_spreadsheet_key_accepts_id_or_url(value, expected):
    assert spreadsheet_key(value) == expected


def test_missing_key_file_setting(two_runs):
    store, first, _ = two_runs
    delivery, opened = do_export(FakeSpreadsheet(), store, first, env={})
    assert not delivery.ok
    assert "GOOGLE_SERVICE_ACCOUNT_JSON is not set" in delivery.detail
    assert opened == []


def test_key_file_that_does_not_exist(two_runs, tmp_path):
    store, first, _ = two_runs
    env = {"GOOGLE_SERVICE_ACCOUNT_JSON": str(tmp_path / "nope.json")}
    delivery, _ = do_export(FakeSpreadsheet(), store, first, env)
    assert not delivery.ok
    assert "which doesn't exist" in delivery.detail


class FakeResponse:
    def __init__(self, code):
        self.code = code
        self.text = "denied"

    def json(self):
        return {"error": {"code": self.code, "message": "denied", "status": "PERMISSION_DENIED"}}


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (SpreadsheetNotFound(), True),
        (APIError(FakeResponse(403)), True),
        (APIError(FakeResponse(500)), False),
        (ValueError("bad key file"), False),
    ],
)
def test_google_errors_are_reported_not_raised(two_runs, key_file, error, hint):
    _store, first, _ = two_runs

    def open_fn(path, sheet_id):
        raise error

    delivery = export(CONFIG, key_file, first.observations, [], open_fn=open_fn)
    assert not delivery.ok
    assert ("is the sheet shared with the service account's email?" in delivery.detail) is hint


def test_same_tab_names_rejected():
    with pytest.raises(Exception, match="must be different tabs"):
        sheets_config(observations_tab="X", latest_tab="X")


def test_price_columns_show_two_decimals(two_runs, key_file):
    """Whole prices would otherwise show as 24 instead of 24.00."""
    store, first, _ = two_runs
    sheet = FakeSpreadsheet()
    do_export(sheet, store, first, key_file)

    two_dp = {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}
    assert sheet.tabs["Latest"].formats == [(["D2:D", "F2:F"], two_dp)]  # my_price, price
    assert sheet.tabs["Observations"].formats == [(["H2:H"], two_dp)]  # price
    assert LATEST_HEADER[3] == "my_price" and LATEST_HEADER[5] == "price"
    assert OBSERVATION_HEADER[7] == "price"


def test_column_letter():
    assert sheets._column(["a", "b", "price"], "price") == "C"
    many = [f"c{i}" for i in range(30)]
    assert sheets._column(many, "c26") == "AA"
