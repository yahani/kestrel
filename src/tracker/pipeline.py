"""One run: fetch and parse every product, store observations, detect changes."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import zip_longest

from tracker.changes import Change, detect_changes
from tracker.config import Config
from tracker.fetch import Fetcher
from tracker.parse import parse_page
from tracker.store import Observation, ProductRow, Store

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime) -> str:
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


@dataclass
class RunResult:
    run_id: str
    started_at: datetime
    finished_at: datetime
    observations: list[tuple[ProductRow, Observation]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for _, o in self.observations if o.ok)

    @property
    def failed(self) -> int:
        return len(self.observations) - self.succeeded

    @property
    def seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _interleave_by_site(products: list[ProductRow]) -> list[ProductRow]:
    """Round-robin across sites so one site's delay overlaps with requests to the others."""
    by_site: dict[str, list[ProductRow]] = {}
    for product in products:
        by_site.setdefault(product.site, []).append(product)
    return [p for group in zip_longest(*by_site.values()) for p in group if p is not None]


def observe(
    product: ProductRow, config: Config, fetcher: Fetcher, run_id: str, now: Clock
) -> Observation:
    """Fetch and parse one product. Never raises: any failure becomes the observation."""
    site = config.site(product.site)
    try:
        result = fetcher.fetch(product.url, site)
        fetched_at = now().isoformat(timespec="seconds")
        if not result.ok or result.html is None:
            return Observation(run_id, fetched_at, result.status, error=result.error)
        page = parse_page(result.html, site)
        return Observation(
            run_id,
            fetched_at,
            page.status,
            title=page.title,
            price=page.price,
            currency=page.currency,
            stock_status=page.stock_status,
            stock_qty=page.stock_qty,
            error=page.error,
        )
    except Exception as exc:  # one broken product must never stop the run
        log.exception("unexpected error on %s", product.url)
        return Observation(
            run_id,
            now().isoformat(timespec="seconds"),
            "parse_error",
            error=f"internal error: {type(exc).__name__}: {exc}",
        )


def detect(config: Config, store: Store, run_id: str) -> list[Change]:
    changes: list[Change] = []
    for current in store.run_observations(run_id):
        product = store.product(current.product_id)
        changes += detect_changes(
            product,
            current,
            previous=store.previous(current),
            previous_ok=store.previous(current, ok_only=True),
            min_change_percent=config.alerts.min_change_percent,
            previous_known_stock=store.previous(current, known_stock=True),
            my_currency=config.site(product.site).currency,
        )
    return changes


def run_once(config: Config, store: Store, fetcher: Fetcher, now: Clock = utc_now) -> RunResult:
    started = now()
    result = RunResult(new_run_id(started), started, started)
    for product in _interleave_by_site(store.sync_products(config)):
        observation = observe(product, config, fetcher, result.run_id, now)
        result.observations.append((product, store.record(product.id, observation)))
    result.changes = detect(config, store, result.run_id)
    result.finished_at = now()
    return result
