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
                ecoscore_grade TEXT,
                nova_group INTEGER,
                ecoscore_data_json TEXT,
                nutriments_json TEXT,
                raw_json TEXT
            );
            """
        )

        # Lightweight migrations for older DBs
        _ensure_column(conn, "products", "ecoscore_grade", "TEXT")
        _ensure_column(conn, "products", "nova_group", "INTEGER")
        _ensure_column(conn, "products", "ecoscore_data_json", "TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )

        # Meal tracking
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at_utc TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_items (
                meal_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                FOREIGN KEY(meal_id) REFERENCES meals(id)
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
            ecoscore_data = p.get("ecoscore_data") or {}
            cur.execute(
                """
                INSERT INTO products(
                    code, last_modified_t, product_name, brands, categories, countries,
                    nutriscore_grade, ecoscore_grade, nova_group, ecoscore_data_json,
                    nutriments_json, raw_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    last_modified_t=excluded.last_modified_t,
                    product_name=excluded.product_name,
                    brands=excluded.brands,
                    categories=excluded.categories,
                    countries=excluded.countries,
                    nutriscore_grade=excluded.nutriscore_grade,
                    ecoscore_grade=excluded.ecoscore_grade,
                    nova_group=excluded.nova_group,
                    ecoscore_data_json=excluded.ecoscore_data_json,
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
                    _safe_text(p.get("ecoscore_grade")),
                    _safe_int(p.get("nova_group")),
                    json.dumps(ecoscore_data, ensure_ascii=False),
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
                ecoscore_grade,
                nova_group,
                ecoscore_data_json,
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

    # Carbon footprint (best-effort): either a nutriment or from ecoscore_data.agribalyse.co2_total
    def carbon_from_ecoscore(row: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            co2_total = (
                obj.get("agribalyse", {}).get("co2_total")
                if isinstance(obj, dict)
                else None
            )
            if co2_total is None:
                return None
            # agribalyse co2_total is typically kg CO2e per kg product -> convert to g CO2e per 100g
            return float(co2_total) * 100.0
        except Exception:
            return None

    carbon_nutr = df["nutriments_json"].apply(lambda s: nutr_value(s, "carbon-footprint_100g"))
    carbon_eco = df["ecoscore_data_json"].apply(carbon_from_ecoscore)
    df["carbon_footprint_gco2e_100g"] = carbon_nutr
    df.loc[df["carbon_footprint_gco2e_100g"].isna(), "carbon_footprint_gco2e_100g"] = carbon_eco

    return df.drop(columns=["nutriments_json", "ecoscore_data_json"], errors="ignore")


def get_product_row(code: str) -> Optional[Dict[str, Any]]:
    code = str(code).strip()
    if not code:
        return None
    df = read_products_dataframe(limit=200_000)
    hit = df[df["code"] == code]
    if hit.empty:
        return None
    return hit.iloc[0].to_dict()


def search_products_by_name(query: str, limit: int = 25) -> pd.DataFrame:
    init_db()
    q = (query or "").strip()
    if not q:
        return pd.DataFrame()
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT code, product_name, brands, nutriscore_grade, ecoscore_grade, nova_group, raw_json
            FROM products
            WHERE product_name LIKE ?
            ORDER BY last_modified_t DESC
            LIMIT ?
            """,
            conn,
            params=(f"%{q}%", limit),
        )
    return df


def get_products_by_codes(codes: list[str]) -> pd.DataFrame:
    """Return basic product info for a list of barcodes.

    Keeps UI fast by querying only the requested codes.
    """
    init_db()
    cleaned = [str(c).strip() for c in codes if str(c).strip()]
    if not cleaned:
        return pd.DataFrame(columns=["code", "product_name", "brands", "raw_json"])

    placeholders = ",".join(["?"] * len(cleaned))
    sql = f"""
        SELECT code, product_name, brands, raw_json
        FROM products
        WHERE code IN ({placeholders})
    """

    with _connect() as conn:
        df = pd.read_sql_query(sql, conn, params=tuple(cleaned))

    if df.empty:
        return df

    # Keep the original ordering of `codes`.
    order = {code: i for i, code in enumerate(cleaned)}
    df["_order"] = df["code"].map(order)
    df = df.sort_values(by="_order", kind="stable").drop(columns=["_order"])
    return df


def add_meal(consumed_codes: list[str]) -> int:
    """Persist a meal (list of product codes) and return meal_id."""
    init_db()
    codes = [str(c).strip() for c in consumed_codes if str(c).strip()]
    if not codes:
        raise ValueError("No product codes")

    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO meals DEFAULT VALUES")
        meal_id = int(cur.lastrowid)
        cur.executemany(
            "INSERT INTO meal_items(meal_id, code) VALUES(?, ?)",
            [(meal_id, c) for c in codes],
        )
        conn.commit()
    return meal_id


def delete_meals_today() -> int:
    """Delete meals created today (UTC) and their items.

    Returns the number of meals deleted.
    """
    init_db()
    with _connect() as conn:
        cur = conn.execute("SELECT id FROM meals WHERE date(created_at_utc) = date('now')")
        meal_ids = [int(r[0]) for r in cur.fetchall()]
        if not meal_ids:
            return 0

        placeholders = ",".join(["?"] * len(meal_ids))
        conn.execute(f"DELETE FROM meal_items WHERE meal_id IN ({placeholders})", meal_ids)
        conn.execute(f"DELETE FROM meals WHERE id IN ({placeholders})", meal_ids)
        conn.commit()
        return len(meal_ids)


def delete_all_meals() -> int:
    """Delete all meals and their items.

    Returns the number of meals deleted.
    """
    init_db()
    with _connect() as conn:
        cur = conn.execute("SELECT COUNT(1) FROM meals")
        count = int(cur.fetchone()[0] or 0)
        conn.execute("DELETE FROM meal_items")
        conn.execute("DELETE FROM meals")
        conn.commit()
        return count


def delete_code_from_all_meals(code: str) -> int:
    """Delete a product code from all meals.

    Returns the number of meal_items deleted.
    """
    init_db()
    c = str(code or "").strip()
    if not c:
        return 0

    with _connect() as conn:
        cur = conn.execute("DELETE FROM meal_items WHERE code = ?", (c,))
        deleted_items = int(cur.rowcount or 0)
        # Cleanup: remove meals without any remaining items
        conn.execute(
            "DELETE FROM meals WHERE id NOT IN (SELECT DISTINCT meal_id FROM meal_items)"
        )
        conn.commit()

    return deleted_items


def read_consumed_items_today() -> pd.DataFrame:
    """Returns products consumed today (UTC) with meal_id and created_at."""
    init_db()
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT mi.meal_id, m.created_at_utc, p.code, p.product_name, p.brands,
                   p.nutriscore_grade, p.ecoscore_grade, p.nova_group,
                   p.categories, p.countries,
                   p.ecoscore_data_json, p.nutriments_json, p.raw_json
            FROM meal_items mi
            JOIN meals m ON m.id = mi.meal_id
            JOIN products p ON p.code = mi.code
            WHERE date(m.created_at_utc) = date('now')
            ORDER BY m.created_at_utc DESC
            """,
            conn,
        )

    if df.empty:
        return df

    # Reuse the same carbon extraction
    def nutr_value(row: str, key: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            v = obj.get(key)
            return float(v) if v is not None else None
        except Exception:
            return None

    def carbon_from_ecoscore(row: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            co2_total = obj.get("agribalyse", {}).get("co2_total") if isinstance(obj, dict) else None
            if co2_total is None:
                return None
            return float(co2_total) * 100.0
        except Exception:
            return None

    carbon_nutr = df["nutriments_json"].apply(lambda s: nutr_value(s, "carbon-footprint_100g"))
    carbon_eco = df["ecoscore_data_json"].apply(carbon_from_ecoscore)
    df["carbon_footprint_gco2e_100g"] = carbon_nutr
    df.loc[df["carbon_footprint_gco2e_100g"].isna(), "carbon_footprint_gco2e_100g"] = carbon_eco

    # Keep nutriments_json and raw_json for downstream metrics (diversity, additives, origin).
    return df.drop(columns=["ecoscore_data_json"], errors="ignore")


def read_consumed_items_since(days: int = 7) -> pd.DataFrame:
    """Returns products consumed since N days ago (UTC)."""
    init_db()
    days = int(days)
    if days < 1:
        raise ValueError("days must be >= 1")

    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT mi.meal_id, m.created_at_utc, p.code, p.product_name, p.brands,
                   p.nutriscore_grade, p.ecoscore_grade, p.nova_group,
                   p.categories, p.countries,
                   p.ecoscore_data_json, p.nutriments_json, p.raw_json
            FROM meal_items mi
            JOIN meals m ON m.id = mi.meal_id
            JOIN products p ON p.code = mi.code
            WHERE date(m.created_at_utc) >= date('now', ?)
            ORDER BY m.created_at_utc DESC
            """,
            conn,
            params=(f"-{days} day",),
        )

    if df.empty:
        return df

    def nutr_value(row: str, key: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            v = obj.get(key)
            return float(v) if v is not None else None
        except Exception:
            return None

    def carbon_from_ecoscore(row: str) -> Optional[float]:
        try:
            obj = json.loads(row) if row else {}
            co2_total = obj.get("agribalyse", {}).get("co2_total") if isinstance(obj, dict) else None
            if co2_total is None:
                return None
            return float(co2_total) * 100.0
        except Exception:
            return None

    carbon_nutr = df["nutriments_json"].apply(lambda s: nutr_value(s, "carbon-footprint_100g"))
    carbon_eco = df["ecoscore_data_json"].apply(carbon_from_ecoscore)
    df["carbon_footprint_gco2e_100g"] = carbon_nutr
    df.loc[df["carbon_footprint_gco2e_100g"].isna(), "carbon_footprint_gco2e_100g"] = carbon_eco

    return df.drop(columns=["ecoscore_data_json"], errors="ignore")


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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}  # name is 2nd column
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
