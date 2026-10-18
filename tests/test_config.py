from decimal import Decimal

import pytest

from tests.conftest import EXAMPLE_CONFIG
from tracker.config import MAX_PRODUCTS, MAX_SITES, ConfigError, load_config


def error_for(path) -> str:
    with pytest.raises(ConfigError) as exc_info:
        load_config(path)
    return str(exc_info.value)


def test_example_config_loads():
    config = load_config(EXAMPLE_CONFIG)

    assert [s.name for s in config.sites] == ["books-sandbox", "scraping-dev"]
    assert config.fetch.contact == "yahanihwgm@gmail.com"
    assert config.fetch.user_agent.startswith("price-tracker/")
    assert "mailto:yahanihwgm@gmail.com" in config.fetch.user_agent
    site = config.site("books-sandbox")
    assert site.fetch_mode == "http"
    assert site.delay_seconds == 5
    assert site.currency == "GBP"
    assert site.selectors.price == "div.product_main p.price_color"
    assert len(config.products) == 1
    assert config.alerts.email.enabled is True
    assert config.alerts.email.to == ["owner@example.com"]
    assert config.alerts.slack.enabled is False
    assert config.sheets.enabled is False


def test_my_price_is_exact_decimal():
    config = load_config(EXAMPLE_CONFIG)
    price = config.products[0].my_price
    assert isinstance(price, Decimal)
    assert price == Decimal("49.99")


def test_defaults_for_optional_sections(example_data, write_config):
    del example_data["alerts"], example_data["sheets"]
    del example_data["sites"][0]["fetch_mode"], example_data["sites"][0]["delay_seconds"]
    config = load_config(write_config(example_data))

    assert config.sites[0].fetch_mode == "http"
    assert config.sites[0].delay_seconds == 5
    assert config.alerts.min_change_percent == Decimal("1")
    assert config.alerts.always_send_summary is False
    assert config.alerts.email.enabled is False
    assert config.sheets.enabled is False


def test_lowercase_currency_is_normalised(example_data, write_config):
    example_data["sites"][0]["currency"] = "usd"
    assert load_config(write_config(example_data)).sites[0].currency == "USD"


def test_missing_file(tmp_path):
    assert "config file not found" in error_for(tmp_path / "nope.yaml")


def test_malformed_yaml_reports_line(write_config):
    msg = error_for(write_config("sites:\n  - name: a\n    base_url: [unclosed\n"))
    assert "invalid YAML" in msg
    assert "line" in msg


def test_top_level_must_be_mapping(write_config):
    assert "expected a mapping" in error_for(write_config("- just\n- a list\n"))


def test_empty_file(write_config):
    assert "expected a mapping" in error_for(write_config(""))


def test_missing_required_field_named(example_data, write_config):
    del example_data["sites"][0]["selectors"]["price"]
    assert "sites[0].selectors.price: missing required field" in error_for(
        write_config(example_data)
    )


def test_wrong_type_named(example_data, write_config):
    example_data["sites"][0]["delay_seconds"] = "slow"
    assert "sites[0].delay_seconds:" in error_for(write_config(example_data))


def test_bad_fetch_mode(example_data, write_config):
    example_data["sites"][0]["fetch_mode"] = "chrome"
    msg = error_for(write_config(example_data))
    assert "sites[0].fetch_mode:" in msg
    assert "'chrome'" in msg


def test_delay_below_minimum(example_data, write_config):
    example_data["sites"][0]["delay_seconds"] = 1
    assert "greater than or equal to 5" in error_for(write_config(example_data))


def test_bad_currency(example_data, write_config):
    example_data["sites"][0]["currency"] = "pounds"
    assert "sites[0].currency: must be a 3-letter ISO currency code" in error_for(
        write_config(example_data)
    )


def test_bad_url(example_data, write_config):
    example_data["products"][0]["url"] = "not a url"
    assert "products[0].url:" in error_for(write_config(example_data))


def test_unknown_field_is_flagged(example_data, write_config):
    example_data["sites"][0]["selector"] = {}
    assert "sites[0].selector: unknown field" in error_for(write_config(example_data))


def test_negative_my_price(example_data, write_config):
    example_data["products"][0]["my_price"] = -1
    assert "products[0].my_price:" in error_for(write_config(example_data))


def test_unknown_product_site(example_data, write_config):
    example_data["products"][0]["site"] = "rival-x"
    msg = error_for(write_config(example_data))
    assert "products[0].site: unknown site 'rival-x' (known: books-sandbox, scraping-dev)" in msg


def test_product_host_must_match_site(example_data, write_config):
    example_data["products"][0]["url"] = "https://elsewhere.com/item"
    assert "does not match site 'books-sandbox'" in error_for(write_config(example_data))


def test_www_prefix_is_ignored_for_host_match(example_data, write_config):
    example_data["products"][0]["url"] = "https://www.books.toscrape.com/item"
    load_config(write_config(example_data))


def test_duplicate_product_url(example_data, write_config):
    example_data["products"].append(dict(example_data["products"][0]))
    assert "products[1].url: duplicate of products[0].url" in error_for(write_config(example_data))


def test_duplicate_site_name(example_data, write_config):
    example_data["sites"].append(dict(example_data["sites"][0]))
    assert "sites[2].name: duplicate site name" in error_for(write_config(example_data))


def test_too_many_sites(example_data, write_config):
    site = example_data["sites"][0]
    example_data["sites"] = [dict(site, name=f"s{i}") for i in range(MAX_SITES + 1)]
    assert "sites:" in error_for(write_config(example_data))


def test_too_many_products(example_data, write_config):
    base = "https://books.toscrape.com/p/"
    example_data["products"] = [
        {"site": "books-sandbox", "url": f"{base}{i}"} for i in range(MAX_PRODUCTS + 1)
    ]
    assert "products:" in error_for(write_config(example_data))


def test_email_enabled_needs_recipients(example_data, write_config):
    example_data["alerts"]["email"]["to"] = []
    assert "alerts.email: 'to' must list at least one address" in error_for(
        write_config(example_data)
    )


def test_invalid_email_address(example_data, write_config):
    example_data["alerts"]["email"]["to"] = ["not-an-email"]
    assert "not a valid email address: not-an-email" in error_for(write_config(example_data))


def test_sheets_enabled_needs_id(example_data, write_config):
    example_data["sheets"]["enabled"] = True
    assert "sheets: 'spreadsheet_id' is required" in error_for(write_config(example_data))


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("email", "password"),
        ("email", "smtp_password"),
        ("slack", "webhook_url"),
        ("slack", "token"),
    ],
)
def test_secrets_in_config_are_rejected(example_data, write_config, section, key):
    example_data["alerts"][section][key] = "hunter2"
    msg = error_for(write_config(example_data))
    assert "secrets must come from environment variables" in msg
    assert f"alerts.{section}.{key}" in msg
    assert "hunter2" not in msg


def test_error_message_names_the_file(example_data, write_config):
    example_data["sites"][0]["fetch_mode"] = "chrome"
    path = write_config(example_data)
    assert error_for(path).startswith(str(path))


def test_fetch_section_is_required(example_data, write_config):
    del example_data["fetch"]
    assert "fetch: missing required field" in error_for(write_config(example_data))


def test_fetch_contact_must_be_email(example_data, write_config):
    example_data["fetch"]["contact"] = "call me"
    assert "fetch.contact: must be an email address" in error_for(write_config(example_data))


def test_fetch_timeout_bounds(example_data, write_config):
    example_data["fetch"]["timeout_seconds"] = 0
    assert "fetch.timeout_seconds:" in error_for(write_config(example_data))


def test_invalid_css_selector(example_data, write_config):
    example_data["sites"][0]["selectors"]["price"] = "p.price[["
    assert "sites[0].selectors.price: invalid CSS selector 'p.price[['" in error_for(
        write_config(example_data)
    )


def test_site_for_url():
    config = load_config(EXAMPLE_CONFIG)
    assert config.site_for_url("https://books.toscrape.com/catalogue/x").name == "books-sandbox"
    assert config.site_for_url("https://www.web-scraping.dev/product/9").name == "scraping-dev"
    assert config.site_for_url("https://unknown.example/x") is None


def test_logs_defaults_and_bounds(example_data, write_config):
    config = load_config(EXAMPLE_CONFIG)
    assert (config.logs.dir, config.logs.keep_days) == ("logs", 30)
    example_data["logs"] = {"keep_days": 0}
    assert "logs.keep_days:" in error_for(write_config(example_data))
