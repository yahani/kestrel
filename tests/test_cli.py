import os
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
    assert "4 products checked: 4 ok, 0 failed, 0 changes" in first.output
    assert "digest: not sent (nothing changed)" in first.output
    assert f"saved to {config.parent / 'tracker.db'}" in first.output

    shop.set("blue-mug", price="19.50")
    shop.overrides["/product/red-mug"] = httpx.Response(404)
    second = invoke("run", "-c", config)
    assert second.exit_code == 0, second.output
    assert "4 products checked: 3 ok, 1 failed, 2 changes" in second.output
    assert "• rival-x cut Blue Mug from $24.00 to $19.50 (-18.8%), below your $23.00" in (
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
    assert "(dry run: nothing saved, no digest sent, Sheets not updated)" in dry.output
    assert "--- digest preview ---" in dry.output
    assert "UNDERCUTTING YOU" in dry.output

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
    assert "$19.50" in rows[0] and "out_of_stock" in rows[0]
    assert "$24.00" in rows[1] and "in_stock (12)" in rows[1]
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


# --- delivery from `run` (session 4) -------------------------------------------------

SMTP_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USER": "tracker@example.com",
    "SMTP_PASSWORD": "app-password",
}


def edit_config(path, **changes):
    data = yaml.safe_load(path.read_text())
    for dotted, value in changes.items():
        target = data
        *parents, leaf = dotted.split("__")
        for key in parents:
            target = target.setdefault(key, {})
        target[leaf] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class RecordingSMTP:
    def __init__(self):
        self.sent = []

    def __call__(self, settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def email_cli(shop_cli, monkeypatch):
    shop, config = shop_cli
    edit_config(config, alerts__email={"enabled": True, "to": ["owner@example.com"]})
    smtp = RecordingSMTP()
    monkeypatch.setattr(cli, "_smtp_connect", smtp)
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    return shop, config, smtp


def test_run_emails_one_digest_when_something_changed(email_cli):
    shop, config, smtp = email_cli
    first = invoke("run", "-c", config)
    assert first.exit_code == 0, first.output
    assert smtp.sent == []  # first run: nothing changed, nothing sent

    for change in ({"slug": "blue-mug", "price": "19.50"},
                   {"slug": "green-teapot", "price": "49.50"},
                   {"slug": "red-mug", "stock": 0}):  # fmt: skip
        shop.set(**change)
    second = invoke("run", "-c", config)
    assert second.exit_code == 0, second.output
    assert "email: ✓ sent to owner@example.com" in second.output
    [message] = smtp.sent
    assert message["Subject"].endswith(": 3 changes, 1 undercut")
    body = message.get_body(("plain",)).get_content()
    assert body.count("\n- ") == 3

    third = invoke("run", "-c", config)
    assert "digest: not sent (nothing changed)" in third.output
    assert len(smtp.sent) == 1  # running again with no changes sends nothing


def test_always_send_summary_sends_even_without_changes(email_cli):
    _, config, smtp = email_cli
    edit_config(config, alerts__always_send_summary=True)
    invoke("run", "-c", config)
    [message] = smtp.sent
    assert message["Subject"].endswith(": no changes")


def test_run_exits_1_when_email_cannot_be_sent(email_cli, monkeypatch):
    shop, config, _smtp = email_cli
    invoke("run", "-c", config)
    monkeypatch.delenv("SMTP_HOST")
    shop.set("blue-mug", price="19.50")
    result = invoke("run", "-c", config)
    assert result.exit_code == 1
    assert "email: ✗ email is enabled but SMTP_HOST not set" in result.output
    assert "saved to" in result.output  # observations are still stored


def test_dry_run_never_sends(email_cli):
    shop, config, smtp = email_cli
    invoke("run", "-c", config)
    shop.set("blue-mug", price="19.50")
    invoke("run", "-c", config, "--dry-run")
    assert smtp.sent == []


def test_dry_run_preview_when_nothing_would_be_sent(shop_cli):
    _, config = shop_cli
    result = invoke("run", "-c", config, "--dry-run")
    assert "digest: would not be sent (nothing changed)" in result.output


def test_run_with_no_channel_enabled(shop_cli):
    shop, config = shop_cli
    invoke("run", "-c", config)
    shop.set("blue-mug", price="19.50")
    result = invoke("run", "-c", config)
    assert result.exit_code == 0
    assert "digest: no channel enabled (alerts.email or alerts.slack)" in result.output


def test_run_reports_failing_site(shop_cli):
    shop, config = shop_cli
    for slug in ("blue-mug", "red-mug", "green-teapot", "coaster-set"):
        shop.overrides[f"/product/{slug}"] = httpx.Response(200, text="<html></html>")
    invoke("run", "-c", config)
    result = invoke("run", "-c", config)
    assert "! rival-x failed 2 runs in a row (parse_error)" in result.output


def test_run_exports_to_sheets(shop_cli, monkeypatch, tmp_path):
    from tests.test_sheets import FakeSpreadsheet

    _, config = shop_cli
    key = tmp_path / "key.json"
    key.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(key))
    edit_config(config, sheets={"enabled": True, "spreadsheet_id": "abc"})
    sheet = FakeSpreadsheet()
    monkeypatch.setattr(cli, "_open_spreadsheet", lambda path, sheet_id: sheet)

    result = invoke("run", "-c", config)
    assert result.exit_code == 0, result.output
    assert "sheets: ✓ 4 rows added, 4 in Latest" in result.output
    assert len(sheet.tabs["Latest"].rows) == 5


def test_run_exits_1_when_sheets_fails(shop_cli):
    _, config = shop_cli
    edit_config(config, sheets={"enabled": True, "spreadsheet_id": "abc"})
    result = invoke("run", "-c", config)
    assert result.exit_code == 1
    assert "sheets: ✗ sheets is enabled but GOOGLE_SERVICE_ACCOUNT_JSON is not set" in result.output


def test_check_config_warns_about_missing_secrets(shop_cli):
    _, config = shop_cli
    edit_config(
        config,
        alerts__email={"enabled": True, "to": ["a@example.com"]},
        sheets={"enabled": True, "spreadsheet_id": "abc"},
    )
    result = invoke("check-config", "-c", config)
    assert result.exit_code == 0  # the file itself is valid
    assert "! email is enabled but SMTP_HOST, SMTP_FROM (or SMTP_USER) not set" in result.output
    assert "! sheets is enabled but GOOGLE_SERVICE_ACCOUNT_JSON is not set" in result.output


def test_check_config_no_warnings_when_secrets_set(email_cli):
    _, config, _ = email_cli
    assert "!" not in invoke("check-config", "-c", config).output


def test_load_env_reads_dotenv_next_to_config(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    monkeypatch.setattr(cli, "load_dotenv", load_dotenv)  # the real one, for this test only
    monkeypatch.chdir(tmp_path)
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / ".env").write_text("SMTP_HOST=from-config-dir\nSMTP_PASSWORD=file\n")
    (tmp_path / ".env").write_text("SMTP_HOST=from-cwd\nSMTP_USER=cwd-user\n")
    monkeypatch.setenv("SMTP_PASSWORD", "real-env-wins")
    # Registered so monkeypatch removes whatever load_dotenv sets when the test ends.
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.delenv("SMTP_HOST")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.delenv("SMTP_USER")

    cli._load_env(tmp_path / "conf" / "config.yaml")

    assert os.environ["SMTP_HOST"] == "from-config-dir"  # config folder first
    assert os.environ["SMTP_USER"] == "cwd-user"  # then the current folder
    assert os.environ["SMTP_PASSWORD"] == "real-env-wins"  # never overrides real env
