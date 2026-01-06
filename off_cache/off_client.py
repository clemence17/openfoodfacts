from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import requests

from .settings import OFF_BASE_URL, OFF_CA_BUNDLE, OFF_SSL_VERIFY, USER_AGENT


@dataclass(frozen=True)
class SearchParams:
    country: str = "fr"
    page_size: int = 200
    fields: str = (
        "code,product_name,brands,categories,countries,nutriscore_grade,"
        "nutriments,last_modified_t,created_t,quantity"
    )


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    # Configure SSL verification for environments with custom trust stores.
    if OFF_CA_BUNDLE:
        s.verify = OFF_CA_BUNDLE
    else:
        s.verify = OFF_SSL_VERIFY
    return s


def fetch_recent_products(
    *,
    country: str = "fr",
    pages: int = 3,
    page_size: int = 200,
    timeout_s: int = 30,
    sleep_s: float = 0.0,
    verify: bool | str | None = None,
) -> Iterable[Dict[str, Any]]:
    """Fetch 'recent' products using the legacy search endpoint.

    This avoids full dumps. It's a pragmatic incremental approach:
    we request the most recently modified products, page by page.

    Notes:
    - OFF has multiple APIs; this keeps dependencies low.
    - If OFF changes query parameters, adjust here.
    """

    sess = _session()
    if verify is not None:
        sess.verify = verify

    for page in range(1, pages + 1):
        url = f"{OFF_BASE_URL}/cgi/search.pl"
        params = {
            "action": "process",
            "json": 1,
            "page": page,
            "page_size": page_size,
            "sort_by": "last_modified_t",
            "fields": SearchParams().fields,
            # filter by country tag (best-effort)
            "countries_tags_en": country,
        }

        r = sess.get(url, params=params, timeout=timeout_s)
        if r.status_code in (400, 404, 422):
            # Some OFF instances / query versions may not accept the filter param.
            params.pop("countries_tags_en", None)
            r = sess.get(url, params=params, timeout=timeout_s)
        r.raise_for_status()
        payload = r.json()

        products = payload.get("products") or []
        for product in products:
            if isinstance(product, dict) and product.get("code"):
                yield product

        if sleep_s:
            time.sleep(sleep_s)
