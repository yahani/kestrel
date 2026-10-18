import re
import smtplib
import sqlite3
import ssl
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import certifi
import httpx
import pytest

from tests.conftest import MOCK_CONFIG_FILE
from tracker.alerts import (
    Delivery,
    build_digest,
    connect_smtp,
    deliver,
    missing_secrets,
    send_email,
    send_slack,
    should_send,
    slack_webhook,
    smtp_settings,
)
from tracker.changes import Change
from tracker.config import Config, load_config
from tracker.pipeline import run_once
from tracker.report import RunReport, SiteHealth, build_report
from tracker.store import ProductRow, Store

CONFIG = load_config(MOCK_CONFIG_FILE)
TODAY = date(2026, 10, 18)
ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "tracker@example.com",
    "SMTP_PASSWORD": "app-password",
}
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def with_alerts(email: bool = True, slack: bool = False, always: bool = False) -> Config:
    data = CONFIG.model_dump(mode="json")
    data["alerts"]["email"] = {"enabled": email, "to": ["owner@example.com"] if email else []}
    data["alerts"]["slack"] = {"enabled": slack}
    data["alerts"]["always_send_summary"] = always
    return Config.model_validate(data)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=5)
        return self.now


@pytest.fixture
def report_after(mock_shop):
    """Run the mock shop once, apply `changes`, run again, and return the second report."""

    def _run(*changes: dict, overrides: dict | None = None) -> RunReport:
        with Store(sqlite3.connect(":memory:")) as store:
            with mock_shop.fetcher(CONFIG) as fetcher:
                run_once(CONFIG, store, fetcher, now=Clock())
            for change in changes:
                mock_shop.set(**change)
            mock_shop.overrides.update(overrides or {})
            with mock_shop.fetcher(CONFIG) as fetcher:
                result = run_once(CONFIG, store, fetcher, now=Clock())
            return build_report(result, store)

    return _run


THREE_CHANGES = (
    {"slug": "blue-mug", "price": "19.50"},
    {"slug": "green-teapot", "price": "49.50"},
    {"slug": "red-mug", "stock": 0},
)


# --- what goes in the digest ---------------------------------------------------------


def test_digest_groups_changes_by_what_matters(report_after):
    digest = build_digest(report_after(*THREE_CHANGES), TODAY)

    assert digest.subject == "Competitor prices 18 Oct 2026: 3 changes, 1 undercut"
    text = digest.text
    assert text.index("UNDERCUTTING YOU") < text.index("PRICE CHANGES") < text.index("STOCK")
    assert "- rival-x cut Blue Mug from $24.00 to $19.50 (-18.8%), below your $23.00" in text
    assert "- rival-x raised Green Teapot from $45.00 to $49.50 (+10.0%)" in text
    assert "- rival-x is out of stock on Red Mug" in text
    assert "  http://127.0.0.1:8000/product/blue-mug" in text
    assert "4 products checked: 4 ok, 0 failed, 3 changes" in text
    assert "SITE HEALTH" not in text


def test_digest_lists_each_change_once(report_after):
    digest = build_digest(report_after(*THREE_CHANGES), TODAY)
    assert digest.text.count("Blue Mug") == 1
    assert digest.html.count("Blue Mug") == 1


def test_html_digest_is_one_column_with_links(report_after):
    html = build_digest(report_after(*THREE_CHANGES), TODAY).html
    assert "max-width:600px" in html
    assert "<h2 style='font-size:17px;margin:20px 0 6px'>Undercutting you</h2>" in html
    assert "<a href='http://127.0.0.1:8000/product/red-mug'" in html
    assert "<style" not in html  # inline styles only; many email clients drop <style>


def test_new_failures_go_in_couldnt_check(report_after):
    digest = build_digest(report_after(overrides={"/product/red-mug": httpx.Response(404)}), TODAY)
    assert "COULDN'T CHECK" in digest.text
    assert "rival-x: could not check Red Mug (http_error: HTTP 404)" in digest.text


def product(title_site: str = "rival-x") -> ProductRow:
    return ProductRow(1, title_site, "http://127.0.0.1:8000/product/x", None, None, True)


def report_with(changes=(), failing=()) -> RunReport:
    return RunReport("20261018T060000Z-abc123", 4, 4, 0, 20.0, tuple(changes), tuple(failing))


def test_competitor_text_is_escaped_in_html_and_slack():
    evil = Change(
        "title_changed",
        product(),
        "x",
        old_title="Mug",
        new_title="<img src=x onerror=alert(1)> & <b>",
    )
    digest = build_digest(report_with([evil]), TODAY)
    assert "<img" not in digest.html
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; &lt;b&gt;" in digest.html
    slack_text = digest.slack["blocks"][1]["text"]["text"]
    assert "<img" not in slack_text
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; &lt;b&gt;" in slack_text


def test_site_health_section_and_subject():
    health = SiteHealth("rival-x", 2, ("parse_error",))
    digest = build_digest(report_with(failing=[health]), TODAY)
    assert digest.subject == "Competitor prices 18 Oct 2026: no changes, 1 site failing"
    assert "SITE HEALTH\n- NEW: rival-x failed 2 runs in a row (parse_error)" in digest.text
    assert "Site health" in digest.html

    ongoing = SiteHealth("rival-x", 5, ("parse_error",))
    assert (
        "- rival-x failed 5 runs in a row"
        in build_digest(report_with(failing=[ongoing]), TODAY).text
    )


def test_empty_digest_says_nothing_changed():
    digest = build_digest(report_with(), TODAY)
    assert digest.subject == "Competitor prices 18 Oct 2026: no changes"
    assert "Nothing changed since the last check." in digest.text
    assert "Nothing changed since the last check." in digest.html
    assert digest.slack["blocks"][1]["text"]["text"] == "Nothing changed since the last check."


def test_subject_pluralisation():
    one = Change("out_of_stock", product(), "Mug")
    mine = ProductRow(1, "rival-x", "http://127.0.0.1:8000/product/x", None, Decimal("2"), True)
    undercut = Change("undercut", mine, "Mug", new_price=Decimal("1"), currency="USD")
    assert build_digest(report_with([one]), TODAY).subject.endswith(": 1 change")
    two_sites = [SiteHealth("a", 2, ("blocked",)), SiteHealth("b", 3, ("blocked",))]
    subject = build_digest(report_with([undercut, undercut], two_sites), TODAY).subject
    assert subject.endswith(": 2 changes, 2 undercuts, 2 sites failing")


def test_slack_payload_shape(report_after):
    payload = build_digest(report_after(*THREE_CHANGES), TODAY).slack
    assert payload["text"].startswith("Competitor prices 18 Oct 2026")
    types = [b["type"] for b in payload["blocks"]]
    assert types == ["header", "section", "section", "section", "context"]
    assert "<http://127.0.0.1:8000/product/blue-mug|view>" in payload["blocks"][1]["text"]["text"]


# --- when to send (F8) ------------------------------------------------------------------


def test_should_send():
    assert should_send(report_with([Change("out_of_stock", product(), "Mug")]), False)
    assert not should_send(report_with(), False)
    assert should_send(report_with(), True)  # always_send_summary
    assert should_send(report_with(failing=[SiteHealth("a", 2, ("blocked",))]), False)
    # A site already reported as failing doesn't force a send every day.
    assert not should_send(report_with(failing=[SiteHealth("a", 3, ("blocked",))]), False)


# --- secrets ----------------------------------------------------------------------------


def test_smtp_settings_defaults():
    s = smtp_settings(ENV)
    assert (s.host, s.port, s.user, s.sender, s.security) == (
        "smtp.example.com", 587, "tracker@example.com", "tracker@example.com", "starttls",
    )  # fmt: skip


def test_smtp_settings_ssl_port_and_explicit_from():
    s = smtp_settings({**ENV, "SMTP_PORT": "465", "SMTP_FROM": "Tracker <t@example.com>"})
    assert (s.port, s.security, s.sender) == (465, "ssl", "Tracker <t@example.com>")


def test_smtp_settings_without_login_for_local_relay():
    s = smtp_settings({"SMTP_HOST": "localhost", "SMTP_PORT": "1025", "SMTP_SECURITY": "none",
                       "SMTP_FROM": "t@localhost"})  # fmt: skip
    assert (s.user, s.password, s.security) == (None, None, "none")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "SMTP_HOST, SMTP_FROM (or SMTP_USER) not set"),
        ({"SMTP_HOST": "h", "SMTP_USER": "u"}, "SMTP_PASSWORD not set"),
        ({**ENV, "SMTP_PORT": "abc"}, "SMTP_PORT must be a number (got 'abc')"),
        ({**ENV, "SMTP_SECURITY": "tls"}, "SMTP_SECURITY must be starttls, ssl or none"),
    ],
)
def test_smtp_settings_errors(env, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        smtp_settings(env)


def test_slack_webhook_checks():
    assert slack_webhook({"SLACK_WEBHOOK_URL": WEBHOOK}) == WEBHOOK
    with pytest.raises(ValueError, match="SLACK_WEBHOOK_URL is not set"):
        slack_webhook({})
    with pytest.raises(ValueError, match=re.escape("should start with https://hooks.slack.com/")):
        slack_webhook({"SLACK_WEBHOOK_URL": "http://evil.example/hook"})


def test_missing_secrets_only_for_enabled_channels():
    assert missing_secrets(with_alerts(email=False, slack=False), {}) == []
    assert missing_secrets(with_alerts(email=True), ENV) == []
    problems = missing_secrets(with_alerts(email=True, slack=True), {})
    assert len(problems) == 2
    assert problems[0].startswith("email is enabled but SMTP_HOST")
    assert problems[1].startswith("slack is enabled but SLACK_WEBHOOK_URL")


# --- sending ----------------------------------------------------------------------------


class FakeSMTP:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.logins: list[tuple[str, str]] = []
        self.sent = []

    def __call__(self, settings):
        self.settings = settings
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if isinstance(self.fail, smtplib.SMTPAuthenticationError):
            raise self.fail
        self.logins.append((user, password))

    def send_message(self, message):
        if self.fail is not None:
            raise self.fail
        self.sent.append(message)


def test_send_email_builds_multipart_message(report_after):
    digest = build_digest(report_after(*THREE_CHANGES), TODAY)
    smtp = FakeSMTP()
    result = send_email(digest, ["a@example.com", "b@example.com"], smtp_settings(ENV), smtp)

    assert result == Delivery("email", True, "sent to 2 recipients")
    assert smtp.logins == [("tracker@example.com", "app-password")]
    [message] = smtp.sent
    assert message["Subject"] == digest.subject
    assert message["From"] == "tracker@example.com"
    assert message["To"] == "a@example.com, b@example.com"
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert "rival-x is out of stock on Red Mug" in plain
    assert "max-width:600px" in html


def test_send_email_without_login():
    settings = smtp_settings({"SMTP_HOST": "localhost", "SMTP_SECURITY": "none",
                              "SMTP_FROM": "t@localhost"})  # fmt: skip
    smtp = FakeSMTP()
    assert send_email(build_digest(report_with(), TODAY), ["a@x.com"], settings, smtp).ok
    assert smtp.logins == []


def test_send_email_auth_failure_has_a_hint():
    smtp = FakeSMTP(fail=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    result = send_email(build_digest(report_with(), TODAY), ["a@x.com"], smtp_settings(ENV), smtp)
    assert not result.ok
    assert "Gmail needs an app password" in result.detail


@pytest.mark.parametrize(
    "error", [smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no")}), ConnectionRefusedError()]
)
def test_send_email_other_failures_are_reported(error):
    result = send_email(
        build_digest(report_with(), TODAY), ["a@x.com"], smtp_settings(ENV), FakeSMTP(error)
    )
    assert not result.ok
    assert result.detail.startswith(type(error).__name__)


def slack_client(status=200, text="ok", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_slack_posts_payload():
    seen = []
    digest = build_digest(report_with(), TODAY)
    result = send_slack(digest, WEBHOOK, slack_client(seen=seen))
    assert result == Delivery("slack", True, "posted")
    assert str(seen[0].url) == WEBHOOK
    assert seen[0].read().decode().startswith('{"text":"Competitor prices')


def test_send_slack_error_status():
    result = send_slack(build_digest(report_with(), TODAY), WEBHOOK, slack_client(404, "no_team"))
    assert result == Delivery("slack", False, "HTTP 404: no_team")


def test_send_slack_network_error():
    def boom(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    result = send_slack(build_digest(report_with(), TODAY), WEBHOOK, client)
    assert not result.ok
    assert result.detail == "ConnectError: down"


def test_send_slack_without_client_makes_a_real_request():
    # The conftest network guard stops the real request; seeing it proves the URL is used.
    with pytest.raises(RuntimeError, match=re.escape("POST https://hooks.slack.com/services/T000")):
        send_slack(build_digest(report_with(), TODAY), WEBHOOK)


def test_deliver_email_only():
    smtp = FakeSMTP()
    digest = build_digest(report_with(), TODAY)
    results = deliver({"email": digest}, with_alerts(email=True), ENV, connect=smtp)
    assert [r.channel for r in results] == ["email"]
    assert results[0].ok


def test_deliver_both_channels():
    env = {**ENV, "SLACK_WEBHOOK_URL": WEBHOOK}
    digest = build_digest(report_with(), TODAY)
    results = deliver(
        {"email": digest, "slack": digest},
        with_alerts(email=True, slack=True),
        env,
        connect=FakeSMTP(),
        http_client=slack_client(),
    )
    assert [(r.channel, r.ok) for r in results] == [("email", True), ("slack", True)]


def test_deliver_reports_missing_secrets_without_raising():
    digest = build_digest(report_with(), TODAY)
    results = deliver({"email": digest, "slack": digest}, with_alerts(email=True, slack=True), {})
    assert [(r.channel, r.ok) for r in results] == [("email", False), ("slack", False)]
    assert "SMTP_HOST" in results[0].detail


def test_deliver_nothing_enabled():
    assert deliver({}, with_alerts(email=False), ENV) == []


class FakeConnection:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.tls = False

    def starttls(self, context):
        self.tls = True
        self.kwargs["context"] = context


@pytest.mark.parametrize(
    ("security", "cls", "tls"),
    [("starttls", "SMTP", True), ("none", "SMTP", False), ("ssl", "SMTP_SSL", False)],
)
def test_connect_uses_the_right_transport(monkeypatch, security, cls, tls):
    monkeypatch.setattr(smtplib, "SMTP", FakeConnection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("FakeSSL", (FakeConnection,), {}))
    settings = smtp_settings({**ENV, "SMTP_SECURITY": security})
    conn = connect_smtp(settings)
    assert type(conn).__name__ == ("FakeConnection" if cls == "SMTP" else "FakeSSL")
    assert conn.args[:2] == ("smtp.example.com", 587)
    assert conn.tls is tls


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_tls_trusts_certifi_even_without_system_certificates(monkeypatch, security):
    """python.org's macOS Python ships with no root certificates until 'Install Certificates'
    is run, so a default SSL context can't verify smtp.gmail.com. Certifi's bundle fixes that."""
    monkeypatch.setattr(smtplib, "SMTP", FakeConnection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", type("FakeSSL", (FakeConnection,), {}))
    conn = connect_smtp(smtp_settings({**ENV, "SMTP_SECURITY": security}))

    context = conn.kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    certifi_count = len(ssl.create_default_context(cafile=certifi.where()).get_ca_certs())
    assert len(context.get_ca_certs()) >= certifi_count > 100


def test_carried_items_are_escaped_and_labelled():
    from tracker.store import PendingRun

    evil = {"section": "Other changes", "text": "renamed '<script>x</script>'", "url": "http://s/x"}
    carried = [PendingRun("r0", "2026-10-17T06:00:00+00:00", [evil])]
    digest = build_digest(report_with(), TODAY, carried)

    assert digest.subject.endswith(": no changes, 1 carried over")
    assert "17 Oct 2026 06:00 UTC\n- [Other changes] renamed '<script>x</script>'" in digest.text
    assert "<script>" not in digest.html
    assert "renamed &#x27;&lt;script&gt;x&lt;/script&gt;&#x27;" in digest.html
    assert "Nothing changed since the last check." not in digest.text
    slack_text = digest.slack["blocks"][1]["text"]["text"]
    assert "<script>" not in slack_text
    assert "(17 Oct 2026 06:00 UTC)" in slack_text


def test_delivery_detail_never_contains_addresses():
    """Delivery details are printed and logged, and logs are public artifacts on Actions."""
    single = send_email(
        build_digest(report_with(), TODAY), ["a@x.com"], smtp_settings(ENV), FakeSMTP()
    )
    assert single.detail == "sent to 1 recipient"


def test_smtp_errors_are_scrubbed_of_addresses_and_username():
    refused = smtplib.SMTPRecipientsRefused({"secret@x.com": (550, b"no such user secret@x.com")})
    result = send_email(
        build_digest(report_with(), TODAY), ["secret@x.com"], smtp_settings(ENV), FakeSMTP(refused)
    )
    assert not result.ok
    assert "secret@x.com" not in result.detail
    assert "<recipient>" in result.detail

    sender = smtplib.SMTPSenderRefused(
        553, b"tracker@example.com not allowed", "tracker@example.com"
    )
    result = send_email(
        build_digest(report_with(), TODAY), ["a@x.com"], smtp_settings(ENV), FakeSMTP(sender)
    )
    assert "tracker@example.com" not in result.detail  # SMTP_USER is private too
