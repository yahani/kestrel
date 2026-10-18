import threading
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

from mock_site import __main__ as mock_cli
from mock_site import shop
from tests.conftest import FIXTURES, ROOT
from tracker.config import load_config
from tracker.parse import parse_page

MOCK_CONFIG = load_config(ROOT / "mock_site" / "tracker-config.yaml")
RIVAL_X = MOCK_CONFIG.site("rival-x")


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "state.json"
    monkeypatch.setenv("MOCK_SITE_STATE", str(path))
    return path


def test_seed_is_used_when_no_state_file(state):
    items = shop.load()
    assert list(items) == ["blue-mug", "red-mug", "green-teapot", "coaster-set"]
    assert items["blue-mug"].price == "24.00"
    assert not state.exists()


def test_update_and_reset(state):
    shop.update("blue-mug", price="19.5")
    shop.update("red-mug", stock=0)
    items = shop.load()
    assert items["blue-mug"].price == "19.50"
    assert items["red-mug"].stock == 0

    shop.reset()
    assert shop.load()["blue-mug"].price == "24.00"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"slug": "nope", "price": "1"}, "unknown product 'nope'"),
        ({"slug": "blue-mug", "price": "0"}, "price must be positive"),
        ({"slug": "blue-mug", "stock": -1}, "stock cannot be negative"),
    ],
)
def test_update_rejects_bad_input(state, kwargs, message):
    with pytest.raises((KeyError, ValueError), match=message):
        shop.update(**kwargs)


def test_update_rejects_non_numeric_price(state):
    with pytest.raises(ArithmeticError):
        shop.update("blue-mug", price="cheap")


# --- the tracker can read the mock shop ------------------------------------------------


@pytest.mark.parametrize(
    ("slug", "title", "price", "stock", "qty"),
    [
        ("blue-mug", "Blue Mug", "24.00", "in_stock", 12),
        ("coaster-set", "Cork Coaster Set", "12.00", "in_stock", 30),  # "2 for $20" promo ignored
    ],
)
def test_tracker_parses_mock_pages(state, slug, title, price, stock, qty):
    page = parse_page(shop.render_product(shop.load()[slug]), RIVAL_X)
    assert page.status == "ok", page.error
    assert (page.title, page.price, page.currency) == (title, Decimal(price), "USD")
    assert (page.stock_status, page.stock_qty) == (stock, qty)


def test_tracker_sees_out_of_stock(state):
    shop.update("red-mug", stock=0)
    page = parse_page(shop.render_product(shop.load()["red-mug"]), RIVAL_X)
    assert page.stock_status == "out_of_stock"


def test_saved_fixture_matches_renderer(state):
    # tests/fixtures/mock_blue_mug.html is the seed blue-mug page, saved from the server.
    saved = (FIXTURES / "mock_blue_mug.html").read_text(encoding="utf-8")
    assert saved == shop.render_product(shop.load()["blue-mug"])
    assert parse_page(saved, RIVAL_X).price == Decimal("24.00")


def test_titles_are_escaped(state):
    item = shop.Item("x", "<script>", "1.00", 1, promo="<b>")
    rendered = shop.render_product(item)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# --- the real HTTP server on localhost --------------------------------------------------


@pytest.fixture
def server(state):
    srv = shop.make_server("127.0.0.1", 0, state_file=state)
    srv.RequestHandlerClass.log_message = lambda *args: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(url: str, form: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_server_pages(server):
    assert get(f"{server}/robots.txt") == (200, shop.ROBOTS_TXT)
    status, body = get(f"{server}/")
    assert status == 200 and "/product/blue-mug" in body
    status, body = get(f"{server}/product/blue-mug")
    assert status == 200 and "$24.00" in body
    assert get(f"{server}/product/nope")[0] == 404
    assert get(f"{server}/admin")[0] == 200


def test_server_picks_up_state_changes_without_restart(server, state):
    shop.update("blue-mug", price="19.50", path=state)
    _, body = get(f"{server}/product/blue-mug")
    assert "$19.50" in body


def test_admin_form_changes_and_resets(server, state):
    status, body = post(f"{server}/admin/blue-mug", {"price": "18.00", "stock": "0"})
    assert status == 200
    assert "Blue Mug: $18.00, out of stock." in body
    assert shop.load(state)["blue-mug"].stock == 0

    status, _ = post(f"{server}/admin/reset", {})
    assert status == 200
    assert shop.load(state)["blue-mug"].price == "24.00"


def test_admin_form_rejects_bad_input(server):
    status, body = post(f"{server}/admin/blue-mug", {"price": "-5", "stock": ""})
    assert status == 400
    assert "Not saved: price must be positive" in body


# --- CLI ------------------------------------------------------------------------------


def test_cli_set_list_reset(state, capsys):
    assert mock_cli.main(["set", "blue-mug", "--price", "19.50", "--stock", "0"]) == 0
    assert "blue-mug: $19.50, Out of stock" in capsys.readouterr().out

    assert mock_cli.main(["list"]) == 0
    assert "blue-mug       $  19.50  Out of stock" in capsys.readouterr().out

    assert mock_cli.main(["reset"]) == 0
    assert "blue-mug       $  24.00  In stock (12 available)" in capsys.readouterr().out


def test_cli_set_unknown_product(state, capsys):
    assert mock_cli.main(["set", "nope", "--price", "1"]) == 1
    assert "unknown product 'nope'" in capsys.readouterr().err


def test_cli_set_needs_a_change(state):
    with pytest.raises(SystemExit):
        mock_cli.main(["set", "blue-mug"])
