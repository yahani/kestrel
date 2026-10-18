"""Command-line interface (F11)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tracker.config import Config, ConfigError, load_config

app = typer.Typer(
    help="Track competitor prices and stock, and alert when something changes.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="Path to the config file.")]
DEFAULT_CONFIG = Path("config.yaml")


def _load_or_exit(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _not_implemented(session: int) -> None:
    typer.echo(f"Not implemented yet (planned for build session {session}).", err=True)
    raise typer.Exit(code=1)


def _on_off(enabled: bool) -> str:
    return "on" if enabled else "off"


@app.command("check-config")
def check_config(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Validate the config file and print a summary."""
    cfg = _load_or_exit(config)

    email = cfg.alerts.email
    email_text = _on_off(email.enabled)
    if email.enabled:
        email_text += f" ({len(email.to)} recipient{'s' if len(email.to) != 1 else ''})"

    typer.echo(f"✓ {config} is valid")
    typer.echo(f"  sites:    {len(cfg.sites)} ({', '.join(s.name for s in cfg.sites)})")
    typer.echo(f"  products: {len(cfg.products)}")
    typer.echo(
        f"  alerts:   email {email_text}, slack {_on_off(cfg.alerts.slack.enabled)}, "
        f"min change {cfg.alerts.min_change_percent}%"
    )
    typer.echo(f"  sheets:   {_on_off(cfg.sheets.enabled)}")


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Fetch and detect changes, but send no alerts and write nothing."
        ),
    ] = False,
) -> None:
    """Run the full daily pipeline: fetch, parse, store, detect changes, alert."""
    _load_or_exit(config)
    _not_implemented(session=4)


@app.command("test-selectors")
def test_selectors(
    url: Annotated[str, typer.Argument(help="Product page URL to test.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Fetch one page and show what the site's selectors extract."""
    _load_or_exit(config)
    _not_implemented(session=2)


@app.command()
def history(
    product: Annotated[str, typer.Argument(help="Product URL or your SKU.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Show stored price and stock history for one product."""
    _load_or_exit(config)
    _not_implemented(session=3)
