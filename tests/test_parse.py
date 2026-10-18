from decimal import Decimal

import pytest

from tests.conftest import EXAMPLE_CONFIG, FIXTURES
from tracker.config import load_config
from tracker.parse import detect_currency, normalise_price, normalise_stock, parse_page

CONFIG = load_config(EXAMPLE_CONFIG)
BOOKS = CONFIG.site("books-sandbox")
SCRAPING_DEV = CONFIG.site("scraping-dev")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- price normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("$1,299.00", "1299.00", None),
        ("£51.77", "51.77", "GBP"),
        ("1.299,00 €", "1299.00", "EUR"),
        ("1\u00a0299,00 €", "1299.00", "EUR"),  # non-breaking space thousands separator
        ("1\u202f299,00 €", "1299.00", "EUR"),  # narrow no-break space (French locale)
        ("From $9", "9", None),
        ("USD 12.50", "12.50", "USD"),
        ("12.50 EUR", "12.50", "EUR"),
        ("¥1,299", "1299", "JPY"),
        ("₹1,23,456.00", "123456.00", "INR"),
        ("Rs. 500", "500", None),
        ("LKR 4,500.00", "4500.00", "LKR"),
        ("$19.99 \u2013 $24.99", "19.99", None),
        ("Was $24.00 Now $19.50", "19.50", None),
        ("12.5", "12.5", None),
        ("12,50 €", "12.50", "EUR"),
        ("CHF 1'299.50", "1299.50", "CHF"),
        ("US$ 45", "45", "USD"),
        ("C$ 45.00", "45.00", "CAD"),
        ("R$ 1.299,90", "1299.90", "BRL"),
        ("1.234.567", "1234567", None),
        ("£51.77 ", "51.77", "GBP"),
        ("  $ 9.99\n", "9.99", None),
    ],
)
def test_normalise_price(text, amount, currency):
    assert normalise_price(text) == (Decimal(amount), currency)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_normalise_price_empty(text):
    assert normalise_price(text) == (None, None)


def test_normalise_price_without_digits_keeps_currency():
    assert normalise_price("Price on request £") == (None, "GBP")


def test_normalise_price_returns_exact_decimal():
    amount, _ = normalise_price("$0.10")
    assert amount == Decimal("0.10")
    assert str(amount) == "0.10"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("USD 5", "USD"), ("5 GBP", "GBP"), ("OFF 20%", None), ("$5", None), ("€5", "EUR")],
)
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected


# --- stock normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "status", "qty"),
    [
        ("In stock (22 available)", "in_stock", 22),
        ("In Stock", "in_stock", None),
        ("in stock", "in_stock", None),
        ("Only 3 left!", "in_stock", 3),
        ("Low stock - 2 left", "in_stock", 2),
        ("5 in stock", "in_stock", 5),
        ("Available", "in_stock", None),
        ("Add to Cart", "in_stock", None),
        ("Add to basket", "in_stock", None),
        ("Out of stock", "out_of_stock", None),
        ("OUT OF STOCK", "out_of_stock", None),
        ("Sold out", "out_of_stock", None),
        ("Currently unavailable", "out_of_stock", None),
        ("Not in stock", "out_of_stock", None),
        ("0 available", "out_of_stock", 0),
        ("Out of stock (0 available)", "out_of_stock", 0),
        ("Available for pre-order", "unknown", None),
        ("Backorder", "unknown", None),
        ("Coming soon", "unknown", None),
        ("Ships in 2-3 weeks", "unknown", None),
        ("", "unknown", None),
        (None, "unknown", None),
    ],
)
def test_normalise_stock(text, status, qty):
    assert normalise_stock(text) == (status, qty)


def test_normalise_stock_collapses_whitespace():
    assert normalise_stock("\n   In stock\n   (22\n available)\n") == ("in_stock", 22)


# --- whole-page parsing on saved fixtures ----------------------------------------


@pytest.mark.parametrize(
    ("name", "site", "title", "price", "currency", "stock", "qty"),
    [
        ("books_in_stock.html", BOOKS, "A Light in the Attic", "51.77", "GBP", "in_stock", 22),
        ("books_sharp_objects.html", BOOKS, "Sharp Objects", "47.82", "GBP", "in_stock", 20),
        ("books_out_of_stock.html", BOOKS, "A Light in the Attic", "51.77", "GBP",
         "out_of_stock", None),
        ("scrapingdev_product_1.html", SCRAPING_DEV, "Box of Chocolate Candy", "9.99", "USD",
         "in_stock", None),
        ("scrapingdev_product_2.html", SCRAPING_DEV, "Dark Red Energy Potion", "4.99", "USD",
         "in_stock", None),
    ],
)  # fmt: skip
def test_parse_fixture(name, site, title, price, currency, stock, qty):
    page = parse_page(fixture(name), site)

    assert page.status == "ok", page.error
    assert page.error is None
    assert page.title == title
    assert page.price == Decimal(price)
    assert page.currency == currency
    assert page.stock_status == stock
    assert page.stock_qty == qty


def test_layout_change_is_a_parse_error_naming_the_selector():
    page = parse_page(fixture("books_layout_changed.html"), BOOKS)

    assert page.status == "parse_error"
    assert page.error == "price selector 'div.product_main p.price_color' matched nothing"
    assert page.title == "A Light in the Attic"  # what still matched is kept
    assert page.raw["price"] is None


def test_wrong_site_selectors_fail_on_title_and_price():
    page = parse_page(fixture("books_in_stock.html"), SCRAPING_DEV)
    assert page.status == "parse_error"
    assert "title selector" in page.error
    assert "price selector" in page.error


def test_missing_stock_element_is_unknown_not_an_error():
    html = '<div class="product_main"><h1>X</h1><p class="price_color">£5.00</p></div>'
    page = parse_page(html, BOOKS)
    assert page.status == "ok"
    assert page.stock_status == "unknown"


def test_unreadable_price_is_a_parse_error():
    html = '<div class="product_main"><h1>X</h1><p class="price_color">Call us</p></div>'
    page = parse_page(html, BOOKS)
    assert page.status == "parse_error"
    assert page.error == "could not read a price from 'Call us'"


def test_site_currency_used_when_symbol_ambiguous():
    html = '<div class="product_main"><h1>X</h1><p class="price_color">$5.00</p></div>'
    assert parse_page(html, BOOKS).currency == "GBP"


def test_detected_currency_overrides_site_currency(caplog):
    html = '<div class="product_main"><h1>X</h1><p class="price_color">€5.00</p></div>'
    page = parse_page(html, BOOKS)
    assert page.currency == "EUR"
    assert "looks like EUR but site currency is GBP" in caplog.text


def test_raw_text_is_recorded_for_each_selector():
    page = parse_page(fixture("books_in_stock.html"), BOOKS)
    assert page.raw == {
        "title": "A Light in the Attic",
        "price": "£51.77",
        "stock": "In stock (22 available)",
    }


def test_garbled_number_is_not_a_price():
    assert normalise_price("1,2.3,4") == (None, None)
