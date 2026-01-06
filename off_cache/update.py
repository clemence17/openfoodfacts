from __future__ import annotations

import argparse

from .cache_db import upsert_products
from .off_client import fetch_recent_products


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Update local OpenFoodFacts cache (SQLite)")
    p.add_argument("--country", default="fr", help="country tag (e.g. fr)")
    p.add_argument("--recent-pages", type=int, default=3, help="number of pages to sync")
    p.add_argument("--page-size", type=int, default=200, help="products per page")
    p.add_argument("--sleep-s", type=float, default=0.0, help="sleep between requests")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="disable SSL certificate verification (use only if you understand the risks)",
    )
    p.add_argument(
        "--ca-bundle",
        default=None,
        help="path to a custom CA bundle (PEM) to trust (corporate proxy)",
    )
    args = p.parse_args(argv)

    verify = None
    if args.ca_bundle:
        verify = args.ca_bundle
    elif args.insecure:
        verify = False

    products = fetch_recent_products(
        country=args.country,
        pages=args.recent_pages,
        page_size=args.page_size,
        sleep_s=args.sleep_s,
        verify=verify,
    )
    rows = upsert_products(products)
    print(f"Upserted {rows} products")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
