# Progress

Updated after every step. Requirement IDs refer to the spec (F1–F12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-18, session 3 in progress._

## 1. Done, by requirement

| ID | Requirement | Status | Where | Notes |
|---|---|---|---|---|
| F1 | Load config | ✅ done | `config.py` | Pydantic validation, one readable line per bad field, rejects secret-looking keys, checks CSS selectors compile, product host must match its site. `fetch.contact` + `timeout_seconds` added in session 2. |
| F2 | Fetch pages | ✅ done | `fetch.py` | httpx, timeout, 3 retries with 1/2/4 s backoff on top of the ≥5 s per-site delay, `Retry-After` honoured (cap 60 s). Optional Playwright browser mode (tested with a fake browser only). |
| F3 | Respect robots.txt | ✅ done | `fetch.py` | Fetched once per site per run; `Crawl-delay` honoured; 401/403 → disallow; 5xx/unreachable → skip site. |
| F4 | Parse | ✅ done | `parse.py` | Title, price (Decimal + currency), stock status + qty via config selectors. 36 price formats, 22 stock phrases tested. **Fixed in session 3:** price no longer picks up percentages, multi-buy counts, ratings or discount amounts; numbers next to a currency mark are preferred. |
| F5 | Store | ✅ done | `store.py` | `products` + append-only `observations` (UPDATE/DELETE blocked by triggers), prices stored as TEXT Decimals, status values checked by the DB. Products removed from config are marked inactive, history kept. `database:` path added to config. In-memory scratch copy for `--dry-run`. |
| F6 | Detect changes | ⏳ session 3 | — | |
| F7 | Compare with own price | ⏳ session 3 | — | |
| F8 | Alert | ⏳ session 4 | — | |
| F9 | Export to Sheets | ⏳ session 4 | — | |
| F10 | Schedule | ⏳ session 5 | — | CI workflow (lint + tests) exists; the daily cron workflow doesn't yet. |
| F11 | CLI | 🟡 partial | `cli.py` | `check-config` and `test-selectors` (with `--html`, `--save`) done. `run` (session 4) and `history` (session 3) are stubs that exit 1. |
| F12 | Health report | ⏳ session 4 | — | |

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, F1, F11 shell, CI | ✅ | `89af1d0` … `196327b` |
| 2 | F2, F3, F4, `test-selectors`, fixtures, ARCHITECTURE.md | ✅ | `1e5fdc8` … `ec49626` |
| 3 | F4 price fix; F5, F6, F7, `history`, mock site | 🔄 in progress | |

## 2. Test results

Run on 2026-10-18 with `uv run pytest --cov=tracker` (after F5):

| Suite | Tests | Result |
|---|---|---|
| `test_config.py` | 36 | ✅ pass |
| `test_fetch.py` | 32 | ✅ pass |
| `test_parse.py` | 83 | ✅ pass |
| `test_cli.py` | 16 | ✅ pass |
| `test_store.py` | 15 | ✅ pass |
| **Total** | **182** | **✅ all pass** |

Coverage: 100% on `config.py`, `fetch.py`, `parse.py` and `cli.py` (spec target is 80%; the
Playwright loader is excluded because it needs a real browser). `ruff check` and
`ruff format --check` are clean.

Tests make no network calls: real HTTP is blocked in `tests/conftest.py`.

**Spec acceptance criteria (automated) so far**

- [x] Price normalisation handles at least 10 formats
- [x] Stock normalisation handles in stock, out of stock, quantity text and unknown
- [x] Parsing works on saved fixtures from each demo site (books.toscrape.com, web-scraping.dev; mock site in session 3)
- [ ] Change detection cases (session 3)
- [x] Invalid config produces a readable error naming the field
- [x] A failed fetch is recorded and does not stop other products (fetch level; the run loop arrives in session 4)
- [x] robots.txt disallow is respected
- [x] Test coverage ≥ 80% on parse, changes and config (`changes.py` doesn't exist yet)

## 3. Next

Session 3 (approved), in this order:

- ✅ **F4 fix:** price picks the right number in promo text.

- ✅ **F5** SQLite store.
- **F6** Change detection against the last successful observation, with the configurable threshold.
- **F7** Undercut flag when a competitor goes below `my_price`.
- **F11** `tracker history <product>`.
- Mock competitor site whose prices and stock change on demand, plus its fixtures.

## 4. Blocked or waiting on you

- **Waiting:** your review of the session 2 points: price/stock edge cases in `tests/test_parse.py`, the politeness rules in `fetch.py`, and a live `test-selectors` run.
- **Not blocking yet:** no GitHub remote, so CI has only run locally. Needed by session 5 (F10); `gh` isn't installed.
- **Open questions from the spec** (needed by sessions 4–5): GitHub Actions or a paid host, email only or Slack too for the demo, and whether to publish the mock site.
