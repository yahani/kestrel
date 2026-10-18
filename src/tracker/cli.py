"""Command-line interface (F11)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tracker.config import Config, ConfigError, load_config
from tracker.fetch import Fetcher
from tracker.parse import parse_page

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


def _make_fetcher(cfg: Config) -> Fetcher:
    return Fetcher(cfg.fetch)


@app.command("test-selectors")
def test_selectors(
    url: Annotated[str, typer.Argument(help="Product page URL to test.")],
    config: ConfigOption = DEFAULT_CONFIG,
    html_file: Annotated[
        Path | None,
        typer.Option("--html", help="Parse this saved HTML file instead of fetching the URL."),
    ] = None,
    save: Annotated[
        Path | None, typer.Option("--save", help="Save the fetched HTML here (for fixtures).")
    ] = None,
) -> None:
    """Fetch one page and show what the site's selectors extract."""
    cfg = _load_or_exit(config)
    site = cfg.site_for_url(url)
    if site is None:
        known = ", ".join(f"{s.name} ({s.base_url})" for s in cfg.sites)
        typer.echo(f"✗ no configured site matches {url}. Known sites: {known}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"site:   {site.name} ({site.fetch_mode})")

    if html_file is not None:
        html = html_file.read_text(encoding="utf-8")
        typer.echo(f"fetch:  skipped, reading {html_file}")
    else:
        with _make_fetcher(cfg) as fetcher:
            result = fetcher.fetch(url, site)
        code = f"HTTP {result.http_status}" if result.http_status else "no response"
        typer.echo(f"fetch:  {result.status} · {code} · {result.elapsed:.2f}s")
        if not result.ok or result.html is None:
            typer.echo(f"✗ {result.error}", err=True)
            raise typer.Exit(code=1)
        html = result.html
        if save is not None:
            save.parent.mkdir(parents=True, exist_ok=True)
            save.write_text(html, encoding="utf-8")
            typer.echo(f"saved:  {save}")

    page = parse_page(html, site)
    normalised = {
        "title": page.title,
        "price": f"{page.price} {page.currency}" if page.price is not None else None,
        "stock": page.stock_status
        + (f", qty {page.stock_qty}" if page.stock_qty is not None else ""),
    }
    typer.echo("")
    for name, selector in site.selectors.model_dump().items():
        raw = page.raw.get(name)
        shown = repr(raw) if raw is not None else "(no match)"
        typer.echo(f"{name:<6} {selector}")
        typer.echo(f"       matched: {shown}")
        if name != "title":
            typer.echo(f"       value:   {normalised[name]}")

    typer.echo("")
    if page.status != "ok":
        typer.echo(f"✗ parse_error: {page.error}", err=True)
        raise typer.Exit(code=1)
    typer.echo("✓ parsed ok")


@app.command()
def history(
    product: Annotated[str, typer.Argument(help="Product URL or your SKU.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Show stored price and stock history for one product."""
    _load_or_exit(config)
    _not_implemented(session=3)
