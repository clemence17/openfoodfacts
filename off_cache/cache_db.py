from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from .settings import DB_PATH, SCHEMA_VERSION


def get_db_path() -> Path:
    return DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                code TEXT PRIMARY KEY,
                last_modified_t INTEGER,
                product_name TEXT,
                brands TEXT,
                categories TEXT,
                countries TEXT,
                nutriscore_grade TEXT,
                nutriments_json TEXT,
                raw_json TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )


def upsert_products(products: Iterable[Dict[str, Any]]) -> int:
    init_db()
    rows = 0

    with _connect() as conn:
        cur = conn.cursor()
        for p in products:
            code = str(p.get("code") or "").strip()
            if not code:
                continue

            nutriments = p.get("nutriments") or {}
            cur.execute(
                """
                INSERT INTO products(
                    code, last_modified_t, product_name, brands, categories, countries,
                    nutriscore_grade, nutriments_json, raw_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    last_modified_t=excluded.last_modified_t,
                    product_name=excluded.product_name,
                    brands=excluded.brands,
                    categories=excluded.categories,
                    countries=excluded.countries,
                    nutriscore_grade=excluded.nutriscore_grade,
                    nutriments_json=excluded.nutriments_json,
                    raw_json=excluded.raw_json
                """,
                (
                    code,
                    _safe_int(p.get("last_modified_t")),
                    _safe_text(p.get("product_name")),
                    _safe_text(p.get("brands")),
                    _safe_text(p.get("categories")),
                    _safe_text(p.get("countries")),
                    _safe_text(p.get("nutriscore_grade")),
                    json.dumps(nutriments, ensure_ascii=False),
                    json.dumps(p, ensure_ascii=False),
                ),
            )
            rows += 1

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_sync_utc', datetime('now'))"
        )
        conn.commit()

    return rows


def read_meta() -> Dict[str, str]:
    init_db()
    with _connect() as conn:
        cur = conn.execute("SELECT key, value FROM meta")
        return {k: v for (k, v) in cur.fetchall()}


def read_products_dataframe(limit: int = 200_000) -> pd.DataFrame:
    init_db()
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                code,
                last_modified_t,
                product_name,
                brands,
                categories,
                countries,
                nutriscore_grade,
                nutriments_json
            FROM products
            ORDER BY last_modified_t DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )

    if df.empty:
        return df

    # Expand a few nutriments into columns (best-effort)
    def nutr_value(row: str, key: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            v = obj.get(key)
            return float(v) if v is not None else None
        except Exception:
            return None

    df["sugars_100g"] = df["nutriments_json"].apply(lambda s: nutr_value(s, "sugars_100g"))
    df["salt_100g"] = df["nutriments_json"].apply(lambda s: nutr_value(s, "salt_100g"))
    df["energy-kcal_100g"] = df["nutriments_json"].apply(lambda s: nutr_value(s, "energy-kcal_100g"))

    return df.drop(columns=["nutriments_json"], errors="ignore")


def _safe_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None
