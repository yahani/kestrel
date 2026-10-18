import itertools
from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest

from tests.conftest import EXAMPLE_CONFIG
from tracker import fetch as fetch_module
from tracker.config import load_config
from tracker.fetch import BROWSER_INSTALL_HINT, Fetcher, _Response, _retry_after

CONFIG = load_config(EXAMPLE_CONFIG)
SITE = CONFIG.site("books-sandbox")
BASE = "https://books.toscrape.com"
PAGE = f"{BASE}/catalogue/item_1/index.html"
ROBOTS = f"{BASE}/robots.txt"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """Scripted responses per URL. A list is consumed one item per request; the last repeats."""

    def __init__(self, time: FakeTime, routes: dict[str, object]):
        self.time = time
        self.routes = routes
        self.requests: list[tuple[float, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.time.now, request))
        route = self.routes.get(str(request.url), httpx.Response(404))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route

    def times(self, url: str) -> list[float]:
        return [t for t, r in self.requests if str(r.url) == url]

    def count(self, url: str) -> int:
        return len(self.times(url))


def make_fetcher(routes: dict[str, object], site_delay: float | None = None):
    time = FakeTime()
    server = Server(time, routes)
    client = httpx.Client(transport=httpx.MockTransport(server))
    fetcher = Fetcher(
        CONFIG.fetch, client=client, sleep=time.sleep, clock=time.clock, jitter=lambda: 0.0
    )
    return fetcher, server, time


def html(body: str = "<h1>ok</h1>") -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def robots(text: str) -> httpx.Response:
    return httpx.Response(200, text=text)


ALLOW_ALL = robots("User-agent: *\nDisallow:\n")


def test_fetch_ok_sends_honest_user_agent():
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: html("<p>hi</p>")})
    result = fetcher.fetch(PAGE, SITE)

    assert result.ok
    assert result.status == "ok"
    assert result.http_status == 200
    assert result.html == "<p>hi</p>"
    assert result.error is None
    for _, request in server.requests:
        assert request.headers["user-agent"] == CONFIG.fetch.user_agent
    assert "mailto:" in CONFIG.fetch.user_agent


def test_robots_disallow_skips_page():
    rules = robots("User-agent: *\nDisallow: /catalogue/\n")
    fetcher, server, _ = make_fetcher({ROBOTS: rules, PAGE: html()})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "skipped_robots"
    assert result.error == "disallowed by robots.txt"
    assert server.count(PAGE) == 0


def test_robots_disallow_for_our_agent_only():
    rules = robots("User-agent: price-tracker\nDisallow: /\n\nUser-agent: *\nDisallow:\n")
    fetcher, _server, _ = make_fetcher({ROBOTS: rules, PAGE: html()})
    assert fetcher.fetch(PAGE, SITE).status == "skipped_robots"


def test_robots_missing_allows_everything():
    fetcher, _, _ = make_fetcher({ROBOTS: httpx.Response(404), PAGE: html()})
    assert fetcher.fetch(PAGE, SITE).ok


def test_robots_forbidden_is_treated_as_disallow():
    fetcher, server, _ = make_fetcher({ROBOTS: httpx.Response(403), PAGE: html()})
    result = fetcher.fetch(PAGE, SITE)
    assert result.status == "skipped_robots"
    assert server.count(PAGE) == 0


def test_robots_server_error_skips_site_after_retries():
    fetcher, server, _ = make_fetcher({ROBOTS: httpx.Response(503), PAGE: html()})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "skipped_robots"
    assert result.error == "robots.txt unreachable: HTTP 503"
    assert server.count(ROBOTS) == 4  # first try + 3 retries
    assert server.count(PAGE) == 0


def test_robots_network_error_skips_site():
    error = httpx.ConnectError("name resolution failed")
    fetcher, server, _ = make_fetcher({ROBOTS: error, PAGE: html()})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "skipped_robots"
    assert "robots.txt unreachable: ConnectError" in result.error
    assert server.count(PAGE) == 0


def test_robots_fetched_once_per_site():
    other = f"{BASE}/catalogue/item_2/index.html"
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: html(), other: html()})
    fetcher.fetch(PAGE, SITE)
    fetcher.fetch(other, SITE)
    assert server.count(ROBOTS) == 1


def test_delay_between_requests_to_same_site():
    other = f"{BASE}/catalogue/item_2/index.html"
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: html(), other: html()})
    fetcher.fetch(PAGE, SITE)
    fetcher.fetch(other, SITE)

    times = [t for t, _ in server.requests]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert len(gaps) == 2
    assert all(gap >= SITE.delay_seconds for gap in gaps)


def test_no_delay_between_different_sites():
    other_site = CONFIG.site("scraping-dev")
    other_page = "https://web-scraping.dev/product/1"
    fetcher, _server, time = make_fetcher(
        {
            ROBOTS: httpx.Response(404),
            PAGE: html(),
            "https://web-scraping.dev/robots.txt": httpx.Response(404),
            other_page: html(),
        }
    )
    fetcher.fetch(PAGE, SITE)
    sleeps_before = len(time.sleeps)
    fetcher.fetch(other_page, other_site)
    # Only the robots->page delay on the new site, not a wait for the previous site.
    assert len(time.sleeps) - sleeps_before == 1


def test_crawl_delay_larger_than_site_delay_wins():
    rules = robots("User-agent: *\nCrawl-delay: 12\nDisallow:\n")
    other = f"{BASE}/catalogue/item_2/index.html"
    fetcher, server, _ = make_fetcher({ROBOTS: rules, PAGE: html(), other: html()})
    fetcher.fetch(PAGE, SITE)
    fetcher.fetch(other, SITE)

    page_times = server.times(PAGE) + server.times(other)
    assert page_times[1] - page_times[0] >= 12


def test_crawl_delay_smaller_than_site_delay_is_ignored():
    rules = robots("User-agent: *\nCrawl-delay: 1\nDisallow:\n")
    other = f"{BASE}/catalogue/item_2/index.html"
    fetcher, server, _ = make_fetcher({ROBOTS: rules, PAGE: html(), other: html()})
    fetcher.fetch(PAGE, SITE)
    fetcher.fetch(other, SITE)

    page_times = server.times(PAGE) + server.times(other)
    assert page_times[1] - page_times[0] >= SITE.delay_seconds


def test_retries_server_error_with_backoff_then_succeeds():
    responses = [httpx.Response(500), httpx.Response(502), html()]
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: responses})
    result = fetcher.fetch(PAGE, SITE)

    assert result.ok
    times = server.times(PAGE)
    assert len(times) == 3
    # Each retry waits the site delay plus exponential backoff (1s, then 2s).
    assert times[1] - times[0] >= SITE.delay_seconds + 1
    assert times[2] - times[1] >= SITE.delay_seconds + 2


def test_server_error_gives_up_after_three_retries():
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: httpx.Response(500)})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "http_error"
    assert result.http_status == 500
    assert result.error == "HTTP 500"
    assert server.count(PAGE) == 4


def test_not_found_is_not_retried():
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: httpx.Response(404)})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "http_error"
    assert result.http_status == 404
    assert server.count(PAGE) == 1


@pytest.mark.parametrize("code", [401, 403])
def test_forbidden_is_blocked_and_not_retried(code):
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: httpx.Response(code)})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "blocked"
    assert result.http_status == code
    assert server.count(PAGE) == 1


def test_rate_limit_honours_retry_after():
    responses = [httpx.Response(429, headers={"Retry-After": "30"}), html()]
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: responses})
    result = fetcher.fetch(PAGE, SITE)

    assert result.ok
    times = server.times(PAGE)
    assert times[1] - times[0] >= SITE.delay_seconds + 30


def test_retry_after_is_capped():
    responses = [httpx.Response(429, headers={"Retry-After": "3600"}), html()]
    fetcher, _, time = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: responses})
    fetcher.fetch(PAGE, SITE)
    # The retry waits the site delay plus the capped Retry-After.
    assert max(time.sleeps) == SITE.delay_seconds + fetch_module.MAX_RETRY_AFTER_SECONDS


def test_persistent_rate_limit_is_blocked():
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: httpx.Response(429)})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "blocked"
    assert result.http_status == 429
    assert server.count(PAGE) == 4


def test_timeout_is_retried_then_reported():
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: httpx.ReadTimeout("slow")})
    result = fetcher.fetch(PAGE, SITE)

    assert result.status == "http_error"
    assert result.http_status is None
    assert result.error == "timed out after 20s (after 3 retries)"
    assert server.count(PAGE) == 4


def test_transient_network_error_recovers():
    fetcher, _, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: [httpx.ConnectError("reset"), html()]})
    assert fetcher.fetch(PAGE, SITE).ok


def test_elapsed_time_is_recorded():
    fetcher, _, _ = make_fetcher({ROBOTS: ALLOW_ALL, PAGE: html()})
    result = fetcher.fetch(PAGE, SITE)
    assert result.elapsed >= SITE.delay_seconds  # includes the wait after robots.txt


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after": "12"}, 12.0), ({"Retry-After": "1.5"}, 1.5), ({}, None)],
)
def test_retry_after_parsing(headers, expected):
    assert _retry_after(headers) == expected


def test_retry_after_http_date_falls_back():
    assert _retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None


def test_context_manager_closes_client():
    fetcher, _, _ = make_fetcher({})
    with fetcher:
        pass
    assert fetcher._client.is_closed


# --- browser mode -------------------------------------------------------------

BROWSER_SITE = SITE.model_copy(update={"fetch_mode": "browser"})


class FakeBrowser:
    instances: ClassVar[list["FakeBrowser"]] = []

    def __init__(self, user_agent: str, timeout_seconds: float):
        self.user_agent = user_agent
        self.closed = False
        self.urls: list[str] = []
        FakeBrowser.instances.append(self)

    def get(self, url: str) -> _Response:
        self.urls.append(url)
        return _Response(200, "<h1>rendered</h1>")

    def close(self) -> None:
        self.closed = True


def test_browser_mode_uses_browser_but_checks_robots_over_http(monkeypatch):
    monkeypatch.setattr(fetch_module, "_BrowserLoader", FakeBrowser)
    FakeBrowser.instances.clear()
    fetcher, server, _ = make_fetcher({ROBOTS: ALLOW_ALL})

    with fetcher:
        result = fetcher.fetch(PAGE, BROWSER_SITE)
        fetcher.fetch(f"{BASE}/catalogue/item_2/index.html", BROWSER_SITE)

    assert result.ok
    assert result.html == "<h1>rendered</h1>"
    assert server.count(ROBOTS) == 1
    assert server.count(PAGE) == 0
    assert len(FakeBrowser.instances) == 1  # one browser per run, not per page
    browser = FakeBrowser.instances[0]
    assert browser.user_agent == CONFIG.fetch.user_agent
    assert browser.closed


def test_browser_mode_still_respects_robots(monkeypatch):
    monkeypatch.setattr(fetch_module, "_BrowserLoader", FakeBrowser)
    FakeBrowser.instances.clear()
    fetcher, _, _ = make_fetcher({ROBOTS: robots("User-agent: *\nDisallow: /\n")})

    assert fetcher.fetch(PAGE, BROWSER_SITE).status == "skipped_robots"
    assert FakeBrowser.instances == []


def test_browser_mode_without_playwright_gives_install_hint(monkeypatch):
    def missing(*args, **kwargs):
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr(fetch_module, "_BrowserLoader", missing)
    fetcher, _, _ = make_fetcher({ROBOTS: ALLOW_ALL})
    result = fetcher.fetch(PAGE, BROWSER_SITE)

    assert result.status == "http_error"
    assert result.error == BROWSER_INSTALL_HINT


def test_browser_that_fails_to_start_gives_install_hint(monkeypatch):
    def no_chromium(*args, **kwargs):
        raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium\nmore detail")

    monkeypatch.setattr(fetch_module, "_BrowserLoader", no_chromium)
    fetcher, _, _ = make_fetcher({ROBOTS: ALLOW_ALL})
    result = fetcher.fetch(PAGE, BROWSER_SITE)

    assert result.status == "http_error"
    assert result.error.startswith("could not start browser (Executable doesn't exist")
    assert "more detail" not in result.error
    assert BROWSER_INSTALL_HINT in result.error
