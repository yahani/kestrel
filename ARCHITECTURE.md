# Architecture

How the Competitor Price and Stock Tracker is put together. This file is updated at the end of
every build session; parts not built yet are marked **planned**.

| Session | Scope | Status |
|---|---|---|
| 1 | Repo skeleton, config loading (F1), CLI shell (F11), CI | ✅ done |
| 2 | Fetching (F2), robots.txt (F3), parsing (F4), `test-selectors` | ✅ done |
| 3 | Price fix (F4), SQLite store (F5), change detection (F6, F7), `history` + `run` printing changes (F11), mock competitor site | ✅ done |
| 4 | Email/Slack digest (F8), Sheets export (F9), health report (F12), hooked into `run` | planned |
| 5 | GitHub Actions schedule (F10), README, demo | planned |

## The daily pipeline

One command (`tracker run`) runs a straight pipeline. A product that fails at any step is
recorded and skipped; it never stops the other products. `pipeline.run_once()` drives it;
today `tracker run` prints the changes, and session 4 sends them as a digest instead.

```mermaid
flowchart LR
    cfg[config.yaml] --> load["Load config<br/>config.py ✅"]
    load --> fetch["Fetch<br/>fetch.py ✅"]
    fetch --> parse["Parse<br/>parse.py ✅"]
    parse --> store[("Store<br/>store.py ✅")]
    store --> detect["Detect changes<br/>changes.py ✅"]
    detect --> alert["Alert digest<br/>alerts.py · planned"]
    detect --> sheets["Google Sheet<br/>sheets.py · planned"]
    detect --> report["Run report<br/>report.py · planned"]

    classDef planned stroke-dasharray: 5 5,color:#888
    class alert,sheets,report planned
```

## Modules

```mermaid
flowchart TD
    cli["cli.py<br/>Typer commands"] --> pipeline["pipeline.py<br/>run_once(), observe(), detect()"]
    cli --> config["config.py<br/>Pydantic models, load_config()"]
    cli --> store
    pipeline --> fetch["fetch.py<br/>Fetcher, Throttle, robots rules"]
    pipeline --> parse["parse.py<br/>parse_page(), normalise_*()"]
    pipeline --> store["store.py<br/>Store, ProductRow, Observation"]
    pipeline --> changes["changes.py<br/>detect_changes(), Change"]
    changes --> store
    fetch --> config
    parse --> config
    store --> sqlite[("SQLite")]
    fetch -. "fetch_mode: browser" .-> pw["Playwright<br/>(optional extra)"]
    fetch --> httpx["httpx"]
    parse --> bs4["BeautifulSoup"]
```

| Module | Responsibility | Never does |
|---|---|---|
| `config.py` | Reads YAML, validates with Pydantic, turns errors into one line per field (`products[2].site: unknown site 'x'`). Rejects secret-looking keys. Checks CSS selectors compile. | Reads secrets (they come from env vars) |
| `fetch.py` | One `Fetcher` per run. Checks robots.txt, waits between requests per site, retries, returns a `FetchResult`. | Raises on a bad page, bypasses blocks or CAPTCHAs |
| `parse.py` | Applies a site's selectors, normalises price text to `Decimal` + currency and stock text to a status + quantity. | Uses floats for money |
| `store.py` | SQLite: syncs `products` from config, appends `observations`, answers "previous", "previous ok", "previous with known stock" and history queries. | Updates or deletes an observation (triggers forbid it) |
| `changes.py` | Pure function from (product, current, previous observations) to a list of `Change`s with human-readable `describe()`. | Touches the network or database |
| `pipeline.py` | One run: sync products, interleave them across sites, fetch → parse → record each, then detect changes for the run. | Lets one product's exception stop the run |
| `cli.py` | `check-config`, `test-selectors`, `run [--dry-run]` (prints changes), `history`. | Holds business logic |

## Config (F1)

```mermaid
classDiagram
    class Config {
        fetch: Fetch
        sites: list~Site~, 1 to 10
        products: list~Product~, 1 to 200
        alerts: Alerts
        sheets: Sheets
        site(name) Site
        site_for_url(url) Site?
    }
    class Fetch {
        contact: email
        timeout_seconds: 20
        user_agent
    }
    class Site {
        name
        base_url
        fetch_mode: http | browser
        delay_seconds ≥ 5
        selectors: title, price, stock
        currency: ISO code
    }
    class Product {
        site
        url
        my_sku?
        my_price?: Decimal
    }
    Config --> Fetch
    Config --> Site
    Config --> Product
    Product ..> Site : site name, same host
```

Validation runs in two passes: field checks first (types, ranges, unknown keys), then
cross-field checks (product site exists, product host matches its site, no duplicate URLs or
site names). Cross-field errors only show once field errors are fixed.

## Fetching (F2, F3)

Each product page goes through the same gate. robots.txt is fetched once per site per run and
cached; the robots.txt request itself counts toward the site's delay.

```mermaid
flowchart TD
    start([fetch url, site]) --> robots{robots.txt<br/>cached?}
    robots -- no --> getrobots[GET /robots.txt<br/>with retries]
    getrobots --> rules{status}
    rules -- 2xx --> parsed[parse rules,<br/>read Crawl-delay]
    rules -- "404 / other 4xx" --> allowall[allow all]
    rules -- "401 / 403" --> denyall[deny all]
    rules -- "5xx / network error" --> unreachable[site unreachable]
    parsed & allowall & denyall --> cache[cache rules for this run]
    unreachable --> skipped
    cache --> allowed
    robots -- yes --> allowed{allowed?}
    allowed -- no --> skipped([skipped_robots])
    allowed -- yes --> wait["wait: max(site delay, Crawl-delay)<br/>+ backoff on retries"]
    wait --> get[GET page<br/>httpx or Playwright]
    get --> code{response}
    code -- 2xx --> ok([ok + html])
    code -- "429 / 5xx / timeout" --> retry{retries<br/>left?}
    retry -- yes --> wait
    retry -- no --> fail(["blocked (429) or http_error"])
    code -- "401 / 403" --> blocked([blocked])
    code -- "other 4xx" --> httperr([http_error])
```

**Timing rules**

- At least `delay_seconds` (≥ 5) between any two requests to the same site; `Crawl-delay`
  from robots.txt wins when it is larger. Different sites don't wait on each other.
- Up to 3 retries. Each retry waits the site delay **plus** 1 s, 2 s, 4 s backoff (+ jitter).
  `Retry-After` on a 429 is honoured, capped at 60 s.
- Sleep, clock and jitter are injected, so tests run instantly with a fake clock.

**Status values** match the `observations.status` column planned for session 3:
`ok`, `http_error`, `blocked`, `skipped_robots` (and `parse_error`, set by the parser).

**User-Agent:** `price-tracker/<version> (+mailto:<fetch.contact>)`; robots.txt rules are
matched against the `price-tracker` token.

## Parsing (F4)

```mermaid
flowchart LR
    html[HTML] --> soup[BeautifulSoup]
    soup --> sel["select_one() per selector<br/>→ raw text"]
    sel --> t[title]
    sel --> p["normalise_price()<br/>→ Decimal, currency?"]
    sel --> s["normalise_stock()<br/>→ status, qty?"]
    p --> cur{currency<br/>detected?}
    cur -- yes --> use[use it, warn on mismatch]
    cur -- "no ($, Rs, kr…)" --> site[use site currency]
```

- **Missing title or price** → `parse_error`, naming the selector that matched nothing. What
  did match is kept, which helps with debugging. **Missing stock** → `unknown`, not an error.
- **Prices:** if both `,` and `.` appear, the last one is the decimal separator. A single
  separator followed by exactly three digits is a thousands separator (`1,299` and `1.299`
  both become 1299). A space joins digit groups only before exactly three digits.
- **Picking the price when text has several numbers:**
  1. Drop numbers that aren't prices: percentages (`Save 15%`), multi-buy counts
     (`2 for $30`, `Buy 2`), ratings and review counts (`4.5 stars`, `128 reviews`),
     discount amounts (`Save $5`, `$5 off`) and stock counts (`3 left`).
  2. Prefer numbers next to a currency symbol or code over bare numbers.
  3. Of what's left, take the lowest (`From $9`, `$19.99 - $24.99`, `Was $24 Now $19`).
- **Stock:** pre-order/backorder → `unknown`; out-of-stock phrases → `out_of_stock`; a
  quantity (`22 available`, `Only 3 left`) → `in_stock` with qty (0 → `out_of_stock`); other
  in-stock phrases, including an "Add to cart" button → `in_stock`; anything else → `unknown`.

## Storage (F5)

Two tables, as in the spec. Prices are stored as **text** Decimals (SQLite has no decimal
type, and floats would turn 19.50 into 19.4999…). Triggers make `observations` append-only.

```mermaid
erDiagram
    products ||--o{ observations : "has"
    products {
        int id PK
        text site
        text url UK
        text my_sku "optional"
        text my_price "Decimal, optional"
        int active "0 when removed from config"
    }
    observations {
        int id PK
        int product_id FK
        text run_id "one per run"
        text fetched_at "ISO 8601 UTC"
        text status "ok | http_error | parse_error | blocked | skipped_robots"
        text title
        text price "Decimal"
        text currency "ISO code"
        text stock_status "in_stock | out_of_stock | unknown"
        int stock_qty
        text error
    }
```

- `database:` in config (default `tracker.db`) is resolved relative to the config file.
- Products are upserted by URL each run; ones removed from config get `active = 0` and keep
  their history.
- `tracker run --dry-run` copies the database into memory (`Store.scratch_copy`) and runs
  there, so it sees real history but writes nothing.

## Change detection (F6, F7)

Each observation in a run is compared with earlier ones for the same product:

```mermaid
flowchart TD
    cur([current observation]) --> okq{status ok?}
    okq -- no --> prevq{previous one<br/>was ok or none?}
    prevq -- yes --> nf([new_failure])
    prevq -- no --> none1([nothing: still failing,<br/>F12 handles repeats])
    okq -- yes --> first{any earlier<br/>ok observation?}
    first -- no --> none2([nothing: first sighting])
    first -- yes --> cmp[compare with last ok]
    cmp --> cur2{same currency?}
    cur2 -- no --> cc([currency_changed])
    cur2 -- yes --> pct{"|% change| ≥ threshold?"}
    pct -- yes --> pc(["price_down / price_up<br/>+ undercut flag if below my_price"])
    pct -- no --> cross{"crossed below<br/>my_price?"}
    cross -- yes --> uc([undercut])
    cmp --> stock["compare stock with last ok<br/>observation whose stock was known"]
    stock --> so(["out_of_stock / back_in_stock"])
    cmp --> title{title differs?}
    title -- yes --> tc([title_changed])
```

- **Threshold** (`alerts.min_change_percent`, default 1) is inclusive and uses the signed %
  change rounded to 0.1.
- **Undercut (F7):** `my_price` is assumed to be in the site's configured currency, so it is
  only compared when the competitor's price uses that currency. A move below `my_price` is
  reported **once**: as the price change's undercut flag, or as a separate `undercut` change
  when the move itself was under the threshold.
- **Last good observation:** failed runs in between don't hide a change; `24.00 → (failed) →
  19.50` is still a price drop. Stock skips `unknown` the same way.

## Mock competitor shop

`mock_site/` is a stdlib HTTP server for demos and end-to-end checks. Its state is a JSON file
re-read on every request, so changes show up on the tracker's next run without a restart.

```mermaid
sequenceDiagram
    actor You
    participant CLI as python -m mock_site
    participant State as state.json
    participant Shop as mock shop :8000
    participant Tracker as tracker run

    You->>CLI: set blue-mug --price 19.50
    CLI->>State: write
    You->>Tracker: tracker run -c mock_site/tracker-config.yaml
    Tracker->>Shop: GET /robots.txt
    Tracker->>Shop: GET /product/blue-mug (5 s apart)
    Shop->>State: read
    Shop-->>Tracker: HTML with $19.50
    Tracker-->>You: rival-x cut Blue Mug from 24.00 USD to 19.50 USD (-18.8%)
```

- Pages: `/`, `/product/<slug>`, `/robots.txt` (disallows `/admin`), and `/admin` with forms
  to change price and stock (handy for the demo video).
- `mock_site/tracker-config.yaml` points the tracker at it, with `my_price` set on each product.
- One product's price text includes a "2 for $20" promo, to show the price fix working.

## Testing

```mermaid
flowchart LR
    subgraph tests["pytest (no network)"]
        guard["conftest: real HTTP<br/>transport blocked"]
        mock["httpx.MockTransport<br/>+ fake clock"]
        fx["tests/fixtures/*.html"]
    end
    mock --> fetchT[test_fetch.py]
    fx --> parseT[test_parse.py]
    fx --> cliT[test_cli.py]
    mock --> cliT
    shop["mock shop served<br/>in-process"] --> pipeT[test_pipeline.py]
    shop --> cliT
    mem[("in-memory SQLite")] --> storeT[test_store.py]
    mem --> pipeT
    cfgT[test_config.py]
    chT[test_changes.py]
    real["real mock server<br/>on 127.0.0.1"] --> mockT[test_mock_site.py]
```

- An autouse fixture makes any real HTTP request through httpx fail, so no test can touch a
  live site. `test_mock_site.py` is the one place a real server runs, on localhost.
- `test_pipeline.py` is the end-to-end check: the mock shop served through
  `httpx.MockTransport`, through fetch, parse, store and change detection.
- Fixtures were saved once from the practice sites (see `tests/fixtures/README.md`), plus two
  synthetic variants for out-of-stock and a changed layout.
- CI (`.github/workflows/ci.yml`) runs ruff lint + format check, `check-config` on the
  example, and pytest with an 80% coverage gate.

## Decisions

| Decision | Why |
|---|---|
| BeautifulSoup over selectolax | Better known, easier for a client's developer to maintain; speed is irrelevant at ≤200 pages/day |
| robots.txt 401/403 → treat as disallow | Stricter than RFC 9309; a site hiding its robots.txt gets left alone |
| robots.txt 5xx / unreachable → skip the site | Don't assume permission we can't verify |
| Ambiguous symbols (`$`, `Rs`, `kr`) use the site currency | `$` alone could be USD, CAD, AUD…; the site config knows |
| Money as `Decimal`, floats from YAML go through `str` | `49.99` stays exactly `49.99` |
| Product host must match its site's `base_url` | Catches copy-paste mistakes in config before any request |
| Prices in SQLite as text | Exact Decimals; SQLite has no decimal type |
| Append-only observations enforced by triggers | "Never overwrite history" holds even for ad-hoc SQL |
| Compare with the last *successful* observation | A failed run in between doesn't hide or invent changes |
| One report per undercut event | The digest must list each real change exactly once |
| Zero price → `parse_error` | A parse glitch must not look like a +100% price rise |
| `run` prints changes in session 3 | `history` and the mock-site check need stored runs before alerts exist |
| Interleave products across sites | One site's delay overlaps requests to the others, so runs finish sooner |
