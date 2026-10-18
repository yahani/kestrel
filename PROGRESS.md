# Progress

Updated after every step. Requirement IDs refer to the spec (F1–F12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-18, end of session 5. All 12 requirements are built; one acceptance check waits on tomorrow's scheduled run._

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
| F8 | Alert | ✅ done (real send through Gmail accepted) | `alerts.py` | One digest per run as text + HTML email (sections: Undercutting you, Price changes, Stock, Other, Couldn't check, Site health) and a Slack Block Kit payload. Competitor text is escaped. Not sent when nothing changed unless `always_send_summary` (a newly failing site also triggers a send). SMTP via STARTTLS/SSL (trusting the system store **plus certifi**, since python.org's macOS Python ships with no root certificates) with secrets from env/`.env`; Slack sender built, off in config. `run` sends it; exits 1 if a channel fails. Delivery details give a **count, never addresses** (`sent to 1 recipient`), and SMTP errors are scrubbed of addresses, because logs are public artifacts on Actions. **A digest that fails to send is carried into the next one that gets through, exactly once, per channel**, under "From earlier runs that couldn't be sent" with the original run's time (append-only `runs` + `deliveries` tables). |
| F9 | Export to Sheets | ✅ done (real export to Google Sheets verified 2026-10-18) | `sheets.py` | Service account from `GOOGLE_SERVICE_ACCOUNT_JSON`. Appends each run's observations to "Observations" and rewrites "Latest" (one row per active product, with an `undercuts_me` column). Creates missing tabs with a frozen header. RAW writes, so a competitor title can't run as a formula. Accepts the sheet ID or its URL. Price columns are formatted `0.00`. Tested against a fake whose method signatures are checked against gspread 6.2.1, and against a real sheet: run `20261018T100321Z-318c90` wrote 4 rows, and Latest matched the database with 0 mismatches. |
| F10 | Schedule | 🟡 manual trigger ✅; first scheduled run due 2026-10-19 06:17 UTC | `.github/workflows/daily.yml`, `tracker ci-config`, `tracker data` | Daily cron at 06:17 UTC plus a manual **Run workflow** button. One run at a time. Restores `tracker.db` from the `data` branch, and saves it back even when the run fails, but only after a successful restore (force-pushed as a single commit, so the repo doesn't grow). Email and Sheets come from repository secrets; the recipient is never committed. Logs are uploaded as an artifact kept 30 days. Two manual runs passed (39120192167, 39120377564): the first started empty and saved 40 KB to `data`; the second restored it and saved 49 KB. The DB now holds 2 runs and 40 observations, and `data` is still 1 commit. |
| F11 | CLI | ✅ done | `cli.py`, `pipeline.py` | `run [--dry-run]`, `check-config` (also warns about missing secrets for enabled channels), `test-selectors <url> [--html] [--save]`, `history <url\|sku\|url part> [--limit]`. `--dry-run` previews the digest, saves nothing and sends nothing. Secrets are read from `.env` next to the config or in the current folder; real env vars win. |
| F12 | Health report | ✅ done | `report.py` | Summary (checked, ok, failed, changes, seconds). A site is failing in a run when **all** its products fail; at 2 runs in a row → an alert whose advice depends on why it failed (parse errors → "selectors may have changed"; fetch, robots or blocking → "unreachable, blocking us or disallowing us in robots.txt"; mixed → check both), sent **once** (when the streak hits 2), then still listed while it lasts. |

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, F1, F11 shell, CI | ✅ | `89af1d0` … `196327b` |
| 2 | F2, F3, F4, `test-selectors`, fixtures, ARCHITECTURE.md | ✅ | `1e5fdc8` … `ec49626` |
| 3 | F4 price fix; F5, F6, F7, `history`, `run` (prints), mock site | ✅ | `1a3d1c6` … `fc46101` |
| 4 | F8 (email; Slack off), F9, F12, hooked into `run`; review fixes | ✅ | `50e735a` … `4d990ca` |
| 5 | F10 schedule, logs, Dependabot, secrets scan, handover | ✅ (scheduled run to confirm) | `d74ff31` … this commit |

## 2. Test results

Run on 2026-10-18 with `uv run pytest --cov=tracker` (client template step 1: `tracker data` / `ci-config` commands):

| Suite | Tests | Result |
|---|---|---|
| `test_config.py` | 37 | ✅ pass |
| `test_fetch.py` | 32 | ✅ pass |
| `test_parse.py` | 85 | ✅ pass |
| `test_cli.py` | 45 | ✅ pass |
| `test_store.py` | 29 | ✅ pass |
| `test_changes.py` | 46 | ✅ pass |
| `test_mock_site.py` | 18 | ✅ pass |
| `test_pipeline.py` | 14 | ✅ pass |
| `test_report.py` | 13 | ✅ pass |
| `test_alerts.py` | 40 | ✅ pass |
| `test_sheets.py` | 23 | ✅ pass |
| `test_carryover.py` | 9 | ✅ pass |
| `test_logs.py` | 7 | ✅ pass |
| `test_ci_command.py` | 10 | ✅ pass |
| `test_datasync.py` | 19 | ✅ pass |
| **Total** | **427** | **✅ all pass** |

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

- [x] `tracker run` on the books sandbox stores correct title, price and stock for 20 products. Checked 2026-10-18 with `demo/books-sandbox.yaml` (run `20261018T103642Z-274e4f`, 100.6 s, 21 requests): all 20 stored titles, prices, stock and quantities match the catalogue listing page, which is an independent source, with 0 mismatches.
- [x] Changing two prices and one stock level on the mock site produces one digest with exactly those three changes. Checked 2026-10-18 with the real mock server and a local SMTP server: exactly 1 email, 3 changes.
- [x] Running twice with no changes sends no alert (same check: runs 1 and 3 sent nothing).
- [x] Email digest arrives and reads clearly on a phone. Sent through Gmail on 2026-10-18; you confirmed it arrived and reads fine on your phone.
- [x] Google Sheet "Latest" tab matches the database. Checked 2026-10-18 by reading the real sheet back through the API: 4 rows, 0 mismatches on every field; headers frozen; prices are number cells.
- [ ] GitHub Actions run succeeds on schedule and on manual trigger. **Manual: ✅** (2 runs on 2026-10-18, with history restored between them). **Scheduled: waiting** for the first cron run, 2026-10-19 06:17 UTC; GitHub can delay scheduled runs by some minutes.
- [x] A fresh clone plus the README gets a stranger running in under 15 minutes. Checked 2026-10-18: cloned from GitHub into an empty folder with an **empty uv cache** and followed only the README quick start. Clone and install took 25 s, the tests 10 s, and the mock-shop demo with a dry-run digest preview 43 s: **78 s of commands** in total, and the clone stayed clean. (Assumes Python 3.12 and uv are installed; installing uv takes about a minute.)
- [x] No secrets in the repository history. Final gitleaks scan on 2026-10-18 of all commits on `main` and `data`, re-run after the review fixes: no leaks. The public `data` branch database contains no secret-looking strings. CI repeats the full-history scan on every push.

## 3. Next

All five build sessions are done. What's left:

- **Confirm the first scheduled run** (due 2026-10-19 06:17 UTC; GitHub can run scheduled
  jobs a few minutes late). Then tick the last acceptance box.
- **Portfolio deliverables from the spec** (yours to make; the repo has what you need):
  - 2-minute demo video: follow `docs/DEMO.md`.
  - One-page case study: problem, what it does, about 2 hours a week saved for 50
    products, and the setup price.
  - Productized listing text for Upwork Project Catalog or Fiverr.
- **Later, when a client asks** (v1 "out of scope" in the spec): visual rule builder,
  logins, SMS/WhatsApp, dashboards, and so on.

**Session 5 summary:**
- **F10:** daily Actions run with history on `data`. Two manual runs passed, and history
  was restored between them.
- **Logs:** JSON-lines, 30 days.
- **Security:** Dependabot weekly; gitleaks in CI.
- **Sheets:** "Latest" is now written before it's trimmed.
- **Handover:** the 20-product books check (20/20 match the catalogue); final README, setup
  guide and demo script; the fresh-clone check (78 s); the final secrets scan (clean).

## 4. Blocked or waiting on you

- **Review of `e52f357` (IN PROGRESS, 2 must-fix):**
  - ✅ **Must-fix 2:** the recipient is never printed or logged. Checked the 2 existing
    artifacts and console logs first: no addresses had leaked, because neither run sent
    email.
  - ✅ **Must-fix 1:** the data-branch restore could wipe history. It's now
    `scripts/data_branch.py`, tested against local bare repos (13 tests):
    - Only a missing branch (`ls-remote` exit 2) starts empty.
    - An unreachable remote, a branch with no `tracker.db`, or an unreadable database fails
      with no outputs, so the save step is skipped.
    - A restore writes to a temp file first, so it never leaves a 0-byte database.
    - A save refuses to write fewer observations than were restored.
  - Verified on GitHub (run 39121212626): restored 40 observations and saved 60, `data` is
    still 1 commit, and the log artifact has 0 email addresses.
  - ✅ **Should-fix items:**
    - README warns to use a private repo for real products, since the `data` branch is
      public, and explains the 60-day schedule pause.
    - `ci_config.py` prints a usage line.
    - The `fetch.contact` Gmail in `mock_site/tracker-config.yaml` was your deliberate choice
      (session 2), so it's left as it is.
- **Review of `6893824`: APPROVED** (no must-fix).
  - ✅ **Should-fix items done:**
    - The restore matches only `refs/heads/data`, so a `feature/data` branch is no longer
      mistaken for it.
    - Git's own error message is shown on failure, with the remote and any token redacted.
    - Checked read-only against the real `data` branch: restored 60 observations.
  - Not done (optional): pinning third-party actions to commit SHAs. Dependabot keeps the
    version tags current.
  - Its next instruction, step 1: confirm the first scheduled run after 2026-10-19 06:17
    UTC. A one-off check is scheduled for 12:20 Colombo time (06:50 UTC).
- **Waiting on GitHub:** the first scheduled Daily run, 2026-10-19 06:17 UTC.
- **Waiting on you:** a review of session 5 (see the session report), and the portfolio
  deliverables above.
- **Your accounts, all set up 2026-10-18:**
  - Gmail app password in `.env`.
  - Google service account key in `~/.config/price-tracker/`.
  - Repository secrets `SMTP_*` and `DIGEST_EMAIL_TO`.
  - `gh` logged in as `yahani`.
  - Revoke any of these from the same pages if needed (see `docs/SETUP.md`).
- **Not on Actions yet, by choice:** Sheets export. Your demo sheet holds mock-shop data, so
  the scheduled books run doesn't write to it. Add `GOOGLE_SERVICE_ACCOUNT_KEY` and
  `SHEETS_SPREADSHEET_ID` secrets (pointing at a separate sheet) to turn it on.
- **`REVIEW.md`:** written by your reviewer and gitignored. The session 4 review was acted
  on (`5aac81e`, `1c71b97`). A `/loop` (job `f6f48c88`) checks for a new review every 30
  minutes while this session is open.

## 5. Client template

Goal: this repo stays public (code + demo on practice sites). Each client gets a **private**
repo created from a template. Their config, `tracker.db` (on its `data` branch) and run logs
live only there. The template installs the tracker from this repo, pinned to a release tag.
Approved 2026-10-18. Decisions:
- The scripts become CLI commands, and the scripts are deleted.
- The privacy guard asks the GitHub API.
- A placeholder guard stops runs on an unedited config.
- `TRACKER_VERSION` is a separate file.
- v1.0.0 gets a GitHub Release.
- A private test client repo will be created.

- [x] `tracker data restore|save`, `tracker ci-config` and `tracker --version` added; the
  scripts are deleted and `daily.yml` uses the commands. Tests first (29 ported, plus
  fetch-failure and version tests). Coverage 100%.
- [x] The version comes from package metadata only. Installs cleanly with `uv tool install --python 3.12 git+…@<commit>`, from an empty cache and a throwaway tool folder. Every command works outside the repo; only the `tracker` package is installed. `--version` and the User-Agent both say 1.0.0. (Re-check from GitHub once the tag is pushed.)
- [x] Release v1.0.0 prepared: `pyproject.toml` set to 1.0.0, and `CHANGELOG.md` written.
  - [ ] Tag `v1.0.0` and its GitHub Release: waiting for your OK.
- [x] Template built locally at `../price-tracker-client-template`, as its own git repo
  with 3 commits; gitleaks is clean:
  - `config.yaml` with placeholders, marked as private client data.
  - `TRACKER_VERSION` (v1.0.0), `.gitignore`, `README.md`, `CLIENT_SETUP.md`.
  - `daily.yml` with a **guard** job (API privacy check, template skip, placeholder check)
    and a **run** job, plus `check.yml`.
  - The guard's shell logic was tested under `bash -e` with a fake `gh`: template → skip,
    public → fail, placeholders → fail (all 6 lines flagged, no comment lines), private and
    filled in → ok, API failure → fail closed.
  - The config validates with the installed v1.0.0.
  - Not checked: actionlint (it wouldn't build; same missing-certificates problem), so the
    real run in the test client repo is the check.
- [ ] Push this repo and the tag (your OK) → create the private template repo (your OK) →
  test client repo and one run by hand.
- [x] ARCHITECTURE.md: a "public package → private client repos" diagram and data table.
  The README warning points to the template and its `CLIENT_SETUP.md`, and lists the new
  commands and `CHANGELOG.md`.
