import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import EXAMPLE_CONFIG, FIXTURES
from tracker import cli
from tracker.cli import app
from tracker.config import load_config
from tracker.fetch import Fetcher

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


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--dry-run"],
        ["history", "BOOK-001"],
    ],
)
def test_unbuilt_commands_exit_nonzero(args):
    result = runner.invoke(app, [*args, "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "Not implemented yet" in result.output


def test_unbuilt_commands_still_validate_config(tmp_path):
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
