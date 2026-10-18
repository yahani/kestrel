# Competitor Price and Stock Tracker

Checks a list of competitor product pages once a day, records price and stock,
and sends one digest when something changes.

> Work in progress: session 3 of 5. Fetching, parsing, history and change detection work;
> `tracker run` prints the changes until email/Slack alerts arrive in session 4. See
> [ARCHITECTURE.md](ARCHITECTURE.md) for how it fits together and [PROGRESS.md](PROGRESS.md)
> for status.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp config.example.yaml config.yaml   # set fetch.contact to your own email
cp .env.example .env                 # secrets live here, never in config.yaml
```

Only for sites with `fetch_mode: browser` (JavaScript-rendered pages):

```sh
uv sync --extra browser
uv run playwright install chromium
```

## Usage

```sh
uv run tracker check-config                      # validate config.yaml
uv run tracker check-config --config other.yaml

# Fetch one page and show what each selector matched and how it was normalised
uv run tracker test-selectors https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html

# Same, but from a saved page (no network), or save the fetched page for later
uv run tracker test-selectors <url> --html saved.html
uv run tracker test-selectors <url> --save tests/fixtures/new_page.html
```

```sh
uv run tracker run                               # check every product, store, print changes
uv run tracker run --dry-run                     # same, but write nothing
uv run tracker history BOOK-001                  # by SKU, full URL, or part of the URL
uv run tracker history a-light-in-the-attic --limit 5
```

History is kept in SQLite (`database:` in the config, default `tracker.db` next to it).

The tracker is polite by design: it obeys robots.txt, waits at least 5 seconds between
requests to the same site (longer if robots.txt asks), and identifies itself with a
User-Agent that includes `fetch.contact`.

## Demo with the mock competitor shop

A tiny local shop whose prices and stock you change on demand:

```sh
# terminal 1
uv run python -m mock_site serve                 # http://127.0.0.1:8000, admin at /admin

# terminal 2
uv run tracker run -c mock_site/tracker-config.yaml          # first run: no changes
uv run python -m mock_site set blue-mug --price 19.50
uv run python -m mock_site set red-mug --stock 0
uv run tracker run -c mock_site/tracker-config.yaml          # reports both changes
uv run tracker history MUG-BLUE -c mock_site/tracker-config.yaml
uv run python -m mock_site reset                             # back to starting prices
```

## Development

```sh
uv run pytest --cov=tracker
uv run ruff check .
uv run ruff format .
```

Tests never touch the network: real HTTP is blocked in `tests/conftest.py`, fetch tests use
`httpx.MockTransport`, and parsing tests use saved pages in `tests/fixtures/`.
