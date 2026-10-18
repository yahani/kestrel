"""Competitor price and stock tracker."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("price-tracker")  # single source of truth: pyproject.toml
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled checkout
    __version__ = "0+unknown"
