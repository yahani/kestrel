from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import EXAMPLE_CONFIG, FIXTURES
from tracker import cli
from tracker.cli import app
from tracker.config import load_config
from tracker.fetch import Fetcher
from tracker.store import Store

runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "check-config", "test-selectors", "history"):
        assert command in result.output


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "check-config" in result.output


def test_check_config_valid():
    result = runner.invoke(app, ["check-config", "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    assert "sites:    2 (books-sandbox, scraping-dev)" in result.output
    assert "products: 1" in result.output
    assert "email on (1 recipient), slack off, min change 1%" in result.output
    assert "sheets:   off" in result.output


def test_check_config_invalid(example_data, write_config):
    example_data["products"][0]["site"] = "rival-x"
    result = runner.invoke(app, ["check-config", "-c", str(write_config(example_data))])
    assert result.exit_code == 1
    assert "unknown site 'rival-x'" in result.output
    assert "Traceback" not in result.output


def test_check_config_missing_file(tmp_path):
    result = runner.invoke(app, ["check-config", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "config file not found" in result.output


def test_run_rejects_bad_config(tmp_path):
    result = runner.invoke(app, ["run", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "config file not found" in result.output


# --- test-selectors ---------------------------------------------------------------

BOOK_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"


def selectors(*args: str):
    return runner.invoke(app, ["test-selectors", *args, "--config", str(EXAMPLE_CONFIG)])


@pytest.fixture
def fake_site(monkeypatch):
    """Serve fixture pages through a mocked transport, with no real sleeping."""
    pages = {
        "https://books.toscrape.com/robots.txt": httpx.Response(404),
        BOOK_URL: httpx.Response(200, text=(FIXTURES / "books_in_stock.html").read_text()),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return pages.get(str(request.url), httpx.Response(404))

    def make(cfg):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return Fetcher(cfg.fetch, client=client, sleep=lambda _: None)

    monkeypatch.setattr(cli, "_make_fetcher", make)
    return pages


def test_test_selectors_fetches_and_shows_values(fake_site):
    result = selectors(BOOK_URL)
    assert result.exit_code == 0, result.output
    assert "site:   books-sandbox (http)" in result.output
    assert "fetch:  ok · HTTP 200" in result.output
    assert "matched: 'A Light in the Attic'" in result.output
    assert "matched: '£51.77'" in result.output
    assert "value:   51.77 GBP" in result.output
    assert "value:   in_stock, qty 22" in result.output
    assert "✓ parsed ok" in result.output


def test_test_selectors_save_writes_fixture(fake_site, tmp_path):
    target = tmp_path / "saved" / "page.html"
    result = selectors(BOOK_URL, "--save", str(target))
    assert result.exit_code == 0, result.output
    assert target.read_text() == (FIXTURES / "books_in_stock.html").read_text()


def test_test_selectors_from_saved_html_needs_no_network():
    fixture = FIXTURES / "scrapingdev_product_1.html"
    result = selectors("https://web-scraping.dev/product/1", "--html", str(fixture))
    assert result.exit_code == 0, result.output
    assert "fetch:  skipped" in result.output
    assert "value:   9.99 USD" in result.output


def test_test_selectors_reports_parse_error():
    fixture = FIXTURES / "books_layout_changed.html"
    result = selectors(BOOK_URL, "--html", str(fixture))
    assert result.exit_code == 1
    assert "matched: (no match)" in result.output
    assert "parse_error: price selector 'div.product_main p.price_color' matched nothing" in (
        result.output
    )


def test_test_selectors_reports_fetch_failure(fake_site):
    fake_site[BOOK_URL] = httpx.Response(403)
    result = selectors(BOOK_URL)
    assert result.exit_code == 1
    assert "fetch:  blocked · HTTP 403" in result.output
    assert "✗ HTTP 403" in result.output


def test_test_selectors_unknown_site():
    result = selectors("https://unknown.example/item")
    assert result.exit_code == 1
    assert "no configured site matches https://unknown.example/item" in result.output
    assert "books-sandbox" in result.output


def test_make_fetcher_uses_config_user_agent():
    cfg = load_config(EXAMPLE_CONFIG)
    with cli._make_fetcher(cfg) as fetcher:
        assert fetcher.user_agent == cfg.fetch.user_agent


# --- run and history over the mock shop ---------------------------------------------


@pytest.fixture
def shop_cli(mock_shop, mock_config_file, monkeypatch):
    monkeypatch.setattr(cli, "_make_fetcher", mock_shop.fetcher)
    return mock_shop, mock_config_file


def invoke(*args):
    return runner.invoke(app, [str(a) for a in args])


def test_run_prints_summary_and_changes(shop_cli):
    shop, config = shop_cli
    first = invoke("run", "-c", config)
    assert first.exit_code == 0, first.output
    assert "4 products, 4 ok, 0 failed, 0 changes" in first.output
    assert f"saved to {config.parent / 'tracker.db'}" in first.output

    shop.set("blue-mug", price="19.50")
    shop.overrides["/product/red-mug"] = httpx.Response(404)
    second = invoke("run", "-c", config)
    assert second.exit_code == 0, second.output
    assert "4 products, 3 ok, 1 failed, 2 changes" in second.output
    assert "• rival-x cut Blue Mug from 24.00 USD to 19.50 USD (-18.8%), below your 23.00 USD" in (
        second.output
    )
    assert "✗ http://127.0.0.1:8000/product/red-mug: http_error (HTTP 404)" in second.output


def test_dry_run_saves_nothing(shop_cli):
    shop, config = shop_cli
    invoke("run", "-c", config)
    shop.set("blue-mug", price="19.50")

    dry = invoke("run", "-c", config, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "1 change" in dry.output
    assert "(dry run: nothing was saved)" in dry.output

    # The real run still sees the change, so the dry run didn't record anything.
    assert "1 change" in invoke("run", "-c", config).output


def test_dry_run_without_database_creates_none(shop_cli):
    _, config = shop_cli
    assert invoke("run", "-c", config, "--dry-run").exit_code == 0
    assert not (config.parent / "tracker.db").exists()


def test_history_shows_newest_first(shop_cli):
    shop, config = shop_cli
    invoke("run", "-c", config)
    shop.set("blue-mug", price="19.50", stock=0)
    invoke("run", "-c", config)

    result = invoke("history", "MUG-BLUE", "-c", config)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "http://127.0.0.1:8000/product/blue-mug"
    assert lines[1] == "site rival-x, sku MUG-BLUE, my price 23.0"
    rows = [line for line in lines if line[:4].isdigit()]  # rows start with the timestamp
    assert len(rows) == 2
    assert "19.50 USD" in rows[0] and "out_of_stock" in rows[0]
    assert "24.00 USD" in rows[1] and "in_stock (12)" in rows[1]
    assert "Blue Mug" in rows[1]


def test_history_limit_and_url_fragment(shop_cli):
    _, config = shop_cli
    invoke("run", "-c", config)
    invoke("run", "-c", config)
    result = invoke("history", "green-teapot", "-c", config, "--limit", "1")
    assert result.exit_code == 0, result.output
    assert sum(1 for line in result.output.splitlines() if " ok " in line) == 1


def test_history_shows_errors(shop_cli):
    shop, config = shop_cli
    shop.overrides["/product/red-mug"] = httpx.Response(404)
    invoke("run", "-c", config)
    result = invoke("history", "MUG-RED", "-c", config)
    assert "http_error" in result.output
    assert "HTTP 404" in result.output


def test_history_ambiguous(shop_cli):
    _, config = shop_cli
    invoke("run", "-c", config)
    result = invoke("history", "mug", "-c", config)
    assert result.exit_code == 1
    assert "2 products match 'mug'; be more specific" in result.output
    assert "MUG-BLUE" in result.output and "MUG-RED" in result.output


def test_history_not_found(shop_cli):
    _, config = shop_cli
    invoke("run", "-c", config)
    result = invoke("history", "nothing", "-c", config)
    assert result.exit_code == 1
    assert "no product matches 'nothing'" in result.output


def test_history_empty_query(shop_cli):
    _, config = shop_cli
    result = invoke("history", "  ", "-c", config)
    assert result.exit_code == 1
    assert "give a product URL, SKU or part of a URL" in result.output


def test_history_before_any_run(shop_cli):
    _, config = shop_cli
    result = invoke("history", "MUG-BLUE", "-c", config)
    assert result.exit_code == 1
    assert "no history yet" in result.output
    assert not (config.parent / "tracker.db").exists()


def test_history_product_without_observations(shop_cli):
    _, config = shop_cli
    with Store.open(config.parent / "tracker.db") as store:
        store.sync_products(load_config(config))
    result = invoke("history", "MUG-BLUE", "-c", config)
    assert result.exit_code == 0
    assert "no observations yet" in result.output


def test_history_marks_products_removed_from_config(shop_cli):
    _, config = shop_cli
    invoke("run", "-c", config)
    data = yaml.safe_load(config.read_text())
    data["products"] = [p for p in data["products"] if p["my_sku"] != "MUG-RED"]
    config.write_text(yaml.safe_dump(data, sort_keys=False))
    invoke("run", "-c", config)

    result = invoke("history", "MUG-RED", "-c", config)
    assert result.exit_code == 0
    assert "(no longer in config)" in result.output


def test_relative_database_path_is_next_to_config(tmp_path):
    cfg = load_config(EXAMPLE_CONFIG)
    assert cli._db_path(tmp_path / "c.yaml", cfg) == tmp_path / "tracker.db"
    absolute = cfg.model_copy(update={"database": str(tmp_path / "x" / "t.db")})
    assert cli._db_path(Path("elsewhere/c.yaml"), absolute) == tmp_path / "x" / "t.db"
