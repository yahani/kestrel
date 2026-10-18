"""End-to-end over the mock shop: fetch -> parse -> store -> detect, with no network."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tests.conftest import MOCK_CONFIG_FILE
from tracker import pipeline
from tracker.config import Config, load_config
from tracker.pipeline import _interleave_by_site, new_run_id, run_once
from tracker.store import ProductRow, Store

CONFIG = load_config(MOCK_CONFIG_FILE)
BLUE = "http://127.0.0.1:8000/product/blue-mug"
RED = "http://127.0.0.1:8000/product/red-mug"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store():
    with Store(sqlite3.connect(":memory:")) as s:
        yield s


def run(shop, store, config: Config = CONFIG):
    with shop.fetcher(config) as fetcher:
        return run_once(config, store, fetcher, now=Clock())


def described(result) -> list[str]:
    return sorted(c.describe() for c in result.changes)


def test_first_run_records_everything_and_reports_no_changes(mock_shop, store):
    result = run(mock_shop, store)

    assert len(result.observations) == 4
    assert result.succeeded == 4
    assert result.failed == 0
    assert result.changes == []
    blue = next(o for p, o in result.observations if p.url == BLUE)
    assert (blue.title, blue.price, blue.currency) == ("Blue Mug", Decimal("24.00"), "USD")
    assert (blue.stock_status, blue.stock_qty) == ("in_stock", 12)
    assert blue.fetched_at.startswith("2026-10-18T06:00")


def test_two_price_changes_and_one_stock_change_give_exactly_three_changes(mock_shop, store):
    run(mock_shop, store)
    mock_shop.set("blue-mug", price="19.50")  # below my 23.00
    mock_shop.set("green-teapot", price="49.50")  # up 10%
    mock_shop.set("red-mug", stock=0)

    result = run(mock_shop, store)

    assert described(result) == [
        "rival-x cut Blue Mug from 24.00 USD to 19.50 USD (-18.8%), below your 23.00 USD",
        "rival-x is out of stock on Red Mug",
        "rival-x raised Green Teapot from 45.00 USD to 49.50 USD (+10.0%)",
    ]


def test_running_twice_with_no_changes_reports_nothing(mock_shop, store):
    run(mock_shop, store)
    assert run(mock_shop, store).changes == []
    assert run(mock_shop, store).changes == []


def test_back_in_stock_after_restock(mock_shop, store):
    run(mock_shop, store)
    mock_shop.set("red-mug", stock=0)
    run(mock_shop, store)
    mock_shop.set("red-mug", stock=8)
    assert described(run(mock_shop, store)) == ["rival-x is back in stock on Red Mug"]


def test_small_change_below_threshold_is_ignored(mock_shop, store):
    run(mock_shop, store)
    mock_shop.set("green-teapot", price="44.80")  # -0.4%, still above my 44.00
    assert run(mock_shop, store).changes == []


def test_failed_product_is_recorded_and_others_continue(mock_shop, store):
    run(mock_shop, store)
    mock_shop.overrides["/product/red-mug"] = httpx.Response(404)
    mock_shop.set("blue-mug", price="19.50")

    result = run(mock_shop, store)

    assert result.succeeded == 3
    assert result.failed == 1
    red = next(o for p, o in result.observations if p.url == RED)
    assert (red.status, red.error) == ("http_error", "HTTP 404")
    assert [c.kind for c in result.changes] == ["price_down", "new_failure"]


def test_change_detected_across_a_failed_run(mock_shop, store):
    run(mock_shop, store)  # blue 24.00
    mock_shop.overrides["/product/blue-mug"] = httpx.Response(503)
    run(mock_shop, store)  # blue fails
    del mock_shop.overrides["/product/blue-mug"]
    mock_shop.set("blue-mug", price="19.50")

    result = run(mock_shop, store)
    assert [c.kind for c in result.changes] == ["price_down"]
    assert result.changes[0].old_price == Decimal("24.00")


def test_layout_change_is_a_parse_error_not_a_crash(mock_shop, store):
    run(mock_shop, store)
    mock_shop.overrides["/product/blue-mug"] = httpx.Response(200, text="<html>redesign</html>")
    result = run(mock_shop, store)
    blue = next(o for p, o in result.observations if p.url == BLUE)
    assert blue.status == "parse_error"
    assert "title selector" in blue.error


def test_unexpected_exception_becomes_an_observation(mock_shop, store, monkeypatch):
    def boom(html, site):
        raise RuntimeError("selector engine exploded")

    monkeypatch.setattr(pipeline, "parse_page", boom)
    result = run(mock_shop, store)

    assert result.failed == 4
    statuses = {o.status for _, o in result.observations}
    assert statuses == {"parse_error"}
    assert result.observations[0][1].error == (
        "internal error: RuntimeError: selector engine exploded"
    )


def test_observations_carry_the_run_id(mock_shop, store):
    result = run(mock_shop, store)
    assert {o.run_id for _, o in result.observations} == {result.run_id}
    assert len(store.run_observations(result.run_id)) == 4


def test_run_timing(mock_shop, store):
    result = run(mock_shop, store)
    assert result.seconds > 0
    assert result.finished_at > result.started_at


def test_utc_now_is_timezone_aware():
    assert pipeline.utc_now().tzinfo is UTC


def test_new_run_id_format():
    run_id = new_run_id(datetime(2026, 10, 18, 6, 0, 5, tzinfo=UTC))
    assert run_id.startswith("20261018T060005Z-")
    assert len(run_id) == len("20261018T060005Z-") + 6


def test_interleave_by_site():
    def p(site, n):
        return ProductRow(n, site, f"http://{site}/{n}", None, None, True)

    products = [p("a", 1), p("a", 2), p("a", 3), p("b", 4), p("c", 5)]
    assert [x.id for x in _interleave_by_site(products)] == [1, 4, 5, 2, 3]
