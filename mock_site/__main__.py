"""Run or change the mock competitor shop.

uv run python -m mock_site serve              # http://127.0.0.1:8000
uv run python -m mock_site list
uv run python -m mock_site set blue-mug --price 19.50
uv run python -m mock_site set red-mug --stock 0
uv run python -m mock_site reset
"""

from __future__ import annotations

import argparse
import sys

from mock_site import shop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mock_site", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the shop")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    commands.add_parser("list", help="Show current prices and stock")

    change = commands.add_parser("set", help="Change a product's price and/or stock")
    change.add_argument("slug")
    change.add_argument("--price")
    change.add_argument("--stock", type=int)

    commands.add_parser("reset", help="Restore starting prices and stock")

    args = parser.parse_args(argv)

    if args.command == "serve":
        server = shop.make_server(args.host, args.port)
        print(f"{shop.SHOP_NAME} on http://{args.host}:{args.port}  (admin: /admin, Ctrl+C stops)")
        print(f"state file: {shop.state_path()}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    try:
        if args.command == "set":
            if args.price is None and args.stock is None:
                parser.error("set needs --price and/or --stock")
            item = shop.update(args.slug, args.price, args.stock)
            print(f"{item.slug}: ${item.price}, {shop.stock_text(item)}")
            return 0
        items = shop.reset() if args.command == "reset" else shop.load()
    except (KeyError, ValueError, ArithmeticError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for item in items.values():
        print(f"{item.slug:<14} ${item.price:>7}  {shop.stock_text(item)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
