# Competitor Price and Stock Tracker

Checks a list of competitor product pages once a day, records price and stock,
and sends one digest when something changes.

> Work in progress: session 1 of 5. Only `tracker check-config` works so far.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp config.example.yaml config.yaml
cp .env.example .env        # secrets live here, never in config.yaml
```

## Usage

```sh
uv run tracker check-config                 # validate config.yaml
uv run tracker check-config --config other.yaml
uv run tracker --help
```

## Development

```sh
uv run pytest --cov=tracker
uv run ruff check .
uv run ruff format .
```
