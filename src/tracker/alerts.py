"""One digest per run, sent by email (SMTP) and/or Slack (incoming webhook) (F8).

Secrets never come from config.yaml: SMTP_* and SLACK_WEBHOOK_URL are read from the
environment (or a .env file loaded by the CLI).
"""

from __future__ import annotations

import html
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from typing import Literal

import certifi
import httpx

from tracker.changes import Change
from tracker.config import Config
from tracker.report import RunReport
from tracker.store import PendingRun, Store

SMTP_TIMEOUT_SECONDS = 30
SLACK_TIMEOUT_SECONDS = 15

# Digest sections, in the order a store owner cares about them.
_SECTIONS: list[tuple[str, Callable[[Change], bool]]] = [
    ("Undercutting you", lambda c: c.kind == "undercut" or c.undercut),
    ("Price changes", lambda c: c.kind in ("price_down", "price_up") and not c.undercut),
    ("Stock", lambda c: c.kind in ("out_of_stock", "back_in_stock")),
    ("Other changes", lambda c: c.kind in ("title_changed", "currency_changed")),
    ("Couldn't check", lambda c: c.kind == "new_failure"),
]


@dataclass(frozen=True)
class Digest:
    subject: str
    text: str
    html: str
    slack: dict


@dataclass(frozen=True)
class Delivery:
    channel: Literal["email", "slack", "sheets"]
    ok: bool
    detail: str


CARRIED_TITLE = "From earlier runs that couldn't be sent"
Channel = Literal["email", "slack"]


def should_send(
    report: RunReport, always_send_summary: bool, carried: Sequence[PendingRun] = ()
) -> bool:
    """F8: no alert when nothing changed, unless always_send_summary is on.

    Items from earlier runs whose digest failed to send also count as something to send.
    """
    return bool(report.changes or report.new_site_alerts or carried or always_send_summary)


def digest_items(report: RunReport) -> list[dict]:
    """What this run's digest has to say, in a form the store can keep and resend later."""
    items = [
        {"section": title, "text": change.describe(), "url": change.product.url}
        for title, changes in _group(report.changes)
        for change in changes
    ]
    items += [
        {"section": "Site health", "text": site.describe(), "url": None}
        for site in report.new_site_alerts
    ]
    return items


def _run_label(started_at: str) -> str:
    return f"{datetime.fromisoformat(started_at):%d %b %Y %H:%M} UTC"


def _carried_count(carried: Sequence[PendingRun]) -> int:
    return sum(len(run.items) for run in carried)


Groups = list[tuple[str, list[Change]]]


def _group(changes: tuple[Change, ...]) -> Groups:
    groups = [(title, [c for c in changes if belongs(c)]) for title, belongs in _SECTIONS]
    return [(title, items) for title, items in groups if items]


def _subject(report: RunReport, today: date, carried: Sequence[PendingRun]) -> str:
    parts = []
    n = len(report.changes)
    parts.append(f"{n} change{'' if n == 1 else 's'}" if n else "no changes")
    undercuts = sum(1 for c in report.changes if c.kind == "undercut" or c.undercut)
    if undercuts:
        parts.append(f"{undercuts} undercut{'' if undercuts == 1 else 's'}")
    if report.failing_sites:
        k = len(report.failing_sites)
        parts.append(f"{k} site{'' if k == 1 else 's'} failing")
    if carried:
        parts.append(f"{_carried_count(carried)} carried over")
    return f"Competitor prices {today:%d %b %Y}: {', '.join(parts)}"


def _health_lines(report: RunReport) -> list[str]:
    return [("NEW: " if s.newly_failing else "") + s.describe() for s in report.failing_sites]


def _text(
    subject: str,
    groups: Groups,
    health: list[str],
    report: RunReport,
    carried: Sequence[PendingRun],
) -> str:
    lines = [subject, ""]
    if not groups and not health and not carried:
        lines += ["Nothing changed since the last check.", ""]
    for title, items in groups:
        lines.append(title.upper())
        for change in items:
            lines += [f"- {change.describe()}", f"  {change.product.url}"]
        lines.append("")
    if health:
        lines += ["SITE HEALTH", *(f"- {line}" for line in health), ""]
    if carried:
        lines.append(CARRIED_TITLE.upper())
        for run in carried:
            lines.append(_run_label(run.started_at))
            for item in run.items:
                lines.append(f"- [{item['section']}] {item['text']}")
                if item["url"]:
                    lines.append(f"  {item['url']}")
        lines.append("")
    lines += [report.summary(), f"Run {report.run_id}"]
    return "\n".join(lines) + "\n"


# Inline styles: email clients ignore <style> blocks. One column, readable on a phone.
_BODY = (
    "font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:16px;line-height:1.45;"
    "color:#1a1a1a;max-width:600px;margin:0 auto;padding:8px"
)
_H1 = "font-size:20px;margin:0 0 12px"
_H2 = "font-size:17px;margin:20px 0 6px"
_UL = "padding-left:20px;margin:0"
_LI = "margin:0 0 8px"
_LINK = "color:#0b57d0"
_ALERT = "color:#b00020"
_FOOT = "color:#666;font-size:13px;margin-top:24px"


def _html(
    subject: str,
    groups: Groups,
    health: list[str],
    report: RunReport,
    carried: Sequence[PendingRun],
) -> str:
    # Everything that came from a competitor's page (titles, errors) is escaped.
    esc = html.escape
    parts = [f"<div style='{_BODY}'>", f"<h1 style='{_H1}'>{esc(subject)}</h1>"]
    if not groups and not health and not carried:
        parts.append("<p>Nothing changed since the last check.</p>")
    for title, items in groups:
        parts.append(f"<h2 style='{_H2}'>{esc(title)}</h2><ul style='{_UL}'>")
        for change in items:
            link = f"<a href='{esc(change.product.url, quote=True)}' style='{_LINK}'>view</a>"
            parts.append(f"<li style='{_LI}'>{esc(change.describe())} {link}</li>")
        parts.append("</ul>")
    if health:
        parts.append(f"<h2 style='{_H2};{_ALERT}'>Site health</h2><ul style='{_UL}'>")
        parts += [f"<li style='{_LI}'>{esc(line)}</li>" for line in health]
        parts.append("</ul>")
    if carried:
        parts.append(f"<h2 style='{_H2}'>{esc(CARRIED_TITLE)}</h2>")
        for run in carried:
            parts.append(f"<p style='{_FOOT};margin:8px 0 4px'>{_run_label(run.started_at)}</p>")
            parts.append(f"<ul style='{_UL}'>")
            for item in run.items:
                link = ""
                if item["url"]:
                    href = esc(item["url"], quote=True)
                    link = f" <a href='{href}' style='{_LINK}'>view</a>"
                text = f"{esc(item['section'])}: {esc(item['text'])}"
                parts.append(f"<li style='{_LI}'>{text}{link}</li>")
            parts.append("</ul>")
    parts.append(
        f"<p style='{_FOOT}'>{esc(report.summary())}<br>Run {esc(report.run_id)}</p></div>"
    )
    return "".join(parts)


def _slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _slack(
    subject: str,
    groups: Groups,
    health: list[str],
    report: RunReport,
    carried: Sequence[PendingRun],
) -> dict:
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": subject[:150]}}]
    for title, items in groups:
        bullets = "\n".join(
            f"• {_slack_escape(c.describe())} <{c.product.url}|view>" for c in items
        )
        blocks.append(_slack_section(f"*{title}*\n{bullets}"))
    if health:
        bullets = "\n".join(f"• {_slack_escape(line)}" for line in health)
        blocks.append(_slack_section(f"*Site health*\n{bullets}"))
    for run in carried:
        bullets = "\n".join(
            f"• {_slack_escape(i['section'])}: {_slack_escape(i['text'])}"
            + (f" <{i['url']}|view>" if i["url"] else "")
            for i in run.items
        )
        label = f"*{CARRIED_TITLE}* ({_run_label(run.started_at)})"
        blocks.append(_slack_section(f"{label}\n{bullets}"))
    if not groups and not health and not carried:
        blocks.append(_slack_section("Nothing changed since the last check."))
    summary = {"type": "mrkdwn", "text": _slack_escape(report.summary())}
    blocks.append({"type": "context", "elements": [summary]})
    return {"text": subject, "blocks": blocks[:50]}


def build_digest(report: RunReport, today: date, carried: Sequence[PendingRun] = ()) -> Digest:
    """This run's digest, plus a labelled section for `carried` runs that never got through."""
    groups = _group(report.changes)
    health = _health_lines(report)
    subject = _subject(report, today, carried)
    return Digest(
        subject,
        _text(subject, groups, health, report, carried),
        _html(subject, groups, health, report, carried),
        _slack(subject, groups, health, report, carried),
    )


# --- secrets -----------------------------------------------------------------------


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str | None
    password: str | None
    sender: str
    security: Literal["starttls", "ssl", "none"]


def smtp_settings(env: Mapping[str, str]) -> SmtpSettings:
    """Read SMTP_* variables. Raises ValueError naming whatever is missing or wrong."""
    missing = [k for k in ("SMTP_HOST",) if not env.get(k)]
    user, password = env.get("SMTP_USER") or None, env.get("SMTP_PASSWORD") or None
    sender = env.get("SMTP_FROM") or user
    if not sender:
        missing.append("SMTP_FROM (or SMTP_USER)")
    if user and not password:
        missing.append("SMTP_PASSWORD")
    if missing:
        raise ValueError(f"email is enabled but {', '.join(missing)} not set (see .env.example)")

    try:
        port = int(env.get("SMTP_PORT") or 587)
    except ValueError:
        raise ValueError(f"SMTP_PORT must be a number (got {env['SMTP_PORT']!r})") from None
    security = (env.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    if security not in ("starttls", "ssl", "none"):
        raise ValueError(f"SMTP_SECURITY must be starttls, ssl or none (got {security!r})")
    return SmtpSettings(env["SMTP_HOST"], port, user, password, sender, security)


def slack_webhook(env: Mapping[str, str]) -> str:
    url = env.get("SLACK_WEBHOOK_URL")
    if not url:
        raise ValueError("slack is enabled but SLACK_WEBHOOK_URL is not set (see .env.example)")
    if not url.startswith("https://hooks.slack.com/"):
        raise ValueError("SLACK_WEBHOOK_URL should start with https://hooks.slack.com/")
    return url


def missing_secrets(config: Config, env: Mapping[str, str]) -> list[str]:
    """Problems with secrets for the channels config enables (for check-config)."""
    problems = []
    if config.alerts.email.enabled:
        try:
            smtp_settings(env)
        except ValueError as exc:
            problems.append(str(exc))
    if config.alerts.slack.enabled:
        try:
            slack_webhook(env)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


# --- sending -------------------------------------------------------------------------

SmtpFactory = Callable[[SmtpSettings], smtplib.SMTP]


def tls_context() -> ssl.SSLContext:
    """System trust store plus certifi's bundle, with full verification.

    Some Python installs (python.org's macOS installer before "Install Certificates" is run)
    have an empty system trust store, so the default context can't verify any SMTP server.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def connect_smtp(settings: SmtpSettings) -> smtplib.SMTP:
    context = tls_context()
    if settings.security == "ssl":
        return smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    smtp = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS)
    if settings.security == "starttls":
        smtp.starttls(context=context)
    return smtp


def send_email(
    digest: Digest, to: list[str], settings: SmtpSettings, connect: SmtpFactory = connect_smtp
) -> Delivery:
    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = settings.sender
    message["To"] = ", ".join(to)
    message.set_content(digest.text)
    message.add_alternative(digest.html, subtype="html")
    try:
        with connect(settings) as smtp:
            if settings.user:
                smtp.login(settings.user, settings.password or "")
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError:
        return Delivery(
            "email",
            False,
            "login failed: check SMTP_USER and SMTP_PASSWORD (Gmail needs an app password)",
        )
    except (smtplib.SMTPException, OSError) as exc:
        return Delivery("email", False, f"{type(exc).__name__}: {exc}")
    return Delivery("email", True, f"sent to {', '.join(to)}")


def send_slack(digest: Digest, webhook_url: str, client: httpx.Client | None = None) -> Delivery:
    owned = client is None
    client = client or httpx.Client(timeout=SLACK_TIMEOUT_SECONDS)
    try:
        response = client.post(webhook_url, json=digest.slack)
    except httpx.HTTPError as exc:
        return Delivery("slack", False, f"{type(exc).__name__}: {exc}")
    finally:
        if owned:
            client.close()
    if response.status_code != 200:
        return Delivery("slack", False, f"HTTP {response.status_code}: {response.text[:200]}")
    return Delivery("slack", True, "posted")


def enabled_channels(config: Config) -> list[Channel]:
    channels: list[Channel] = []
    if config.alerts.email.enabled:
        channels.append("email")
    if config.alerts.slack.enabled:
        channels.append("slack")
    return channels


def deliver(
    digests: Mapping[Channel, Digest],
    config: Config,
    env: Mapping[str, str],
    *,
    connect: SmtpFactory = connect_smtp,
    http_client: httpx.Client | None = None,
) -> list[Delivery]:
    """Send each channel its digest. Never raises; each result says what happened."""
    results: list[Delivery] = []
    if "email" in digests:
        try:
            settings = smtp_settings(env)
        except ValueError as exc:
            results.append(Delivery("email", False, str(exc)))
        else:
            results.append(send_email(digests["email"], config.alerts.email.to, settings, connect))
    if "slack" in digests:
        try:
            url = slack_webhook(env)
        except ValueError as exc:
            results.append(Delivery("slack", False, str(exc)))
        else:
            results.append(send_slack(digests["slack"], url, http_client))
    return results


@dataclass(frozen=True)
class Notification:
    channels: list[Channel]  # enabled channels
    digests: dict[Channel, Digest]  # what each channel was (or would be) sent
    carried: dict[Channel, list[PendingRun]]
    deliveries: list[Delivery]


def notify(
    report: RunReport,
    started_at: datetime,
    config: Config,
    store: Store,
    env: Mapping[str, str],
    *,
    dry_run: bool = False,
    connect: SmtpFactory = connect_smtp,
    http_client: httpx.Client | None = None,
) -> Notification:
    """Record this run's digest items, then send each channel this run's digest plus anything
    from earlier runs that never reached it. Items are marked delivered only after a
    successful send. A dry run records nothing, marks nothing and sends nothing.
    """
    channels = enabled_channels(config)
    items = digest_items(report)
    if not dry_run:
        store.record_run(
            report.run_id, started_at.isoformat(timespec="seconds"), list(channels), items
        )

    digests: dict[Channel, Digest] = {}
    carried: dict[Channel, list[PendingRun]] = {}
    for channel in channels:
        pending = store.pending_runs(channel, exclude_run=report.run_id)
        if should_send(report, config.alerts.always_send_summary, pending):
            digests[channel] = build_digest(report, started_at.date(), pending)
            carried[channel] = pending

    if dry_run or not digests:
        return Notification(channels, digests, carried, [])

    deliveries = deliver(digests, config, env, connect=connect, http_client=http_client)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for result in deliveries:
        if not result.ok:
            continue
        runs = [p.run_id for p in carried[result.channel]] + ([report.run_id] if items else [])
        for run_id in runs:
            store.record_delivery(
                run_id, result.channel, delivered_in=report.run_id, delivered_at=now
            )
    return Notification(channels, digests, carried, deliveries)
