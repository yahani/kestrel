"""A tiny competitor shop whose prices and stock change on demand.

State lives in a JSON file that is re-read on every request, so `python -m mock_site set ...`
in another terminal (or the /admin page) changes what the tracker sees on its next run.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

DEFAULT_STATE = Path(__file__).with_name("state.json")
SHOP_NAME = "Rival X Homeware"


@dataclass
class Item:
    slug: str
    title: str
    price: str  # Decimal as text
    stock: int
    promo: str = ""  # extra text inside the price element, e.g. "2 for $20"


SEED = [
    Item("blue-mug", "Blue Mug", "24.00", 12),
    Item("red-mug", "Red Mug", "22.00", 5),
    Item("green-teapot", "Green Teapot", "45.00", 3),
    Item("coaster-set", "Cork Coaster Set", "12.00", 30, promo="2 for $20"),
]


def state_path() -> Path:
    return Path(os.environ.get("MOCK_SITE_STATE", DEFAULT_STATE))


def load(path: Path | None = None) -> dict[str, Item]:
    path = path or state_path()
    if not path.is_file():
        return {item.slug: Item(**asdict(item)) for item in SEED}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {row["slug"]: Item(**row) for row in data}


def save(items: dict[str, Item], path: Path | None = None) -> None:
    path = path or state_path()
    path.write_text(json.dumps([asdict(i) for i in items.values()], indent=2), encoding="utf-8")


def reset(path: Path | None = None) -> dict[str, Item]:
    items = {item.slug: Item(**asdict(item)) for item in SEED}
    save(items, path)
    return items


def update(
    slug: str, price: str | None = None, stock: int | None = None, path: Path | None = None
) -> Item:
    items = load(path)
    if slug not in items:
        raise KeyError(f"unknown product {slug!r}; known: {', '.join(items)}")
    item = items[slug]
    if price is not None:
        value = Decimal(price)
        if value <= 0:
            raise ValueError("price must be positive")
        item.price = f"{value:.2f}"
    if stock is not None:
        if stock < 0:
            raise ValueError("stock cannot be negative")
        item.stock = stock
    save(items, path)
    return item


# --- HTML --------------------------------------------------------------------------

_STYLE = """
body { font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }
.price { font-size: 1.6rem; color: #0a7d32; } .price small { color: #555; font-size: .9rem; }
.stock.out { color: #b00020; } table { border-collapse: collapse; width: 100%; }
td, th { border-bottom: 1px solid #ddd; padding: .4rem; text-align: left; }
"""


def _page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} | {SHOP_NAME}</title><style>{_STYLE}</style></head>"
        f"<body><header><a href='/'>{SHOP_NAME}</a></header>{body}</body></html>"
    )


def stock_text(item: Item) -> str:
    return f"In stock ({item.stock} available)" if item.stock > 0 else "Out of stock"


def render_product(item: Item) -> str:
    promo = f" <small>{html.escape(item.promo)}</small>" if item.promo else ""
    css = "stock in" if item.stock > 0 else "stock out"
    return _page(
        item.title,
        f"<main class='product'>"
        f"<h1 class='product-name'>{html.escape(item.title)}</h1>"
        f"<p><span class='price'>${item.price}{promo}</span></p>"
        f"<p class='{css}'>{stock_text(item)}</p>"
        f"<button class='add'{'' if item.stock > 0 else ' disabled'}>Add to basket</button>"
        f"</main>",
    )


def render_index(items: dict[str, Item]) -> str:
    rows = "".join(
        f"<li><a href='/product/{i.slug}'>{html.escape(i.title)}</a> ${i.price}</li>"
        for i in items.values()
    )
    return _page("Shop", f"<h1>{SHOP_NAME}</h1><ul>{rows}</ul>")


def render_admin(items: dict[str, Item], message: str = "") -> str:
    rows = "".join(
        f"<tr><td><a href='/product/{i.slug}'>{html.escape(i.title)}</a></td>"
        f"<td><form method='post' action='/admin/{i.slug}'>"
        f"$<input name='price' value='{i.price}' size='7'> "
        f"stock <input name='stock' value='{i.stock}' size='4'> "
        f"<button>Save</button></form></td></tr>"
        for i in items.values()
    )
    note = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    return _page(
        "Admin",
        f"<h1>Admin: change prices and stock</h1>{note}"
        f"<table><tr><th>Product</th><th>Price and stock</th></tr>{rows}</table>"
        f"<form method='post' action='/admin/reset'><button>Reset all</button></form>",
    )


ROBOTS_TXT = "User-agent: *\nDisallow: /admin\n"


# --- HTTP server ---------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "MockShop/1.0"
    state_file: Path | None = None  # set by make_server; None means state_path()

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        items = load(self.state_file)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/robots.txt":
            self._send(HTTPStatus.OK, ROBOTS_TXT, "text/plain")
        elif path == "/":
            self._send(HTTPStatus.OK, render_index(items))
        elif path == "/admin":
            self._send(HTTPStatus.OK, render_admin(items))
        elif path.startswith("/product/") and path.removeprefix("/product/") in items:
            self._send(HTTPStatus.OK, render_product(items[path.removeprefix("/product/")]))
        else:
            self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<h1>Not found</h1>"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        slug = self.path.removeprefix("/admin/")
        try:
            if slug == "reset":
                reset(self.state_file)
                message = "Reset to starting prices."
            else:
                stock = int(form["stock"]) if form.get("stock") else None
                item = update(slug, form.get("price") or None, stock, self.state_file)
                message = f"{item.title}: ${item.price}, {stock_text(item).lower()}."
            status = HTTPStatus.OK
        except (KeyError, ValueError, ArithmeticError) as exc:
            message, status = f"Not saved: {exc}", HTTPStatus.BAD_REQUEST
        self._send(status, render_admin(load(self.state_file), message))

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}")


def make_server(host: str, port: int, state_file: Path | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state_file": state_file})
    return ThreadingHTTPServer((host, port), handler)
