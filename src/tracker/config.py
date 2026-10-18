"""Load and validate config.yaml (F1)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_SITES = 10
MAX_PRODUCTS = 200
MIN_DELAY_SECONDS = 5

# Keys that look like secrets. These belong in environment variables, never in the config file.
SECRET_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|api_?key|webhook_url|private_key", re.IGNORECASE
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Selectors(_Strict):
    title: str = Field(min_length=1)
    price: str = Field(min_length=1)
    stock: str = Field(min_length=1)


class Site(_Strict):
    name: str = Field(min_length=1)
    base_url: HttpUrl
    fetch_mode: Literal["http", "browser"] = "http"
    delay_seconds: float = Field(default=MIN_DELAY_SECONDS, ge=MIN_DELAY_SECONDS)
    selectors: Selectors
    currency: str = Field(description="ISO 4217 code, e.g. USD")

    @field_validator("currency")
    @classmethod
    def _iso_currency(cls, value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"must be a 3-letter ISO currency code like USD or GBP (got {value!r})")
        return value


class Product(_Strict):
    site: str = Field(min_length=1)
    url: HttpUrl
    my_sku: str | None = None
    my_price: Decimal | None = Field(default=None, gt=0)

    @field_validator("my_price", mode="before")
    @classmethod
    def _float_via_str(cls, value: Any) -> Any:
        # YAML parses 49.99 as a float; go through str so it stays exactly 49.99.
        return str(value) if isinstance(value, float) else value


class EmailAlerts(_Strict):
    enabled: bool = False
    to: list[str] = Field(default_factory=list)

    @field_validator("to")
    @classmethod
    def _valid_addresses(cls, value: list[str]) -> list[str]:
        bad = [addr for addr in value if not EMAIL_PATTERN.match(addr)]
        if bad:
            raise ValueError(f"not a valid email address: {', '.join(bad)}")
        return value

    @model_validator(mode="after")
    def _recipients_when_enabled(self) -> EmailAlerts:
        if self.enabled and not self.to:
            raise ValueError("'to' must list at least one address when email is enabled")
        return self


class SlackAlerts(_Strict):
    enabled: bool = False


class Alerts(_Strict):
    min_change_percent: Decimal = Field(default=Decimal("1"), ge=0)
    always_send_summary: bool = False
    email: EmailAlerts = Field(default_factory=EmailAlerts)
    slack: SlackAlerts = Field(default_factory=SlackAlerts)


class Sheets(_Strict):
    enabled: bool = False
    spreadsheet_id: str = ""

    @model_validator(mode="after")
    def _id_when_enabled(self) -> Sheets:
        if self.enabled and not self.spreadsheet_id:
            raise ValueError("'spreadsheet_id' is required when sheets is enabled")
        return self


class Config(_Strict):
    sites: list[Site] = Field(min_length=1, max_length=MAX_SITES)
    products: list[Product] = Field(min_length=1, max_length=MAX_PRODUCTS)
    alerts: Alerts = Field(default_factory=Alerts)
    sheets: Sheets = Field(default_factory=Sheets)

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        problems: list[str] = []

        sites_by_name: dict[str, Site] = {}
        for i, site in enumerate(self.sites):
            if site.name in sites_by_name:
                problems.append(f"sites[{i}].name: duplicate site name '{site.name}'")
            sites_by_name[site.name] = site

        seen_urls: dict[str, int] = {}
        for i, product in enumerate(self.products):
            url = str(product.url)
            if url in seen_urls:
                problems.append(
                    f"products[{i}].url: duplicate of products[{seen_urls[url]}].url ({url})"
                )
            else:
                seen_urls[url] = i

            site = sites_by_name.get(product.site)
            if site is None:
                known = ", ".join(sites_by_name)
                problems.append(
                    f"products[{i}].site: unknown site '{product.site}' (known: {known})"
                )
            elif _host(url) != _host(str(site.base_url)):
                problems.append(
                    f"products[{i}].url: host '{_host(url)}' does not match "
                    f"site '{site.name}' base_url host '{_host(str(site.base_url))}'"
                )

        if problems:
            raise ValueError("\n".join(problems))
        return self

    def site(self, name: str) -> Site:
        return next(s for s in self.sites if s.name == name)


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def load_config(path: str | Path) -> Config:
    """Read and validate a config file. Raises ConfigError with a readable message."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path}: config file not found")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"{path}: invalid YAML{where}: {problem}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping with 'sites' and 'products' at the top")

    secret_paths = list(_find_secret_keys(raw))
    if secret_paths:
        lines = [f"{path}: secrets must come from environment variables, not the config file:"]
        lines += [f"  {p}" for p in secret_paths]
        raise ConfigError("\n".join(lines))

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        lines = [f"{path}: invalid config:"]
        lines += [f"  {line}" for line in _format_errors(exc)]
        raise ConfigError("\n".join(lines)) from exc


def _find_secret_keys(node: Any, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if SECRET_KEY_PATTERN.search(str(key)):
                yield key_path
            yield from _find_secret_keys(value, key_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _find_secret_keys(item, f"{prefix}[{i}]")


def _format_loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _format_errors(exc: ValidationError) -> list[str]:
    lines: list[str] = []
    for err in exc.errors():
        loc = _format_loc(err["loc"])
        kind = err["type"]
        if kind == "missing":
            msg = "missing required field"
        elif kind == "extra_forbidden":
            msg = "unknown field (check the spelling)"
        else:
            msg = err["msg"].removeprefix("Value error, ")
            value = err.get("input")
            if isinstance(value, str | int | float | bool) and kind != "value_error":
                msg += f" (got {value!r})"

        for msg_line in msg.splitlines():
            # Cross-field checks already include their own path in the message.
            lines.append(f"{loc}: {msg_line}" if loc else msg_line)
    return lines
