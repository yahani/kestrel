import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import MOCK_CONFIG_FILE
from tracker import report as report_module
from tracker.config import load_config
from tracker.pipeline import run_once
from tracker.report import SITE_FAILURE_RUNS, SiteHealth, build_report
from tracker.store import Store

CONFIG = load_config(MOCK_CONFIG_FILE)
SLUGS = ["blue-mug", "red-mug", "green-teapot", "coaster-set"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=5)
        return self.now


@pytest.fixture
def store():
    with Store(sqlite3.connect(":memory:")) as s:
        yield s


def run_and_report(shop, store):
    with shop.fetcher(CONFIG) as fetcher:
        result = run_once(CONFIG, store, fetcher, now=Clock())
    return build_report(result, store)


def break_site(shop, response=None):
    for slug in SLUGS:
        shop.overrides[f"/product/{slug}"] = response or httpx.Response(
            200, text="<html>redesigned</html>"
        )


def test_summary_counts(mock_shop, store):
    run_and_report(mock_shop, store)
    mock_shop.set("blue-mug", price="19.50")
    mock_shop.overrides["/product/red-mug"] = httpx.Response(404)
    report = run_and_report(mock_shop, store)

    assert (report.checked, report.succeeded, report.failed) == (4, 3, 1)
    assert len(report.changes) == 2  # price_down + new_failure
    assert report.seconds > 0
    assert report.summary().startswith("4 products checked: 3 ok, 1 failed, 2 changes, ")
    assert report.failing_sites == ()


def test_summary_singular_change(mock_shop, store):
    run_and_report(mock_shop, store)
    mock_shop.set("blue-mug", price="19.50")
    assert ", 1 change, " in run_and_report(mock_shop, store).summary()


def test_one_failed_run_is_not_a_site_alert(mock_shop, store):
    run_and_report(mock_shop, store)
    break_site(mock_shop)
    assert run_and_report(mock_shop, store).failing_sites == ()


def test_site_failing_two_runs_in_a_row_alerts_once(mock_shop, store):
    run_and_report(mock_shop, store)
    break_site(mock_shop)
    run_and_report(mock_shop, store)

    second = run_and_report(mock_shop, store)
    [health] = second.failing_sites
    assert health == SiteHealth("rival-x", SITE_FAILURE_RUNS, ("parse_error",))
    assert second.new_site_alerts == (health,)
    assert health.describe() == (
        "rival-x failed 2 runs in a row (parse_error): selectors may have changed; "
        "check a page with `tracker test-selectors <url>`"
    )

    third = run_and_report(mock_shop, store)
    assert third.failing_sites[0].failed_runs == 3  # still listed in the report...
    assert third.new_site_alerts == ()  # ...but doesn't trigger a new alert


def test_partial_failure_breaks_the_streak(mock_shop, store):
    break_site(mock_shop)
    run_and_report(mock_shop, store)
    del mock_shop.overrides["/product/blue-mug"]  # one product works again
    run_and_report(mock_shop, store)
    break_site(mock_shop)
    assert run_and_report(mock_shop, store).failing_sites == ()


def test_recovery_clears_the_alert(mock_shop, store):
    break_site(mock_shop)
    run_and_report(mock_shop, store)
    run_and_report(mock_shop, store)
    mock_shop.overrides.clear()
    assert run_and_report(mock_shop, store).failing_sites == ()


def test_mixed_failure_statuses_are_listed(mock_shop, store):
    break_site(mock_shop, httpx.Response(403))
    mock_shop.overrides["/product/blue-mug"] = httpx.Response(500)
    run_and_report(mock_shop, store)
    report = run_and_report(mock_shop, store)
    assert report.failing_sites[0].statuses == ("blocked", "http_error")


def test_first_runs_failing_count_from_the_start(mock_shop, store):
    break_site(mock_shop)
    run_and_report(mock_shop, store)
    assert run_and_report(mock_shop, store).new_site_alerts != ()


@pytest.mark.parametrize(
    ("statuses", "advice"),
    [
        (("parse_error",), report_module.ADVICE_SELECTORS),
        (("skipped_robots",), report_module.ADVICE_UNREACHABLE),
        (("blocked", "http_error"), report_module.ADVICE_UNREACHABLE),
        (("http_error", "parse_error"), report_module.ADVICE_MIXED),
    ],
)
def test_site_health_advice_depends_on_why_it_failed(statuses, advice):
    health = SiteHealth("rival-x", 2, statuses)
    assert health.describe() == f"rival-x failed 2 runs in a row ({', '.join(statuses)}): {advice}"


def test_advice_wording():
    assert "selectors may have changed" in report_module.ADVICE_SELECTORS
    assert (
        "unreachable, blocking us or disallowing us in robots.txt"
        in report_module.ADVICE_UNREACHABLE
    )
    assert "fail to load or parse" in report_module.ADVICE_MIXED
