# Progress

Updated after every step. Requirement IDs refer to the spec (F1–F12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-18, session 4 in progress._

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
| F8 | Alert | 🟡 built, not yet in `run` | `alerts.py` | One digest per run as text + HTML email (sections: Undercutting you, Price changes, Stock, Other, Couldn't check, Site health) and a Slack Block Kit payload. Competitor text is escaped. Not sent when nothing changed unless `always_send_summary` (a newly failing site also triggers a send). SMTP via STARTTLS/SSL with secrets from env/`.env`; Slack sender built, off in config. **Not yet sent for real** (no SMTP account yet). |
| F9 | Export to Sheets | ⏳ session 4 | — | |
| F10 | Schedule | ⏳ session 5 | — | CI workflow (lint + tests) exists; the daily cron workflow doesn't yet. |
| F11 | CLI | 🟡 partial | `cli.py`, `pipeline.py` | `check-config`, `test-selectors` (`--html`, `--save`) and `history <url\|sku\|url part> [--limit]` done. `run [--dry-run]` fetches, stores and **prints** changes; alerts, Sheets and the health report hook in during session 4. `--dry-run` works on an in-memory copy, so nothing is written. |
| F12 | Health report | 🟡 built, not yet in `run` | `report.py` | Summary (checked, ok, failed, changes, seconds). A site is failing in a run when **all** its products fail; at 2 runs in a row → "selectors may have changed" alert, sent **once** (when the streak hits 2), then still listed while it lasts. |

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, F1, F11 shell, CI | ✅ | `89af1d0` … `196327b` |
| 2 | F2, F3, F4, `test-selectors`, fixtures, ARCHITECTURE.md | ✅ | `1e5fdc8` … `ec49626` |
| 3 | F4 price fix; F5, F6, F7, `history`, `run` (prints), mock site | ✅ | `1a3d1c6` … `fc46101` |
| 4 | F8 (email; Slack off), F9 (untested live), F12, hooked into `run` | 🔄 in progress | |

## 2. Test results

Run on 2026-10-18 with `uv run pytest --cov=tracker` (after F8):

| Suite | Tests | Result |
|---|---|---|
| `test_config.py` | 36 | ✅ pass |
| `test_fetch.py` | 32 | ✅ pass |
| `test_parse.py` | 85 | ✅ pass |
| `test_cli.py` | 26 | ✅ pass |
| `test_store.py` | 21 | ✅ pass |
| `test_changes.py` | 39 | ✅ pass |
| `test_mock_site.py` | 18 | ✅ pass |
| `test_pipeline.py` | 14 | ✅ pass |
| `test_report.py` | 8 | ✅ pass |
| `test_alerts.py` | 35 | ✅ pass |
| **Total** | **314** | **✅ all pass** |

Coverage: 100% on `config.py`, `fetch.py`, `parse.py` and `cli.py` (spec target is 80%; the
Playwright loader is excluded because it needs a real browser). `ruff check` and
`ruff format --check` are clean.

Tests make no network calls: real HTTP is blocked in `tests/conftest.py`.

**Spec acceptance criteria (automated) so far**

- [x] Price normalisation handles at least 10 formats
- [x] Stock normalisation handles in stock, out of stock, quantity text and unknown
- [x] Parsing works on saved fixtures from each demo site (books.toscrape.com, web-scraping.dev, mock shop)
- [x] Change detection: up, down, below-threshold ignored, back in stock, out of stock, undercut, first observation (no alert)
- [x] Invalid config produces a readable error naming the field
- [x] A failed fetch is recorded and does not stop other products (`test_pipeline.py`)
- [x] robots.txt disallow is respected
- [x] Test coverage ≥ 80% on parse, changes and config (`changes.py` doesn't exist yet)

## 3. Next

Session 4, approved: **email digest only, Slack built but switched off, Sheets built and
tested against a fake client but not against real Google.** In this order:

- ✅ Money shown as `$24.00` / `£51.77` / `LKR 4500.00` in change text and `history`.
- ✅ **F12** run summary + site health (hooked into `run` in the last step).
- ✅ **F8** digest + email/Slack senders (hooked into `run` in the last step).
- **F9** Sheets export: append observations, refresh "Latest".
- Hook all three into `tracker run`; `--dry-run` previews the digest without sending.

**By hand, session 3** (done 2026-10-18 against the real mock server on localhost): run 1
gave 4 ok and 0 changes. Then blue-mug went to 19.50, green-teapot to 49.50 and red-mug to
stock 0, and run 2 gave **exactly those 3 changes**. Run 3 gave 0 changes, and `history
MUG-BLUE` showed all 3 observations. Each run took 20 s (5 s delay × 4 products).

## 4. Blocked or waiting on you

- **Waiting:** your review of session 3 (see the list in the session report).
- **Decided 2026-10-18:** email only for the demo digest; Sheets built and left untested.
- **Later, walkthroughs you asked for:** (1) SMTP setup for the email digest, (2) Google
  service account setup for Sheets. Until then, "email arrives on a phone" and "Sheet matches
  the database" stay unticked.
- **`REVIEW.md`:** an untracked file in the repo root that wasn't written in this session.
  It's left alone and uncommitted. Its 5 "should fix" points were each checked and fixed in
  `61ff94e`.
- **Not blocking yet:** no GitHub remote, so CI has only run locally. Needed by session 5
  (F10); `gh` isn't installed.
- **Open questions from the spec** (needed by session 5): GitHub Actions or a paid host,
  and whether to publish the mock site.
