"""SQLite storage: products, an append-only log of observations (F5), and a record of
which runs' digests were delivered, so a failed send is carried into the next digest."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from tracker.config import Config

ObservationStatus = Literal["ok", "http_error", "parse_error", "blocked", "skipped_robots"]

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id       INTEGER PRIMARY KEY,
    site     TEXT NOT NULL,
    url      TEXT NOT NULL UNIQUE,
    my_sku   TEXT,
    my_price TEXT,                       -- Decimal as text, never float
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    run_id       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,          -- ISO 8601, UTC
    status       TEXT NOT NULL CHECK (status IN
                     ('ok', 'http_error', 'parse_error', 'blocked', 'skipped_robots')),
    title        TEXT,
    price        TEXT,                   -- Decimal as text, never float
    currency     TEXT,
    stock_status TEXT CHECK (stock_status IN ('in_stock', 'out_of_stock', 'unknown')),
    stock_qty    INTEGER,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS observations_product ON observations (product_id, id);
CREATE INDEX IF NOT EXISTS observations_run ON observations (run_id);

-- History is never overwritten (F5).
CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;

-- One row per real (non-dry) run: which channels were on, and what its digest had to say.
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,            -- ISO 8601, UTC
    channels   TEXT NOT NULL,            -- comma-separated, e.g. 'email,slack'; '' for none
    items      TEXT NOT NULL             -- JSON list of {section, text, url}; [] when nothing
);

-- One row per run per channel whose items reached that channel.
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    channel      TEXT NOT NULL,
    delivered_in TEXT NOT NULL,          -- the run whose digest carried it
    delivered_at TEXT NOT NULL,
    UNIQUE (run_id, channel)
);

CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS deliveries_no_update BEFORE UPDATE ON deliveries
BEGIN SELECT RAISE(ABORT, 'deliveries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS deliveries_no_delete BEFORE DELETE ON deliveries
BEGIN SELECT RAISE(ABORT, 'deliveries are append-only'); END;
"""


@dataclass(frozen=True)
class ProductRow:
    id: int
    site: str
    url: str
    my_sku: str | None
    my_price: Decimal | None
    active: bool


@dataclass(frozen=True)
class SiteRun:
    """How one site did in one run."""

    run_id: str
    ok: int
    total: int
    statuses: tuple[str, ...]  # distinct statuses seen, sorted


@dataclass(frozen=True)
class PendingRun:
    """An earlier run whose digest items haven't reached a channel yet."""

    run_id: str
    started_at: str
    items: list[dict]


@dataclass(frozen=True)
class Observation:
    run_id: str
    fetched_at: str
    status: ObservationStatus
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    stock_status: str | None = None
    stock_qty: int | None = None
    error: str | None = None
    id: int | None = None
    product_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None  # "100", never "1E+2"


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    @classmethod
    def open(cls, path: str | Path) -> Store:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path))

    @classmethod
    def scratch_copy(cls, path: str | Path) -> Store:
        """An in-memory copy of the database (empty if it doesn't exist). For dry runs."""
        memory = sqlite3.connect(":memory:")
        if Path(path).is_file():
            disk = sqlite3.connect(path)
            disk.backup(memory)
            disk.close()
        return cls(memory)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- products ------------------------------------------------------------------

    def sync_products(self, config: Config) -> list[ProductRow]:
        """Upsert products from config; mark ones no longer in config inactive (history kept).

        Returns the active products in config order.
        """
        with self.conn:
            self.conn.execute("UPDATE products SET active = 0")
            for product in config.products:
                self.conn.execute(
                    """
                    INSERT INTO products (site, url, my_sku, my_price, active)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT (url) DO UPDATE SET
                        site = excluded.site,
                        my_sku = excluded.my_sku,
                        my_price = excluded.my_price,
                        active = 1
                    """,
                    (product.site, str(product.url), product.my_sku, _text(product.my_price)),
                )
        by_url = {p.url: p for p in self.products(active_only=True)}
        return [by_url[str(p.url)] for p in config.products]

    def products(self, active_only: bool = False) -> list[ProductRow]:
        sql = "SELECT * FROM products" + (" WHERE active = 1" if active_only else "")
        return [self._product(row) for row in self.conn.execute(sql + " ORDER BY id")]

    def product(self, product_id: int) -> ProductRow:
        row = self.conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise KeyError(product_id)
        return self._product(row)

    def find_products(self, query: str) -> list[ProductRow]:
        """Products whose URL or SKU equals `query`, or whose URL contains it."""
        if not query.strip():
            return []
        rows = self.conn.execute(
            """
            SELECT * FROM products
            WHERE url = :q OR my_sku = :q OR instr(url, :q) > 0
            ORDER BY (url = :q OR my_sku = :q) DESC, active DESC, id
            """,
            {"q": query},
        )
        products = [self._product(row) for row in rows]
        exact = [p for p in products if query in (p.url, p.my_sku)]
        return exact or products

    @staticmethod
    def _product(row: sqlite3.Row) -> ProductRow:
        return ProductRow(
            id=row["id"],
            site=row["site"],
            url=row["url"],
            my_sku=row["my_sku"],
            my_price=_dec(row["my_price"]),
            active=bool(row["active"]),
        )

    # --- observations --------------------------------------------------------------

    def record(self, product_id: int, observation: Observation) -> Observation:
        """Append one observation. Returns it with its id and product_id set."""
        o = observation
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO observations (product_id, run_id, fetched_at, status, title, price,
                                          currency, stock_status, stock_qty, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id, o.run_id, o.fetched_at, o.status, o.title, _text(o.price),
                    o.currency, o.stock_status, o.stock_qty, o.error,
                ),
            )  # fmt: skip
        return self.observation(cursor.lastrowid)

    def observation(self, observation_id: int) -> Observation:
        row = self.conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(observation_id)
        return self._observation(row)

    def run_observations(self, run_id: str) -> list[Observation]:
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [self._observation(row) for row in rows]

    def previous(
        self, current: Observation, ok_only: bool = False, known_stock: bool = False
    ) -> Observation | None:
        """The observation of the same product just before `current`.

        `ok_only` skips failed observations; `known_stock` also skips ones whose stock
        status is `unknown`.
        """
        sql = "SELECT * FROM observations WHERE product_id = ? AND id < ?"
        if ok_only or known_stock:
            sql += " AND status = 'ok'"
        if known_stock:
            sql += " AND stock_status IN ('in_stock', 'out_of_stock')"
        row = self.conn.execute(
            sql + " ORDER BY id DESC LIMIT 1", (current.product_id, current.id)
        ).fetchone()
        return self._observation(row) if row is not None else None

    def history(self, product_id: int, limit: int = 20) -> list[Observation]:
        """Most recent observations first."""
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE product_id = ? ORDER BY id DESC LIMIT ?",
            (product_id, limit),
        )
        return [self._observation(row) for row in rows]

    def site_runs(self, site: str, limit: int = 100) -> list[SiteRun]:
        """Per-run results for one site, newest run first."""
        rows = self.conn.execute(
            """
            SELECT o.run_id,
                   SUM(o.status = 'ok') AS ok,
                   COUNT(*) AS total,
                   group_concat(DISTINCT o.status) AS statuses,
                   MIN(o.id) AS first_id
            FROM observations o JOIN products p ON p.id = o.product_id
            WHERE p.site = ?
            GROUP BY o.run_id
            ORDER BY first_id DESC
            LIMIT ?
            """,
            (site, limit),
        )
        return [
            SiteRun(r["run_id"], r["ok"], r["total"], tuple(sorted(r["statuses"].split(","))))
            for r in rows
        ]

    def latest_observations(self) -> list[tuple[ProductRow, Observation]]:
        """The newest observation of every active product that has one, in product order."""
        rows = self.conn.execute(
            """
            SELECT o.* FROM observations o
            JOIN products p ON p.id = o.product_id
            WHERE p.active = 1
              AND o.id = (SELECT MAX(id) FROM observations WHERE product_id = p.id)
            ORDER BY p.id
            """
        )
        return [(self.product(row["product_id"]), self._observation(row)) for row in rows]

    # --- runs and deliveries ------------------------------------------------------------

    def record_run(
        self, run_id: str, started_at: str, channels: list[str], items: list[dict]
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, started_at, channels, items) VALUES (?, ?, ?, ?)",
                (run_id, started_at, ",".join(channels), json.dumps(items)),
            )

    def record_delivery(
        self, run_id: str, channel: str, delivered_in: str, delivered_at: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO deliveries (run_id, channel, delivered_in, delivered_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, channel, delivered_in, delivered_at),
            )

    def pending_runs(self, channel: str, exclude_run: str) -> list[PendingRun]:
        """Earlier runs with digest items that were due on `channel` but never reached it.

        Oldest first. Runs from before the channel was enabled are never pending.
        """
        rows = self.conn.execute(
            """
            SELECT r.* FROM runs r
            WHERE r.items != '[]'
              AND r.run_id != :exclude
              AND instr(',' || r.channels || ',', ',' || :channel || ',') > 0
              AND NOT EXISTS (SELECT 1 FROM deliveries d
                              WHERE d.run_id = r.run_id AND d.channel = :channel)
            ORDER BY r.started_at, r.rowid
            """,
            {"channel": channel, "exclude": exclude_run},
        )
        return [PendingRun(r["run_id"], r["started_at"], json.loads(r["items"])) for r in rows]

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            id=row["id"],
            product_id=row["product_id"],
            run_id=row["run_id"],
            fetched_at=row["fetched_at"],
            status=row["status"],
            title=row["title"],
            price=_dec(row["price"]),
            currency=row["currency"],
            stock_status=row["stock_status"],
            stock_qty=row["stock_qty"],
            error=row["error"],
        )
