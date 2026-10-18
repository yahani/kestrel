"""Extract and normalise title, price and stock from product pages (F4)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

from bs4 import BeautifulSoup

from tracker.config import Site

log = logging.getLogger(__name__)

StockStatus = Literal["in_stock", "out_of_stock", "unknown"]

# Longest symbols first so "US$" wins over "$". A bare "$", "Rs" or "kr" is ambiguous
# (USD/CAD/AUD, INR/LKR/PKR, SEK/NOK/DKK), so those fall back to the site's currency.
_SYMBOLS: list[tuple[str, str | None]] = [
    ("US$", "USD"),
    ("CA$", "CAD"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("AU$", "AUD"),
    ("NZ$", "NZD"),
    ("HK$", "HKD"),
    ("S$", "SGD"),
    ("R$", "BRL"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("¥", "JPY"),
    ("₹", "INR"),
    ("₩", "KRW"),
    ("₱", "PHP"),
    ("₺", "TRY"),
    ("zł", "PLN"),
    ("$", None),
]
_ISO_CODES = {
    "USD", "EUR", "GBP", "JPY", "CNY", "INR", "LKR", "PKR", "AUD", "CAD", "NZD", "CHF",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "SGD", "HKD", "KRW", "BRL", "MXN", "ZAR",
    "AED", "SAR", "TRY", "THB", "MYR", "IDR", "PHP",
}  # fmt: skip
_ISO_PATTERN = re.compile(r"\b([A-Z]{3})\b")
# A number, allowing thousands separators. A space only joins groups when exactly three
# digits follow, so "1 299,00" is one number but "19.99 24.99" is two.
_NUMBER_PATTERN = re.compile(r"\d(?:[\d.,'\u2019]|[ \u00a0\u202f](?=\d{3}(?!\d)))*")

# Any currency marker, including ambiguous ones that detect_currency can't map to one code.
_CURRENCY_MARK = "|".join(
    [re.escape(symbol) for symbol, _ in _SYMBOLS]
    + [r"\bRs\.?", r"\bkr\b", *(rf"\b{code}\b" for code in sorted(_ISO_CODES))]
)
_CURRENCY_BEFORE = re.compile(rf"(?:{_CURRENCY_MARK})\s*$")
_CURRENCY_AFTER = re.compile(rf"^\s*(?:{_CURRENCY_MARK})")
# Numbers that are not the price: "15%", "2 for $30", "4.5 stars", "$5 off", "3 left".
_NOT_PRICE_AFTER = re.compile(
    r"^\s*(?:%|for\b|x\b|stars?\b|reviews?\b|ratings?\b|off\b|left\b|in stock\b|available\b)",
    re.IGNORECASE,
)
# Discount amounts and multi-buy counts: "Save $5", "You save 10", "Buy 2", "Get 1 free".
_NOT_PRICE_BEFORE = re.compile(r"\b(?:save|saving|buy|get)\s+(?:\S{1,4}\s*)?$", re.IGNORECASE)

_UNKNOWN_STOCK = re.compile(r"pre-?order|back-?order|coming soon|notify me")
_OUT_OF_STOCK = re.compile(
    r"out of stock|sold out|unavailable|not available|no longer available|not in stock"
)
_QUANTITY = re.compile(r"(\d+)\s*(?:available|left|in stock|units?|items?|pcs)\b|only\s+(\d+)")
# A purchase button counts as in stock; a disabled button with the same text cannot be told
# apart by text alone, so point the stock selector at a real stock label when a site has one.
_IN_STOCK = re.compile(
    r"in stock|available|low stock|limited stock|ships? (?:today|now)"
    r"|add to (?:cart|basket|bag)|buy now"
)


@dataclass
class ParsedPage:
    status: Literal["ok", "parse_error"]
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    stock_status: StockStatus = "unknown"
    stock_qty: int | None = None
    error: str | None = None
    # Raw text each selector matched (None when nothing matched); used by test-selectors.
    raw: dict[str, str | None] = field(default_factory=dict)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _to_decimal(token: str) -> Decimal | None:
    token = re.sub(r"[\s'\u2019]", "", token).rstrip(".,")
    has_comma, has_dot = "," in token, "." in token

    if has_comma and has_dot:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        token = token.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        whole, _, last = token.rpartition(sep)
        if token.count(sep) > 1 or len(last) == 3:
            token = token.replace(sep, "")  # thousands separator: 1,299 or 1.234.567
        else:
            token = f"{whole}.{last}"  # decimal separator: 12,50 or 12.5
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def detect_currency(text: str) -> str | None:
    for code in _ISO_PATTERN.findall(text):
        if code in _ISO_CODES:
            return code
    for symbol, code in _SYMBOLS:
        if symbol in text:
            return code
    return None


def normalise_price(text: str | None) -> tuple[Decimal | None, str | None]:
    """Turn price text into (amount, ISO currency or None).

    Numbers that are clearly not the price are ignored: percentages ("Save 15%"), multi-buy
    counts ("2 for $30"), ratings ("4.5 stars") and discount amounts ("Save $5"). Numbers next
    to a currency symbol or code are preferred over bare ones. If several candidates remain
    ("From $9", "$19.99 - $24.99", "Was $24 Now $19"), the lowest is returned.
    """
    if not text or not text.strip():
        return None, None

    marked: list[Decimal] = []
    bare: list[Decimal] = []
    for match in _NUMBER_PATTERN.finditer(text):
        before, after = text[: match.start()], text[match.end() :]
        if _NOT_PRICE_AFTER.match(after) or _NOT_PRICE_BEFORE.search(before):
            continue
        amount = _to_decimal(match.group())
        if amount is None:
            continue
        if _CURRENCY_BEFORE.search(before) or _CURRENCY_AFTER.match(after):
            marked.append(amount)
        else:
            bare.append(amount)

    candidates = marked or bare
    return (min(candidates) if candidates else None), detect_currency(text)


def normalise_stock(text: str | None) -> tuple[StockStatus, int | None]:
    """Turn stock text into (status, quantity or None)."""
    if not text:
        return "unknown", None
    text = _clean(text).lower()

    if _UNKNOWN_STOCK.search(text):
        return "unknown", None
    if _OUT_OF_STOCK.search(text):
        return "out_of_stock", 0 if re.search(r"\b0\b", text) else None

    qty_match = _QUANTITY.search(text)
    if qty_match:
        qty = int(qty_match.group(1) or qty_match.group(2))
        return ("in_stock" if qty > 0 else "out_of_stock"), qty
    if _IN_STOCK.search(text):
        return "in_stock", None
    return "unknown", None


def parse_page(html: str, site: Site) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")
    raw: dict[str, str | None] = {}
    for name, selector in site.selectors.model_dump().items():
        node = soup.select_one(selector)
        raw[name] = _clean(node.get_text(" ")) if node is not None else None

    page = ParsedPage(status="ok", raw=raw, title=raw["title"])
    page.stock_status, page.stock_qty = normalise_stock(raw["stock"])

    problems = [
        f"{name} selector {getattr(site.selectors, name)!r} matched nothing"
        for name in ("title", "price")
        if not raw[name]
    ]
    if raw["price"]:
        page.price, detected = normalise_price(raw["price"])
        if page.price is None:
            problems.append(f"could not read a price from {raw['price']!r}")
        if detected and detected != site.currency:
            log.warning(
                "site %s: price %r looks like %s but site currency is %s; using %s",
                site.name, raw["price"], detected, site.currency, detected,
            )  # fmt: skip
        page.currency = detected or site.currency

    if problems:
        page.status = "parse_error"
        page.error = "; ".join(problems)
    return page
