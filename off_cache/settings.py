from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("OFF_CACHE_DB", str(DATA_DIR / "off_cache.sqlite")))

# OpenFoodFacts base
OFF_BASE_URL = os.environ.get("OFF_BASE_URL", "https://world.openfoodfacts.org")

# SSL handling (useful behind corporate proxies):
# - OFF_SSL_VERIFY=0 to disable verification (not recommended, but pragmatic)
# - OFF_CA_BUNDLE=C:\path\to\corp-ca.pem to trust a custom CA
OFF_SSL_VERIFY = os.environ.get("OFF_SSL_VERIFY", "1") not in ("0", "false", "False")
OFF_CA_BUNDLE = os.environ.get("OFF_CA_BUNDLE")

# User-Agent: recommandé pour OFF
USER_AGENT = os.environ.get(
    "OFF_USER_AGENT",
    "off-cache-streamlit/0.1 (contact: reuse@openfoodfacts.org)",
)

SCHEMA_VERSION = "1"
