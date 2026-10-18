"""Polite page fetching: robots.txt, per-site delays and retries (F2, F3)."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from tracker.config import Fetch, Site

log = logging.getLogger(__name__)

FetchStatus = Literal["ok", "http_error", "blocked", "skipped_robots"]

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0
ROBOTS_AGENT = "price-tracker"
BROWSER_INSTALL_HINT = (
    "fetch_mode 'browser' needs Playwright: "
    "run `uv sync --extra browser` then `uv run playwright install chromium`"
)


@dataclass
class FetchResult:
    url: str
    status: FetchStatus
    http_status: int | None = None
    html: str | None = None
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class _Response:
    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)


class _TransportError(Exception):
    """A request failed before any HTTP status came back (timeout, DNS, connection)."""


@dataclass
class _RobotsRules:
    parser: RobotFileParser | None = None  # None means "allow everything"
    deny_all: bool = False
    unreachable: str | None = None  # error message when robots.txt could not be read
    crawl_delay: float | None = None

    def allows(self, url: str) -> bool:
        if self.deny_all or self.unreachable:
            return False
        return self.parser is None or self.parser.can_fetch(ROBOTS_AGENT, url)


class Throttle:
    """Keeps at least `delay` seconds between requests to the same site."""

    def __init__(self, sleep: Callable[[float], None], clock: Callable[[], float]):
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def wait(self, key: str, delay: float, extra: float = 0.0) -> None:
        """Sleep until `delay` has passed since the last request, plus `extra` on top."""
        last = self._last.get(key)
        remaining = delay - (self._clock() - last) if last is not None else 0.0
        if max(remaining, 0.0) + extra > 0:
            self._sleep(max(remaining, 0.0) + extra)
        self._last[key] = self._clock()


class Fetcher:
    """Fetches product pages for configured sites. `fetch` never raises."""

    def __init__(
        self,
        settings: Fetch,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ):
        self.user_agent = settings.user_agent
        self._timeout = settings.timeout_seconds
        self._client = client or httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self._client.headers["User-Agent"] = self.user_agent
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._throttle = Throttle(sleep, clock)
        self._robots: dict[str, _RobotsRules] = {}
        self._browser: _BrowserLoader | None = None

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        if self._browser is not None:
            self._browser.close()

    def fetch(self, url: str, site: Site) -> FetchResult:
        started = self._clock()
        result = self._fetch(url, site)
        result.elapsed = self._clock() - started
        if not result.ok:
            log.warning("fetch %s: %s (%s)", url, result.status, result.error)
        return result

    def _fetch(self, url: str, site: Site) -> FetchResult:
        rules = self._robots_for(site)
        if rules.unreachable:
            return FetchResult(url, "skipped_robots", error=rules.unreachable)
        if not rules.allows(url):
            return FetchResult(url, "skipped_robots", error="disallowed by robots.txt")

        if site.fetch_mode == "browser":
            try:
                get = self._browser_loader().get
            except ImportError:
                return FetchResult(url, "http_error", error=BROWSER_INSTALL_HINT)
            except Exception as exc:  # e.g. Chromium not downloaded yet
                first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                return FetchResult(
                    url,
                    "http_error",
                    error=f"could not start browser ({first_line}); " + BROWSER_INSTALL_HINT,
                )
        else:
            get = self._http_get

        try:
            response = self._get_with_retries(url, site, get)
        except _TransportError as exc:
            return FetchResult(url, "http_error", error=str(exc))

        code = response.status_code
        if 200 <= code < 300:
            return FetchResult(url, "ok", http_status=code, html=response.text)
        if code in (401, 403, 429):
            return FetchResult(url, "blocked", http_status=code, error=f"HTTP {code}")
        return FetchResult(url, "http_error", http_status=code, error=f"HTTP {code}")

    def _delay_for(self, site: Site) -> float:
        crawl_delay = self._robots.get(site.name, _RobotsRules()).crawl_delay
        return max(site.delay_seconds, crawl_delay or 0)

    def _get_with_retries(self, url: str, site: Site, get: Callable[[str], _Response]) -> _Response:
        backoff = 0.0
        for attempt in range(MAX_RETRIES + 1):
            # Backoff comes on top of the normal per-site delay, never instead of it.
            self._throttle.wait(site.name, self._delay_for(site), extra=backoff)
            try:
                response = get(url)
            except _TransportError as exc:
                if attempt == MAX_RETRIES:
                    raise _TransportError(f"{exc} (after {MAX_RETRIES} retries)") from exc
                backoff = self._backoff(attempt, url, str(exc))
                continue

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == MAX_RETRIES:
                return response
            backoff = self._backoff(
                attempt,
                url,
                f"HTTP {response.status_code}",
                retry_after=_retry_after(response.headers),
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(
        self, attempt: int, url: str, reason: str, retry_after: float | None = None
    ) -> float:
        wait = BACKOFF_BASE_SECONDS * 2**attempt + self._jitter()
        if retry_after is not None:
            wait = max(wait, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        log.info("retry %d/%d for %s in +%.1fs: %s", attempt + 1, MAX_RETRIES, url, wait, reason)
        return wait

    def _http_get(self, url: str) -> _Response:
        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise _TransportError(f"timed out after {self._timeout:g}s") from exc
        except httpx.TransportError as exc:
            raise _TransportError(f"{type(exc).__name__}: {exc}") from exc
        return _Response(response.status_code, response.text, response.headers)

    def _robots_for(self, site: Site) -> _RobotsRules:
        if site.name in self._robots:
            return self._robots[site.name]

        robots_url = urljoin(str(site.base_url), "/robots.txt")
        try:
            response = self._get_with_retries(robots_url, site, self._http_get)
        except _TransportError as exc:
            rules = _RobotsRules(unreachable=f"robots.txt unreachable: {exc}")
        else:
            rules = _rules_from_response(response)

        if rules.unreachable:
            log.warning("site %s skipped: %s", site.name, rules.unreachable)
        self._robots[site.name] = rules
        return rules

    def _browser_loader(self) -> _BrowserLoader:
        if self._browser is None:
            self._browser = _BrowserLoader(self.user_agent, self._timeout)
        return self._browser


def _rules_from_response(response: _Response) -> _RobotsRules:
    code = response.status_code
    if 200 <= code < 300:
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        delay = parser.crawl_delay(ROBOTS_AGENT)
        return _RobotsRules(parser=parser, crawl_delay=float(delay) if delay else None)
    if code in (401, 403):
        # More cautious than RFC 9309: a site that hides its robots.txt gets left alone.
        return _RobotsRules(deny_all=True)
    if 400 <= code < 500 and code != 429:
        return _RobotsRules()
    return _RobotsRules(unreachable=f"robots.txt unreachable: HTTP {code}")


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # HTTP-date form; fall back to normal backoff


class _BrowserLoader:  # pragma: no cover - needs a real browser; exercised by hand
    """Loads JavaScript-rendered pages with Playwright (optional extra)."""

    def __init__(self, user_agent: str, timeout_seconds: float):
        from playwright.sync_api import sync_playwright

        self._timeout_ms = timeout_seconds * 1000
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self._context = self._browser.new_context(user_agent=user_agent)

    def get(self, url: str) -> _Response:
        from playwright.sync_api import Error as PlaywrightError

        page = self._context.new_page()
        try:
            response = page.goto(url, timeout=self._timeout_ms, wait_until="networkidle")
            if response is None:
                raise _TransportError("no response from browser")
            return _Response(response.status, page.content(), response.headers)
        except PlaywrightError as exc:
            raise _TransportError(f"browser: {exc.message.splitlines()[0]}") from exc
        finally:
            page.close()

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()
