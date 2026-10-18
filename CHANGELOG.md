# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Client repos pin a version tag in their
`TRACKER_VERSION` file; see the client template's `CLIENT_SETUP.md`.

## [1.0.0] - 2026-10-18

First release. It covers all twelve requirements in the spec (F1–F12) and is installable
with `uv tool install git+https://github.com/yahani/price-tracker@v1.0.0`.

### Added
- **Config (F1):** `config.yaml` validated with Pydantic, one readable error per field.
  CSS selectors are checked, secret-looking keys are rejected, and a product's host must
  match its site.
- **Fetching (F2, F3):** httpx with timeouts and 3 retries with backoff on top of a per-site
  delay of at least 5 s. `Retry-After` is honoured. robots.txt, including `Crawl-delay`, is
  obeyed. An honest User-Agent with a contact address. Optional Playwright browser mode.
- **Parsing (F4):** title, price (`Decimal` plus currency) and stock (status plus quantity)
  from CSS selectors. Promo text such as "Save 15%", "2 for $30" and ratings is ignored when
  reading prices.
- **Storage (F5):** SQLite with append-only observations, plus `runs` and `deliveries`
  tables, enforced by triggers.
- **Change detection (F6, F7):** price up and down with a threshold, out of stock, back in
  stock, title and currency changes, and new failures. Comparisons use the last successful
  check. An undercut is reported once when a competitor goes below `my_price`.
- **Alerts (F8):** one digest per run by email (text and HTML, readable on a phone) and/or
  Slack. A failed send is carried into the next digest that gets through.
- **Google Sheets (F9):** history appended to "Observations"; "Latest" written first and then
  trimmed. Prices are number cells formatted `0.00`, and competitor text is written RAW.
- **Schedule (F10):** daily GitHub Actions workflow, with history kept on a `data` branch.
- **CLI (F11):**
  - `tracker run [--dry-run]`, `check-config`, `test-selectors`, `history`.
  - `tracker data restore|save` and `tracker ci-config` for scheduled runs, so client repos
    use the installed package rather than copied scripts.
  - `tracker --version`.
- **Health (F12):** a run summary, and a one-time alert when a site fails two runs in a row.
  The advice depends on why it failed.
- JSON-lines logs, one file per day, kept 30 days.

### Security
- Secrets only come from environment variables or `.env`; tests can never see real ones.
- Digest recipients are never printed or logged: delivery details give a count, SMTP errors
  are scrubbed, and recipients are masked on Actions.
- `tracker data restore` starts empty history only when the branch is truly missing. Any
  other error fails without saving, and a save refuses to write fewer observations than were
  restored.
- SMTP over TLS trusts certifi as well as the system store.
- CI runs gitleaks over the full history; Dependabot checks dependencies weekly.

[1.0.0]: https://github.com/yahani/price-tracker/releases/tag/v1.0.0
