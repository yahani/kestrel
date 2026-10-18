import copy
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config.example.yaml"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any test that tries a real HTTP request. httpx.MockTransport still works."""

    def _blocked(self, request):
        raise RuntimeError(f"tests must not hit the network: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


@pytest.fixture
def example_data() -> dict:
    """The example config as a dict, safe to mutate per test."""
    return copy.deepcopy(yaml.safe_load(EXAMPLE_CONFIG.read_text()))


@pytest.fixture
def write_config(tmp_path):
    """Write a dict (or raw text) to a temp config file and return its path."""

    def _write(data: dict | str) -> Path:
        path = tmp_path / "config.yaml"
        text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
        path.write_text(text)
        return path

    return _write


MOCK_CONFIG_FILE = ROOT / "mock_site" / "tracker-config.yaml"


class MockShop:
    """The mock competitor shop served in-process through httpx.MockTransport."""

    def __init__(self, state: Path):
        from mock_site import shop

        self.shop = shop
        self.state = state
        self.overrides: dict[str, httpx.Response] = {}  # path -> forced response
        shop.reset(state)

    def set(self, slug: str, price: str | None = None, stock: int | None = None) -> None:
        self.shop.update(slug, price, stock, path=self.state)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path]
        if path == "/robots.txt":
            return httpx.Response(200, text=self.shop.ROBOTS_TXT)
        items = self.shop.load(self.state)
        slug = path.removeprefix("/product/")
        if path.startswith("/product/") and slug in items:
            return httpx.Response(200, text=self.shop.render_product(items[slug]))
        return httpx.Response(404)

    def fetcher(self, cfg):
        from tracker.fetch import Fetcher

        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return Fetcher(cfg.fetch, client=client, sleep=lambda _: None)


@pytest.fixture
def mock_shop(tmp_path) -> MockShop:
    return MockShop(tmp_path / "shop-state.json")


@pytest.fixture
def mock_config_file(tmp_path) -> Path:
    """The mock shop's tracker config, with its database in a temp folder."""
    data = yaml.safe_load(MOCK_CONFIG_FILE.read_text())
    data["database"] = str(tmp_path / "tracker.db")
    path = tmp_path / "mock-config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


SECRET_VARS = [
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY",
    "SLACK_WEBHOOK_URL", "GOOGLE_SERVICE_ACCOUNT_JSON",
]  # fmt: skip


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    """Tests never see real secrets: clear them and stop the CLI from reading any .env file."""
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    import tracker.cli

    monkeypatch.setattr(tracker.cli, "load_dotenv", lambda *args, **kwargs: False)
