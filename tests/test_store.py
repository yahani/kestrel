import sqlite3
from decimal import Decimal

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tracker.config import Config, load_config
from tracker.store import Observation, Store

BASE_CONFIG = load_config(EXAMPLE_CONFIG)
URL_A = "https://books.toscrape.com/catalogue/a/index.html"
URL_B = "https://books.toscrape.com/catalogue/b/index.html"


def config_with(*products: dict) -> Config:
    data = BASE_CONFIG.model_dump(mode="json")
    data["products"] = list(products)
    return Config.model_validate(data)


TWO_PRODUCTS = config_with(
    {"site": "books-sandbox", "url": URL_A, "my_sku": "A-1", "my_price": "10.00"},
    {"site": "books-sandbox", "url": URL_B},
)


def obs(run_id: str = "r1", status: str = "ok", **fields) -> Observation:
    defaults = {"fetched_at": "2026-10-18T06:00:00+00:00", "title": "A", "price": Decimal("9.99"),
                "currency": "GBP", "stock_status": "in_stock", "stock_qty": 5}  # fmt: skip
    if status != "ok":
        defaults = {"fetched_at": defaults["fetched_at"], "error": "HTTP 500"}
    return Observation(run_id=run_id, status=status, **{**defaults, **fields})


@pytest.fixture
def store():
    with Store(sqlite3.connect(":memory:")) as s:
        yield s


def test_sync_products_inserts_in_config_order(store):
    rows = store.sync_products(TWO_PRODUCTS)

    assert [r.url for r in rows] == [URL_A, URL_B]
    assert rows[0].my_sku == "A-1"
    assert rows[0].my_price == Decimal("10.00")
    assert rows[1].my_price is None
    assert all(r.active for r in rows)
    assert store.product(rows[0].id) == rows[0]


def test_sync_products_updates_and_deactivates(store):
    store.sync_products(TWO_PRODUCTS)
    changed = config_with({"site": "books-sandbox", "url": URL_A, "my_price": "12.50"})
    rows = store.sync_products(changed)

    assert [r.url for r in rows] == [URL_A]
    assert rows[0].my_price == Decimal("12.50")
    assert rows[0].my_sku is None
    by_url = {p.url: p for p in store.products()}
    assert by_url[URL_B].active is False  # removed from config, kept with history


def test_sync_products_reactivates_and_keeps_id(store):
    first = store.sync_products(TWO_PRODUCTS)
    store.sync_products(config_with({"site": "books-sandbox", "url": URL_A}))
    again = store.sync_products(TWO_PRODUCTS)
    assert [r.id for r in again] == [r.id for r in first]
    assert all(r.active for r in again)


def test_record_round_trips_exact_decimal(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    saved = store.record(product.id, obs(price=Decimal("1299.10")))

    assert saved.id is not None
    assert saved.product_id == product.id
    assert saved.price == Decimal("1299.10")
    assert str(saved.price) == "1299.10"
    raw = store.conn.execute("SELECT typeof(price), price FROM observations").fetchone()
    assert tuple(raw) == ("text", "1299.10")


def test_failed_observation_is_recorded(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    saved = store.record(product.id, obs(status="http_error", error="HTTP 503"))
    assert saved.status == "http_error"
    assert saved.error == "HTTP 503"
    assert saved.price is None
    assert not saved.ok


def test_invalid_status_is_rejected(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    with pytest.raises(sqlite3.IntegrityError):
        store.record(product.id, obs(status="weird"))


def test_observations_are_append_only(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    store.record(product.id, obs())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE observations SET price = '1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM observations")


def test_previous_and_previous_ok(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    first = store.record(product.id, obs("r1", price=Decimal("5")))
    failed = store.record(product.id, obs("r2", status="http_error"))
    current = store.record(product.id, obs("r3", price=Decimal("6")))

    assert store.previous(first) is None
    assert store.previous(current) == failed
    assert store.previous(current, ok_only=True) == first


def test_previous_ignores_other_products(store):
    a, b = store.sync_products(TWO_PRODUCTS)
    store.record(b.id, obs("r1"))
    current = store.record(a.id, obs("r1"))
    assert store.previous(current) is None


def test_run_observations_and_history(store):
    a, b = store.sync_products(TWO_PRODUCTS)
    store.record(a.id, obs("r1", price=Decimal("1")))
    store.record(a.id, obs("r2", price=Decimal("2")))
    store.record(b.id, obs("r2", price=Decimal("3")))

    assert [o.price for o in store.run_observations("r2")] == [Decimal("2"), Decimal("3")]
    assert [o.price for o in store.history(a.id)] == [Decimal("2"), Decimal("1")]
    assert len(store.history(a.id, limit=1)) == 1


def test_find_products_by_sku_url_or_fragment(store):
    a, b = store.sync_products(TWO_PRODUCTS)
    assert store.find_products("A-1") == [a]
    assert store.find_products(URL_B) == [b]
    assert store.find_products("catalogue/b") == [b]
    assert {p.url for p in store.find_products("catalogue")} == {URL_A, URL_B}
    assert store.find_products("nothing-here") == []


def test_lookups_raise_for_unknown_ids(store):
    with pytest.raises(KeyError):
        store.product(999)
    with pytest.raises(KeyError):
        store.observation(999)


def test_open_creates_file_and_persists(tmp_path):
    path = tmp_path / "data" / "tracker.db"
    with Store.open(path) as s:
        product = s.sync_products(TWO_PRODUCTS)[0]
        s.record(product.id, obs())
    with Store.open(path) as s:
        assert len(s.history(product.id)) == 1
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_scratch_copy_never_touches_disk(tmp_path):
    path = tmp_path / "tracker.db"
    with Store.open(path) as s:
        product = s.sync_products(TWO_PRODUCTS)[0]
        s.record(product.id, obs("r1"))

    with Store.scratch_copy(path) as scratch:
        assert len(scratch.history(product.id)) == 1  # sees existing history
        scratch.record(product.id, obs("dry"))
        assert len(scratch.history(product.id)) == 2

    with Store.open(path) as s:
        assert len(s.history(product.id)) == 1  # disk unchanged


def test_scratch_copy_without_existing_db(tmp_path):
    path = tmp_path / "missing.db"
    with Store.scratch_copy(path) as scratch:
        assert scratch.products() == []
    assert not path.exists()


def test_previous_with_known_stock(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    known = store.record(product.id, obs("r1", stock_status="in_stock"))
    store.record(product.id, obs("r2", stock_status="unknown"))
    store.record(product.id, obs("r3", status="http_error"))
    current = store.record(product.id, obs("r4", stock_status="out_of_stock"))
    assert store.previous(current, ok_only=True, known_stock=True) == known


@pytest.mark.parametrize("query", ["", "   "])
def test_find_products_empty_query_matches_nothing(store, query):
    store.sync_products(TWO_PRODUCTS)
    assert store.find_products(query) == []


def test_decimals_are_stored_in_plain_notation(store):
    product = store.sync_products(TWO_PRODUCTS)[0]
    store.record(product.id, obs(price=Decimal("1E+2")))
    raw = store.conn.execute("SELECT price FROM observations").fetchone()[0]
    assert raw == "100"
