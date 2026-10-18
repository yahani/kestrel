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
