# Competitor Price and Stock Tracker

Checks a list of competitor product pages once a day, records price and stock,
and sends one digest when something changes.

> Work in progress: session 2 of 5. `check-config` and `test-selectors` work; `run` and
> `history` arrive in sessions 3–4. See [ARCHITECTURE.md](ARCHITECTURE.md) for how it fits
> together.

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

The tracker is polite by design: it obeys robots.txt, waits at least 5 seconds between
requests to the same site (longer if robots.txt asks), and identifies itself with a
User-Agent that includes `fetch.contact`.

## Development

```sh
uv run pytest --cov=tracker
uv run ruff check .
uv run ruff format .
```

Tests never touch the network: real HTTP is blocked in `tests/conftest.py`, fetch tests use
`httpx.MockTransport`, and parsing tests use saved pages in `tests/fixtures/`.
