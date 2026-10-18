"""A digest that fails to send is carried into the next one that gets through, exactly once."""

import re

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from tracker import cli
from tracker.cli import app

runner = CliRunner()
SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "t@example.com", "SMTP_PASSWORD": "x"}
WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
SLUGS = ("blue-mug", "red-mug", "green-teapot", "coaster-set")


class FlakySMTP:
    """Records sent messages; raises ConnectionRefusedError while `down` is True."""

    def __init__(self):
        self.down = False
        self.sent = []

    def __call__(self, settings):
        if self.down:
            raise ConnectionRefusedError(61, "Connection refused")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.sent.append(message)

    def bodies(self) -> list[str]:
        return [m.get_body(("plain",)).get_content() for m in self.sent]


@pytest.fixture
def world(mock_shop, mock_config_file, monkeypatch):
    data = yaml.safe_load(mock_config_file.read_text())
    data["alerts"]["email"] = {"enabled": True, "to": ["owner@example.com"]}
    mock_config_file.write_text(yaml.safe_dump(data, sort_keys=False))
    smtp = FlakySMTP()
    monkeypatch.setattr(cli, "_make_fetcher", mock_shop.fetcher)
    monkeypatch.setattr(cli, "_smtp_connect", smtp)
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    return mock_shop, mock_config_file, smtp


def run(config, *extra):
    return runner.invoke(app, ["run", "-c", str(config), *extra])


def set_channels(config, email=True, slack=False):
    data = yaml.safe_load(config.read_text())
    data["alerts"]["email"] = {"enabled": email, "to": ["owner@example.com"] if email else []}
    data["alerts"]["slack"] = {"enabled": slack}
    config.write_text(yaml.safe_dump(data, sort_keys=False))


UNDERCUT = "rival-x cut Cork Coaster Set from $12.00 to $8.00 (-33.3%), below your $11.00"


def test_review_repro_undercut_is_emailed_after_smtp_recovers(world):
    shop, config, smtp = world
    run(config)
    shop.set("coaster-set", price="8.00")
    smtp.down = True
    failed = run(config)
    assert failed.exit_code == 1
    assert "email: ✗ ConnectionRefusedError" in failed.output
    assert "undelivered changes will be included in the next digest" in failed.output

    smtp.down = False
    recovered = run(config)
    assert recovered.exit_code == 0, recovered.output
    assert "email: carrying over 1 item(s) from 1 earlier run(s)" in recovered.output
    [message] = smtp.sent
    assert message["Subject"].endswith(": no changes, 1 carried over")
    body = smtp.bodies()[0]
    assert "FROM EARLIER RUNS THAT COULDN'T BE SENT" in body
    assert f"- [Undercutting you] {UNDERCUT}" in body
    # Labelled with the failed run's own time, on the line after the section title.
    label = body.split("FROM EARLIER RUNS THAT COULDN'T BE SENT\n")[1].splitlines()[0]
    assert re.fullmatch(r"\d{2} \w{3} \d{4} \d{2}:\d{2} UTC", label), label


def test_carried_items_are_sent_exactly_once(world):
    shop, config, smtp = world
    run(config)
    shop.set("coaster-set", price="8.00")
    smtp.down = True
    run(config)
    smtp.down = False
    run(config)
    third = run(config)
    assert "digest: not sent (nothing changed)" in third.output
    assert len(smtp.sent) == 1
    assert sum(body.count(UNDERCUT) for body in smtp.bodies()) == 1


def test_carried_items_join_new_changes_in_one_digest(world):
    shop, config, smtp = world
    run(config)
    shop.set("coaster-set", price="8.00")
    smtp.down = True
    run(config)
    smtp.down = False
    shop.set("red-mug", stock=0)
    run(config)
    [body] = smtp.bodies()
    assert "STOCK\n- rival-x is out of stock on Red Mug" in body
    assert f"- [Undercutting you] {UNDERCUT}" in body
    assert smtp.sent[0]["Subject"].endswith(": 1 change, 1 carried over")


def test_repeated_failures_accumulate_and_are_sent_once(world):
    shop, config, smtp = world
    run(config)
    smtp.down = True
    shop.set("coaster-set", price="8.00")
    run(config)
    shop.set("red-mug", stock=0)
    run(config)
    run(config)  # nothing new, still down: nothing extra to carry
    smtp.down = False
    final = run(config)
    assert "email: carrying over 2 item(s) from 2 earlier run(s)" in final.output
    [body] = smtp.bodies()
    assert body.index(UNDERCUT) < body.index("rival-x is out of stock on Red Mug")  # oldest first
    assert run(config).output.count("carrying over") == 0


def test_dry_run_neither_sends_nor_marks(world):
    shop, config, smtp = world
    run(config)
    shop.set("coaster-set", price="8.00")
    smtp.down = True
    run(config)
    smtp.down = False

    dry = run(config, "--dry-run")
    assert dry.exit_code == 0
    assert smtp.sent == []
    assert "FROM EARLIER RUNS THAT COULDN'T BE SENT" in dry.output  # preview shows it

    run(config)
    assert len(smtp.sent) == 1 and UNDERCUT in smtp.bodies()[0]  # still pending after dry run


def test_dry_run_during_an_outage_records_no_run(world):
    shop, config, smtp = world
    run(config)
    shop.set("coaster-set", price="8.00")
    run(config, "--dry-run")  # sees the change, but must not record it as due
    run(config)  # the real run sees and sends it as a normal change
    [body] = smtp.bodies()
    assert "FROM EARLIER RUNS" not in body
    assert f"- {UNDERCUT}" in body


def test_new_site_health_alert_is_carried_too(world):
    shop, config, smtp = world
    for slug in SLUGS:
        shop.overrides[f"/product/{slug}"] = httpx.Response(200, text="<html></html>")
    smtp.down = True
    run(config)
    run(config)  # the run that raises "NEW: rival-x failed 2 runs in a row"
    smtp.down = False
    run(config)
    [body] = smtp.bodies()
    assert "- [Site health] rival-x failed 2 runs in a row" in body


def test_nothing_is_pending_when_no_channel_was_enabled(world):
    shop, config, smtp = world
    set_channels(config, email=False)
    run(config)
    shop.set("coaster-set", price="8.00")
    no_channel = run(config)
    assert "digest: no channel enabled" in no_channel.output
    assert no_channel.exit_code == 0

    set_channels(config, email=True)
    shop.set("red-mug", stock=0)
    run(config)
    [body] = smtp.bodies()
    assert UNDERCUT not in body  # runs from before email was enabled aren't carried
    assert "FROM EARLIER RUNS" not in body


def test_pending_is_tracked_per_channel(world, monkeypatch):
    shop, config, smtp = world
    set_channels(config, email=True, slack=True)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    slack_posts = []
    slack_up = {"ok": False}

    def slack(request):
        slack_posts.append(request.read().decode())
        return httpx.Response(200 if slack_up["ok"] else 500, text="ok" if slack_up["ok"] else "x")

    monkeypatch.setattr(cli, "_slack_client", httpx.Client(transport=httpx.MockTransport(slack)))
    run(config)
    shop.set("coaster-set", price="8.00")
    partial = run(config)
    assert "email: ✓" in partial.output and "slack: ✗ HTTP 500" in partial.output
    assert partial.exit_code == 1

    slack_up["ok"] = True
    run(config)
    assert len(smtp.sent) == 1  # email already had it; not repeated
    assert "From earlier runs that couldn't be sent" in slack_posts[-1]
    assert "Cork Coaster Set" in slack_posts[-1]
