from decimal import Decimal

import pytest

from tracker.changes import detect_changes, format_money
from tracker.store import Observation, ProductRow

THRESHOLD = Decimal("1")
PRODUCT = ProductRow(
    id=1, site="rival-x", url="http://shop/blue-mug", my_sku="MUG-B", my_price=None, active=True
)
UNDERCUT_PRODUCT = ProductRow(
    id=1,
    site="rival-x",
    url="http://shop/blue-mug",
    my_sku="MUG-B",
    my_price=Decimal("22.00"),
    active=True,
)


def ok(price="24.00", stock="in_stock", title="Blue Mug", currency="USD", run="r") -> Observation:
    return Observation(
        run_id=run,
        fetched_at="2026-10-18T06:00:00+00:00",
        status="ok",
        title=title,
        price=Decimal(price) if price is not None else None,
        currency=currency,
        stock_status=stock,
    )


def failed(status="http_error", error="HTTP 503") -> Observation:
    return Observation(
        run_id="r", fetched_at="2026-10-18T06:00:00+00:00", status=status, error=error
    )


def detect(current, previous_ok, previous="same", product=PRODUCT, threshold=THRESHOLD):
    if previous == "same":
        previous = previous_ok
    return detect_changes(product, current, previous, previous_ok, threshold)


def kinds(changes) -> list[str]:
    return [c.kind for c in changes]


def test_first_observation_has_no_changes():
    assert detect(ok(), previous_ok=None) == []


def test_first_observation_undercutting_has_no_changes():
    assert detect(ok("10.00"), previous_ok=None, product=UNDERCUT_PRODUCT) == []


def test_nothing_changed():
    assert detect(ok(), ok()) == []


def test_price_down():
    [change] = detect(ok("19.50"), ok("24.00"))
    assert change.kind == "price_down"
    assert change.old_price == Decimal("24.00")
    assert change.new_price == Decimal("19.50")
    assert change.percent == Decimal("-18.8")
    assert change.currency == "USD"
    assert change.undercut is False
    assert change.describe() == "rival-x cut Blue Mug from $24.00 to $19.50 (-18.8%)"


def test_price_up():
    [change] = detect(ok("26.40"), ok("24.00"))
    assert change.kind == "price_up"
    assert change.percent == Decimal("10.0")
    assert change.describe() == "rival-x raised Blue Mug from $24.00 to $26.40 (+10.0%)"


def test_price_change_below_threshold_is_ignored():
    assert detect(ok("23.80"), ok("24.00")) == []  # -0.83%


def test_price_change_exactly_at_threshold_counts():
    [change] = detect(ok("99.00"), ok("100.00"))
    assert change.percent == Decimal("-1.0")


def test_threshold_is_configurable():
    assert detect(ok("22.00"), ok("24.00"), threshold=Decimal("10")) == []  # -8.3%
    assert kinds(detect(ok("22.00"), ok("24.00"), threshold=Decimal("0"))) == ["price_down"]


def test_price_from_zero_counts_as_change():
    [change] = detect(ok("5.00"), ok("0.00"))
    assert change.kind == "price_up"
    assert change.percent == Decimal("100.0")


def test_out_of_stock():
    [change] = detect(ok(stock="out_of_stock"), ok(stock="in_stock"))
    assert change.kind == "out_of_stock"
    assert change.describe() == "rival-x is out of stock on Blue Mug"


def test_back_in_stock():
    [change] = detect(ok(stock="in_stock"), ok(stock="out_of_stock"))
    assert change.kind == "back_in_stock"
    assert change.describe() == "rival-x is back in stock on Blue Mug"


@pytest.mark.parametrize(
    ("old", "new"),
    [("unknown", "out_of_stock"), ("in_stock", "unknown"), ("unknown", "in_stock")],
)
def test_unknown_stock_transitions_are_ignored(old, new):
    assert detect(ok(stock=new), ok(stock=old)) == []


def test_title_change():
    [change] = detect(ok(title="Blue Mug XL"), ok(title="Blue Mug"))
    assert change.kind == "title_changed"
    assert change.describe() == "rival-x renamed 'Blue Mug' to 'Blue Mug XL'"


def test_several_changes_at_once():
    changes = detect(ok("19.50", stock="out_of_stock"), ok("24.00", stock="in_stock"))
    assert kinds(changes) == ["price_down", "out_of_stock"]


def test_compares_with_last_successful_not_last_failed():
    # r1 ok at 24.00, r2 failed, r3 ok at 19.50: compare r3 with r1.
    changes = detect(ok("19.50"), previous_ok=ok("24.00"), previous=failed())
    assert kinds(changes) == ["price_down"]


def test_new_failure():
    [change] = detect(failed("blocked", "HTTP 403"), previous_ok=ok())
    assert change.kind == "new_failure"
    assert change.status == "blocked"
    assert change.title == "Blue Mug"  # from the last good observation
    assert change.describe() == "rival-x: could not check Blue Mug (blocked: HTTP 403)"


def test_failure_on_first_ever_check_is_new():
    [change] = detect(failed("parse_error", "price selector matched nothing"), previous_ok=None)
    assert change.kind == "new_failure"
    assert change.title is None
    assert "http://shop/blue-mug" in change.describe()


def test_continuing_failure_is_not_reported_again():
    assert detect(failed(), previous_ok=ok(), previous=failed()) == []


def test_currency_change_is_reported_not_compared():
    [change] = detect(ok("20.00", currency="EUR"), ok("24.00", currency="USD"))
    assert change.kind == "currency_changed"
    assert change.old_currency == "USD"
    assert change.describe() == "rival-x changed currency on Blue Mug: $24.00 to €20.00"


def test_missing_price_on_either_side_skips_price_checks():
    assert detect(ok(price=None), ok("24.00")) == []
    assert detect(ok("24.00"), ok(price=None)) == []


# --- F7: undercutting my price -------------------------------------------------------


def test_price_drop_below_my_price_is_flagged_on_the_change():
    [change] = detect(ok("19.50"), ok("24.00"), product=UNDERCUT_PRODUCT)
    assert change.kind == "price_down"
    assert change.undercut is True
    assert change.describe().endswith("(-18.8%), below your $22.00")


def test_price_drop_staying_above_my_price_is_not_undercut():
    [change] = detect(ok("23.00"), ok("24.00"), product=UNDERCUT_PRODUCT)
    assert change.undercut is False


def test_small_move_below_my_price_still_flags_undercut():
    product = ProductRow(1, "rival-x", "http://shop/x", None, Decimal("49.95"), True)
    [change] = detect(ok("49.90"), ok("50.00"), product=product)  # -0.2%, under threshold
    assert change.kind == "undercut"
    assert change.describe() == "rival-x now undercuts you on Blue Mug: $49.90 vs your $49.95"


def test_undercut_not_repeated_while_staying_below():
    assert detect(ok("19.50"), ok("19.50"), product=UNDERCUT_PRODUCT) == []
    # Below the threshold and already below my price: nothing new.
    assert detect(ok("19.45"), ok("19.50"), product=UNDERCUT_PRODUCT) == []


def test_price_drop_while_already_below_my_price_keeps_flag():
    [change] = detect(ok("15.00"), ok("19.50"), product=UNDERCUT_PRODUCT)
    assert change.kind == "price_down"
    assert change.undercut is True


def test_matching_my_price_is_not_undercut():
    [change] = detect(ok("22.00"), ok("24.00"), product=UNDERCUT_PRODUCT)
    assert change.undercut is False


# --- edge cases ------------------------------------------------------------------------


def test_stock_compared_with_last_known_stock_status():
    # in_stock -> unknown -> out_of_stock: still an out_of_stock change.
    changes = detect_changes(
        PRODUCT,
        ok(stock="out_of_stock"),
        previous=ok(stock="unknown"),
        previous_ok=ok(stock="unknown"),
        min_change_percent=THRESHOLD,
        previous_known_stock=ok(stock="in_stock"),
    )
    assert kinds(changes) == ["out_of_stock"]


def test_undercut_needs_same_currency_as_my_price():
    # my_price is in the site's currency (USD); a EUR price can't be compared with it.
    old, new = ok("24.00", currency="EUR"), ok("19.50", currency="EUR")
    [change] = detect_changes(UNDERCUT_PRODUCT, new, old, old, THRESHOLD, my_currency="USD")
    assert change.kind == "price_down"
    assert change.undercut is False

    product = ProductRow(1, "rival-x", "http://shop/x", None, Decimal("49.95"), True)
    small_old, small_new = ok("50.00", currency="EUR"), ok("49.90", currency="EUR")
    assert (
        detect_changes(product, small_new, small_old, small_old, THRESHOLD, my_currency="USD") == []
    )


def test_undercut_with_matching_currency():
    old, new = ok("24.00"), ok("19.50")
    [change] = detect_changes(UNDERCUT_PRODUCT, new, old, old, THRESHOLD, my_currency="USD")
    assert change.undercut is True


def test_my_price_shown_with_competitor_precision():
    product = ProductRow(1, "rival-x", "http://shop/x", None, Decimal("23.0"), True)
    [change] = detect(ok("19.50"), ok("24.00"), product=product)
    assert change.describe().endswith("below your $23.00")


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("24.00", "USD", "$24.00"),
        ("51.77", "GBP", "£51.77"),
        ("9.99", "EUR", "€9.99"),
        ("1299", "JPY", "¥1299"),
        ("4500.00", "LKR", "LKR 4500.00"),
        ("12.00", None, "12.00"),
        (None, "USD", "-"),
    ],
)
def test_format_money(amount, currency, expected):
    value = Decimal(amount) if amount is not None else None
    assert format_money(value, currency) == expected


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("23.0", "USD", "$23.00"),
        ("11", "GBP", "£11.00"),
        ("21.5", "EUR", "€21.50"),
        ("49.999", "USD", "$49.999"),  # never rounds away precision
        ("1299", "JPY", "¥1299"),  # no minor units
        ("1299.0", "KRW", "KRW 1299.0"),
        ("12.5", None, "12.5"),
    ],
)
def test_format_money_padded_to_minor_units(amount, currency, expected):
    assert format_money(Decimal(amount), currency, pad=True) == expected
