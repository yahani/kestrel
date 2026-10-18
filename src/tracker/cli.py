"""Command-line interface (F11)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from tracker import alerts, sheets
from tracker.alerts import Delivery, build_digest, missing_secrets, notify, should_send
from tracker.changes import format_money
from tracker.config import Config, ConfigError, load_config
from tracker.fetch import Fetcher
from tracker.parse import parse_page
from tracker.pipeline import run_once
from tracker.report import build_report
from tracker.store import Observation, Store

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


def _load_env(config_path: Path) -> None:
    """Load secrets from a .env next to the config, then from the current folder.

    Real environment variables (e.g. GitHub Actions secrets) always win.
    """
    for env_file in (config_path.parent / ".env", Path.cwd() / ".env"):
        if env_file.is_file():
            load_dotenv(env_file, override=False)


def _secret_problems(cfg: Config) -> list[str]:
    problems = missing_secrets(cfg, os.environ)
    if cfg.sheets.enabled:
        try:
            sheets.service_account_path(os.environ)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


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

    _load_env(config)
    for problem in _secret_problems(cfg):
        typer.echo(f"! {problem}")


def _db_path(config_path: Path, cfg: Config) -> Path:
    """The database path from config, relative to the config file's folder."""
    path = Path(cfg.database)
    return path if path.is_absolute() else config_path.parent / path


@app.command()
def run(
    config: ConfigOption = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Fetch and detect changes, then preview the digest. Saves nothing, sends nothing.",
        ),
    ] = False,
) -> None:
    """Run the daily pipeline: fetch, parse, store, detect changes, export, send the digest.

    Exits with status 1 if an enabled channel (email, Slack, Sheets) failed, so a scheduled
    run shows up as failed. Products that can't be checked are reported in the digest instead.
    """
    cfg = _load_or_exit(config)
    _load_env(config)
    db = _db_path(config, cfg)
    store = Store.scratch_copy(db) if dry_run else Store.open(db)
    with store, _make_fetcher(cfg) as fetcher:
        result = run_once(cfg, store, fetcher)
        report = build_report(result, store)
        latest = store.latest_observations()

        typer.echo(f"run {result.run_id}: {report.summary()}")
        for change in report.changes:
            typer.echo(f"  • {change.describe()}")
        failures = [(p, o) for p, o in result.observations if not o.ok]
        if failures:
            typer.echo("failed this run:")
            for product, observation in failures:
                typer.echo(f"  ✗ {product.url}: {observation.status} ({observation.error})")
        for site in report.failing_sites:
            typer.echo(f"  ! {site.describe()}")

        deliveries: list[Delivery] = []
        if cfg.sheets.enabled and not dry_run:
            deliveries.append(
                sheets.export(
                    cfg, os.environ, result.observations, latest, open_fn=_open_spreadsheet
                )
            )
        sent = notify(
            report, result.started_at, cfg, store, os.environ,
            dry_run=dry_run, connect=_smtp_connect, http_client=_slack_client,
        )  # fmt: skip

    for channel, pending in sent.carried.items():
        if pending:
            n = sum(len(run.items) for run in pending)
            typer.echo(f"{channel}: carrying over {n} item(s) from {len(pending)} earlier run(s)")

    if dry_run:
        typer.echo("(dry run: nothing saved, no digest sent, Sheets not updated)")
        preview = next(iter(sent.digests.values()), None)
        if preview is None and not sent.channels:
            should = should_send(report, cfg.alerts.always_send_summary)
            preview = build_digest(report, result.started_at.date()) if should else None
        if preview is not None:
            typer.echo("\n--- digest preview ---\n" + preview.text)
        else:
            typer.echo("digest: would not be sent (nothing changed)")
        return

    if not sent.channels and should_send(report, cfg.alerts.always_send_summary):
        typer.echo("digest: no channel enabled (alerts.email or alerts.slack)")
    elif not sent.digests:
        typer.echo("digest: not sent (nothing changed)")
    deliveries += sent.deliveries
    for delivery in deliveries:
        mark = "✓" if delivery.ok else "✗"
        typer.echo(f"{delivery.channel}: {mark} {delivery.detail}")
    if any(not d.ok and d.channel != "sheets" for d in sent.deliveries):
        typer.echo("  (undelivered changes will be included in the next digest that gets through)")
    typer.echo(f"saved to {db}")
    if any(not d.ok for d in deliveries):
        raise typer.Exit(code=1)


# Seams for tests: the real SMTP connection and Google client.
_smtp_connect = alerts.connect_smtp
_open_spreadsheet = sheets.open_spreadsheet
_slack_client = None  # None: alerts.send_slack makes its own httpx client


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


def _history_line(o: Observation) -> str:
    price = format_money(o.price, o.currency)
    stock = o.stock_status or "-"
    if o.stock_qty is not None:
        stock += f" ({o.stock_qty})"
    detail = o.title if o.ok else o.error
    return f"{o.fetched_at:<25} {o.status:<14} {price:>14}  {stock:<18} {detail or ''}"


@app.command()
def history(
    product: Annotated[str, typer.Argument(help="Product URL, your SKU, or part of the URL.")],
    config: ConfigOption = DEFAULT_CONFIG,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Rows to show.")] = 20,
) -> None:
    """Show stored price and stock history for one product, newest first."""
    if not product.strip():
        typer.echo("✗ give a product URL, SKU or part of a URL", err=True)
        raise typer.Exit(code=1)
    cfg = _load_or_exit(config)
    db = _db_path(config, cfg)
    if not db.is_file():
        typer.echo(f"✗ no history yet ({db} doesn't exist): run `tracker run` first", err=True)
        raise typer.Exit(code=1)

    with Store.open(db) as store:
        matches = store.find_products(product)
        if not matches:
            typer.echo(f"✗ no product matches {product!r}", err=True)
            raise typer.Exit(code=1)
        if len(matches) > 1:
            typer.echo(f"✗ {len(matches)} products match {product!r}; be more specific:", err=True)
            for p in matches:
                typer.echo(f"  {p.my_sku or '-':<14} {p.url}", err=True)
            raise typer.Exit(code=1)

        match = matches[0]
        rows = store.history(match.id, limit)

    # A product removed from config may belong to a site that's gone too.
    currency = next((site.currency for site in cfg.sites if site.name == match.site), None)
    shown = format_money(match.my_price, currency, pad=True)
    my_price = f", my price {shown}" if match.my_price is not None else ""
    inactive = " (no longer in config)" if not match.active else ""
    typer.echo(f"{match.url}{inactive}")
    typer.echo(f"site {match.site}, sku {match.my_sku or '-'}{my_price}")
    if not rows:
        typer.echo("no observations yet")
        return
    typer.echo("")
    typer.echo(f"{'fetched (UTC)':<25} {'status':<14} {'price':>14}  {'stock':<18} title / error")
    for observation in rows:
        typer.echo(_history_line(observation))
