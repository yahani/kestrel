"""Compare each observation with the last good one and list what changed (F6, F7)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from tracker.store import Observation, ProductRow

ChangeKind = Literal[
    "price_down",
    "price_up",
    "currency_changed",
    "out_of_stock",
    "back_in_stock",
    "title_changed",
    "undercut",
    "new_failure",
]


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    product: ProductRow
    title: str | None  # latest known title, for display
    old_price: Decimal | None = None
    new_price: Decimal | None = None
    currency: str | None = None
    old_currency: str | None = None  # on currency_changed
    percent: Decimal | None = None  # signed, 1 decimal place
    undercut: bool = False  # on price changes: competitor is now below my_price
    old_title: str | None = None
    new_title: str | None = None
    status: str | None = None  # on new_failure
    error: str | None = None

    def describe(self) -> str:
        name = self.title or self.product.url
        cur = f" {self.currency}" if self.currency else ""
        match self.kind:
            case "price_down" | "price_up":
                verb = "cut" if self.kind == "price_down" else "raised"
                text = (
                    f"{self.product.site} {verb} {name} from {self.old_price}{cur} "
                    f"to {self.new_price}{cur} ({self.percent:+}%)"
                )
                if self.undercut:
                    text += f", below your {self.product.my_price}{cur}"
                return text
            case "currency_changed":
                return (
                    f"{self.product.site} changed currency on {name}: "
                    f"{self.old_price} {self.old_currency} to {self.new_price}{cur}"
                )
            case "out_of_stock":
                return f"{self.product.site} is out of stock on {name}"
            case "back_in_stock":
                return f"{self.product.site} is back in stock on {name}"
            case "title_changed":
                return f"{self.product.site} renamed {self.old_title!r} to {self.new_title!r}"
            case "undercut":
                return (
                    f"{self.product.site} now undercuts you on {name}: "
                    f"{self.new_price}{cur} vs your {self.product.my_price}{cur}"
                )
            case "new_failure":
                return f"{self.product.site}: could not check {name} ({self.status}: {self.error})"
        raise AssertionError(self.kind)  # pragma: no cover


def _percent(old: Decimal, new: Decimal) -> Decimal:
    return ((new - old) / old * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def detect_changes(
    product: ProductRow,
    current: Observation,
    previous: Observation | None,
    previous_ok: Observation | None,
    min_change_percent: Decimal,
    *,
    previous_known_stock: Observation | None = None,
    my_currency: str | None = None,
) -> list[Change]:
    """Changes between `current` and the last successful observation before it.

    `previous` is the observation just before `current` (any status) and is only used to
    tell a new failure from a continuing one. Stock is compared with
    `previous_known_stock` (the last good observation whose stock was in or out of stock)
    when given, so an `unknown` in between doesn't hide a change. `my_currency` is the
    currency `my_price` is in; undercuts are only flagged when the competitor uses it too.
    The first successful observation of a product produces no changes.
    """
    if not current.ok:
        if previous is None or previous.ok:
            title = previous_ok.title if previous_ok else None
            return [
                Change("new_failure", product, title, status=current.status, error=current.error)
            ]
        return []  # still failing; the health report (F12) covers repeat failures

    if previous_ok is None:
        return []

    changes: list[Change] = []
    old, new = previous_ok, current
    title = new.title or old.title
    comparable = my_currency is None or new.currency == my_currency
    my_price = product.my_price if comparable else None

    if old.price is not None and new.price is not None:
        if old.currency != new.currency:
            changes.append(
                Change(
                    "currency_changed",
                    product,
                    title,
                    old_price=old.price,
                    new_price=new.price,
                    currency=new.currency,
                    old_currency=old.currency,
                )
            )
        elif new.price != old.price:
            percent = _percent(old.price, new.price) if old.price else Decimal("100.0")
            if abs(percent) >= min_change_percent:
                changes.append(
                    Change(
                        "price_down" if new.price < old.price else "price_up",
                        product,
                        title,
                        old_price=old.price,
                        new_price=new.price,
                        currency=new.currency,
                        percent=percent,
                        undercut=my_price is not None and new.price < my_price,
                    )
                )

        # F7: a price change already carries the undercut flag. Flag the moment a competitor
        # goes below my price on its own only when the move was under the threshold
        # (e.g. 50.00 -> 49.90 against my 49.95), so one event is never reported twice.
        price_reported = any(c.kind in ("price_down", "price_up") for c in changes)
        if (
            not price_reported
            and my_price is not None
            and old.currency == new.currency
            and new.price < my_price <= old.price
        ):
            changes.append(
                Change(
                    "undercut",
                    product,
                    title,
                    old_price=old.price,
                    new_price=new.price,
                    currency=new.currency,
                )
            )

    old_stock = (previous_known_stock or old).stock_status
    if old_stock == "in_stock" and new.stock_status == "out_of_stock":
        changes.append(Change("out_of_stock", product, title))
    elif old_stock == "out_of_stock" and new.stock_status == "in_stock":
        changes.append(Change("back_in_stock", product, title))

    if old.title and new.title and old.title != new.title:
        changes.append(
            Change("title_changed", product, title, old_title=old.title, new_title=new.title)
        )
    return changes
