# Progress

Updated after every step. Requirement IDs refer to the spec (F1–F12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-18, end of session 4._

## 1. Done, by requirement

| ID | Requirement | Status | Where | Notes |
|---|---|---|---|---|
| F1 | Load config | ✅ done | `config.py` | Pydantic validation, one readable line per bad field, rejects secret-looking keys, checks CSS selectors compile, product host must match its site. `fetch.contact` + `timeout_seconds` added in session 2. |
| F2 | Fetch pages | ✅ done | `fetch.py` | httpx, timeout, 3 retries with 1/2/4 s backoff on top of the ≥5 s per-site delay, `Retry-After` honoured (cap 60 s). Optional Playwright browser mode (tested with a fake browser only). |
| F3 | Respect robots.txt | ✅ done | `fetch.py` | Fetched once per site per run; `Crawl-delay` honoured; 401/403 → disallow; 5xx/unreachable → skip site. |
| F4 | Parse | ✅ done | `parse.py` | Title, price (Decimal + currency), stock status + qty via config selectors. 36 price formats, 22 stock phrases tested. **Fixed in session 3:** a zero price is a `parse_error`; price no longer picks up percentages, multi-buy counts, ratings or discount amounts; numbers next to a currency mark are preferred. |
| F5 | Store | ✅ done | `store.py` | `products` + append-only `observations` (UPDATE/DELETE blocked by triggers), prices stored as TEXT Decimals, status values checked by the DB. Products removed from config are marked inactive, history kept. `database:` path added to config. In-memory scratch copy for `--dry-run`. Decimals stored in plain notation (`100`, never `1E+2`). |
| F6 | Detect changes | ✅ done | `changes.py` | Compared with the last *successful* observation: price up/down with signed % (threshold inclusive, default 1%), out of stock, back in stock, title change, currency change, new failure (repeat failures left to F12). First observation → no changes. Stock is compared with the last observation whose stock was known, so `in → unknown → out` still reports out of stock. |
| F7 | Compare with own price | ✅ done | `changes.py` | Price changes carry an `undercut` flag when the new price is below `my_price`. A separate `undercut` change fires only when a move under the threshold crosses below `my_price`, so one event is never reported twice. Only flagged when the competitor's currency matches the site currency (`my_price` is assumed to be in it). |
| F8 | Alert | ✅ done (real send through Gmail accepted) | `alerts.py` | One digest per run as text + HTML email (sections: Undercutting you, Price changes, Stock, Other, Couldn't check, Site health) and a Slack Block Kit payload. Competitor text is escaped. Not sent when nothing changed unless `always_send_summary` (a newly failing site also triggers a send). SMTP via STARTTLS/SSL (trusting the system store **plus certifi**, since python.org's macOS Python ships with no root certificates) with secrets from env/`.env`; Slack sender built, off in config. `run` sends it; exits 1 if a channel fails. |
| F9 | Export to Sheets | ✅ done (real export to Google Sheets verified 2026-10-18) | `sheets.py` | Service account from `GOOGLE_SERVICE_ACCOUNT_JSON`. Appends each run's observations to "Observations" and rewrites "Latest" (one row per active product, with an `undercuts_me` column). Creates missing tabs with a frozen header. RAW writes, so a competitor title can't run as a formula. Accepts the sheet ID or its URL. Price columns are formatted `0.00`. Tested against a fake whose method signatures are checked against gspread 6.2.1, and against a real sheet: run `20261018T100321Z-318c90` wrote 4 rows, and Latest matched the database with 0 mismatches. |
| F10 | Schedule | ⏳ session 5 | — | CI workflow (lint + tests) exists; the daily cron workflow doesn't yet. |
| F11 | CLI | ✅ done | `cli.py`, `pipeline.py` | `run [--dry-run]`, `check-config` (also warns about missing secrets for enabled channels), `test-selectors <url> [--html] [--save]`, `history <url\|sku\|url part> [--limit]`. `--dry-run` previews the digest, saves nothing and sends nothing. Secrets are read from `.env` next to the config or in the current folder; real env vars win. |
| F12 | Health report | ✅ done | `report.py` | Summary (checked, ok, failed, changes, seconds). A site is failing in a run when **all** its products fail; at 2 runs in a row → "selectors may have changed" alert, sent **once** (when the streak hits 2), then still listed while it lasts. |

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, F1, F11 shell, CI | ✅ | `89af1d0` … `196327b` |
| 2 | F2, F3, F4, `test-selectors`, fixtures, ARCHITECTURE.md | ✅ | `1e5fdc8` … `ec49626` |
| 3 | F4 price fix; F5, F6, F7, `history`, `run` (prints), mock site | ✅ | `1a3d1c6` … `fc46101` |
| 4 | F8 (email; Slack off), F9 (untested live), F12, hooked into `run` | ✅ | `50e735a` … this commit |

## 2. Test results

Run on 2026-10-18 with `uv run pytest --cov=tracker` (after the sheet price format fix):

| Suite | Tests | Result |
|---|---|---|
| `test_config.py` | 36 | ✅ pass |
| `test_fetch.py` | 32 | ✅ pass |
| `test_parse.py` | 85 | ✅ pass |
| `test_cli.py` | 38 | ✅ pass |
| `test_store.py` | 21 | ✅ pass |
| `test_changes.py` | 39 | ✅ pass |
| `test_mock_site.py` | 18 | ✅ pass |
| `test_pipeline.py` | 14 | ✅ pass |
| `test_report.py` | 8 | ✅ pass |
| `test_alerts.py` | 37 | ✅ pass |
| `test_sheets.py` | 21 | ✅ pass |
| **Total** | **349** | **✅ all pass** |

Coverage: 100% on every module in `src/tracker/` (spec target is 80%). Excluded: the
Playwright loader and `sheets.open_spreadsheet`, which need a real browser or real Google
credentials. `ruff check` and
`ruff format --check` are clean.

Tests make no network calls: real HTTP is blocked in `tests/conftest.py`.

**Spec acceptance criteria: automated**

- [x] Price normalisation handles at least 10 formats (36 tested)
- [x] Stock normalisation handles in stock, out of stock, quantity text and unknown
- [x] Parsing works on saved fixtures from each demo site (books.toscrape.com, web-scraping.dev, mock shop)
- [x] Change detection: up, down, below-threshold ignored, back in stock, out of stock, undercut, first observation (no alert)
- [x] Invalid config produces a readable error naming the field
- [x] A failed fetch is recorded and does not stop other products
- [x] robots.txt disallow is respected
- [x] Test coverage ≥ 80% on parse, changes and config (100%)

**Spec acceptance criteria: end to end, by hand**

- [ ] `tracker run` on the books sandbox stores correct title, price and stock for 20 products. Not run yet (the example config has 1 product); planned for session 5.
- [x] Changing two prices and one stock level on the mock site produces one digest with exactly those three changes. Checked 2026-10-18 with the real mock server and a local SMTP server: exactly 1 email, 3 changes.
- [x] Running twice with no changes sends no alert (same check: runs 1 and 3 sent nothing).
- [x] Email digest arrives and reads clearly on a phone. Sent through Gmail on 2026-10-18; you confirmed it arrived and reads fine on your phone.
- [x] Google Sheet "Latest" tab matches the database. Checked 2026-10-18 by reading the real sheet back through the API: 4 rows, 0 mismatches on every field; headers frozen; prices are number cells.
- [ ] GitHub Actions run succeeds on schedule and on manual trigger (session 5).
- [ ] A fresh clone plus the README gets a stranger running in under 15 minutes (session 5).
- [ ] No secrets in the repository history (scanned each session so far; final check in session 5).

## 3. Next

Session 4 is done. Session 5 needs a plan and your approval:

- **F10** GitHub Actions daily cron + manual trigger, with secrets from repository secrets.
  Needs a decision on where the SQLite history lives between runs (Actions runners start
  empty).
- Structured JSON-lines logs kept for 30 days (spec, "Reliability").
- Weekly dependency update check (spec, "Security").
- Final README pass and setup guide (the 15-minute fresh-clone check), demo script, and the
  20-product books sandbox check.
- Final secrets scan of the whole history.
- **Found during SMTP setup:** if sending the digest fails, that run's changes are already
  stored, so the next run won't report them again and the alert is lost. Fix: remember
  which runs' digests were delivered, and include undelivered changes in the next digest.

**By hand, session 4** (2026-10-18, real mock server + local SMTP server, real `smtplib`):
- Run 1: 4 ok, no email.
- Then blue-mug went to 19.50, green-teapot to 49.50 and red-mug to stock 0. Run 2 sent
  **1 email**: "3 changes, 1 undercut", with sections Undercutting you, Price changes and
  Stock.
- Run 3: no email.
- The HTML part reads clearly at phone width (390 px).

## 4. Blocked or waiting on you

- **Waiting:** approval of the session 5 plan (not written yet).
- **Waiting:** your review of session 4 (see the list in the session report).
- **Walkthroughs you asked for:**
  1. ✅ SMTP setup for the email digest: **done**. `.env` has all five `SMTP_*` values,
     and `check-config` finds them. The gitignored `mock_site/demo-email.yaml` sends to your
     Gmail. The first real send failed with `CERTIFICATE_VERIFY_FAILED` (this Python had no
     root certificates); fixed in code. **Real send succeeded 2026-10-18 09:48 UTC** through
     Gmail to yahanihwgm@gmail.com (run `20261018T094757Z-b46489`, 3 changes); the next
     run with no changes sent nothing. You confirmed it reads fine on your phone.
  2. ✅ Google service account setup for Sheets: **done**. The key is in
     `~/.config/price-tracker/` (outside the repo, mode 600), its path is in `.env`, and the
     sheet is shared with the service account. The real export worked first time.
- **Decided 2026-10-18:** email only for the demo digest (Slack built, off). Sheets is now
  tested for real.
- ✅ **Fixed:** sheet prices now show as `24.00`, not `24`. Checked on the real sheet;
  a second real run also appended to Observations (8 rows), and nothing was overwritten.
- **`REVIEW.md`:** an untracked file in the repo root that wasn't written in these sessions.
  It's left alone and uncommitted (last changed 12:13, reviewing `b3ceeb1`).
- **Needed for session 5:**
  - a GitHub repository (public or private) and a remote; `gh` isn't installed
  - the spec's open question: GitHub Actions (free, public repo) or a small paid host
  - whether to publish the mock site
