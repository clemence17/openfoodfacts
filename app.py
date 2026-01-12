from __future__ import annotations

import json
from pathlib import Path
import re
from textwrap import dedent

import pandas as pd
import pydeck as pdk
import plotly.express as px
import requests
import streamlit as st

from off_cache.cache_db import (
    add_meal,
    delete_all_meals,
    delete_code_from_all_meals,
    delete_meals_today,
    get_products_by_codes,
    read_consumed_items_today,
    read_consumed_items_since,
    search_products_by_name,
    upsert_products,
)
from off_cache.off_client import fetch_product_by_code
from off_cache.off_client import search_products_by_name_online
from off_cache.cache_db import read_products_dataframe
from off_cache.settings import OFF_CA_BUNDLE, OFF_SSL_VERIFY, USER_AGENT


st.set_page_config(page_title="FoodTrack", layout="wide")


def _html_block(s: str) -> str:
    return "\n".join(line.lstrip() for line in dedent(s).splitlines() if line.strip())


@st.cache_data(show_spinner=False)
def _load_products_for_reporting(limit: int = 200_000) -> pd.DataFrame:
    return read_products_dataframe(limit=limit)


def _top_categories(df: pd.DataFrame, *, top_n: int = 60) -> list[str]:
    if df.empty or "categories" not in df.columns:
        return []

    cats = df["categories"].fillna("").astype(str)
    exploded = (
        cats[cats.str.len() > 0]
        .str.split(",")
        .explode()
        .astype(str)
        .str.strip()
    )
    exploded = exploded[exploded.str.len() > 0]
    if exploded.empty:
        return []

    # Keep the dropdown usable: show the most common categories.
    return exploded.value_counts().head(top_n).index.tolist()


def _filter_by_category(df: pd.DataFrame, category: str | None) -> pd.DataFrame:
    if df.empty:
        return df
    if not category or category == "Toutes catégories" or "categories" not in df.columns:
        return df
    return df[df["categories"].fillna("").astype(str).str.contains(re.escape(category), case=False, na=False)]


def _normalize_country_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    # OFF often uses tags like "en:france".
    s = re.sub(r"^[a-z]{2}:", "", s.strip(), flags=re.IGNORECASE)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()

    key = s.casefold()
    # Best-effort mapping: keep this small and pragmatic.
    mapping = {
        # English
        "fr": "France",
        "france": "France",
        "germany": "Germany",
        "united kingdom": "United Kingdom",
        "uk": "United Kingdom",
        "usa": "United States",
        "united states": "United States",
        "czech republic": "Czechia",
        "bosnia herzegovina": "Bosnia and Herzegovina",
        "north macedonia": "North Macedonia",
        "switzerland": "Switzerland",
        "romania": "Romania",
        "italy": "Italy",
        "australia": "Australia",
        "belgium": "Belgium",
        # French
        "allemagne": "Germany",
        "royaume uni": "United Kingdom",
        "etats unis": "United States",
        "tchequie": "Czechia",
        "bosnie herzegovine": "Bosnia and Herzegovina",
        "macedoine du nord": "North Macedonia",
        "suisse": "Switzerland",
        "roumanie": "Romania",
        "italie": "Italy",
        "australie": "Australia",
        "belgique": "Belgium",
        # German
        "frankreich": "France",
        "deutschland": "Germany",
    }
    if key in mapping:
        return mapping[key]

    # Title-case fallback (works for many Plotly country names).
    return " ".join([w[:1].upper() + w[1:] for w in key.split()])


def _countries_counts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "countries" not in df.columns:
        return pd.DataFrame(columns=["country", "count"])
    countries = df["countries"].fillna("").astype(str)
    exploded = (
        countries[countries.str.len() > 0]
        .str.split(",")
        .explode()
        .astype(str)
        .str.strip()
    )
    exploded = exploded[exploded.str.len() > 0]
    if exploded.empty:
        return pd.DataFrame(columns=["country", "count"])

    normalized = exploded.apply(_normalize_country_name)
    normalized = normalized[normalized.str.len() > 0]
    if normalized.empty:
        return pd.DataFrame(columns=["country", "count"])

    out = normalized.value_counts().reset_index()
    out.columns = ["country", "count"]
    return out


def _render_reporting_tab() -> None:
    st.header("Reporting")
    st.caption("Dashboard filtrable par catégorie (cache local SQLite).")

    df_all = _load_products_for_reporting()
    if df_all.empty:
        st.info("Cache vide: lance une mise à jour avant d'utiliser le reporting.")
        return

    categories = _top_categories(df_all, top_n=60)
    category = st.selectbox(
        "Catégorie de produit",
        options=["Toutes catégories", *categories] if categories else ["Toutes catégories"],
        index=0,
    )

    df = _filter_by_category(df_all, category)
    if df.empty:
        st.warning("Aucun produit pour cette catégorie dans le cache.")
        return

    # KPIs
    col1, col2, col3, col4 = st.columns(4)
    nutri = df.get("nutriscore_grade", pd.Series(dtype=str)).fillna("").astype(str).str.upper().str.strip()
    sugars = pd.to_numeric(df.get("sugars_100g"), errors="coerce")
    salt = pd.to_numeric(df.get("salt_100g"), errors="coerce")
    energy = pd.to_numeric(df.get("energy-kcal_100g"), errors="coerce")

    with col1:
        st.metric("Produits", f"{len(df):,}".replace(",", " "))
    with col2:
        pct_a = (nutri.eq("A").mean() * 100.0) if len(nutri) else 0.0
        st.metric("Nutri-Score A", f"{pct_a:.1f}%")
    with col3:
        st.metric("Sucre médian", "—" if not sugars.notna().any() else f"{sugars.median():.1f} g/100g")
    with col4:
        st.metric("Sel médian", "—" if not salt.notna().any() else f"{salt.median():.2f} g/100g")

    # Charts
    c1, c2 = st.columns(2)
    with c1:
        counts = nutri.replace({"": "UNKNOWN"}).value_counts().sort_index()
        nutri_df = counts.reset_index()
        x_col = nutri_df.columns[0]
        fig = px.bar(
            nutri_df,
            x=x_col,
            y="count",
            labels={x_col: "Nutri-Score", "count": "Produits"},
            title="Répartition Nutri-Score",
        )
        st.plotly_chart(fig, width="stretch")

    with c2:
        if sugars.notna().any():
            fig = px.histogram(
                df,
                x="sugars_100g",
                nbins=40,
                title="Distribution du sucre (g/100g)",
                labels={"sugars_100g": "Sucre (g/100g)"},
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Pas assez de données sucre pour tracer la distribution.")

    c3, c4 = st.columns(2)
    with c3:
        brands = df.get("brands", pd.Series(dtype=str)).fillna("")
        top_brands = (
            brands[brands.astype(str).str.len() > 0]
            .astype(str)
            .str.split(",")
            .explode()
            .astype(str)
            .str.strip()
            .value_counts()
            .head(15)
        )
        if not top_brands.empty:
            bd = top_brands.reset_index()
            x_col = bd.columns[0]
            fig = px.bar(
                bd,
                x=x_col,
                y="count",
                title="Top marques (15)",
                labels={x_col: "Marque", "count": "Produits"},
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Pas assez de données de marques.")

    with c4:
        if energy.notna().any():
            fig = px.histogram(
                df,
                x="energy-kcal_100g",
                nbins=40,
                title="Distribution énergie (kcal/100g)",
                labels={"energy-kcal_100g": "Énergie (kcal/100g)"},
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Pas assez de données énergie.")

    # Optional: NOVA distribution if present
    if "nova_group" in df.columns and pd.to_numeric(df["nova_group"], errors="coerce").notna().any():
        nova = pd.to_numeric(df["nova_group"], errors="coerce")
        nova_counts = nova.dropna().astype(int).value_counts().sort_index()
        nd = nova_counts.reset_index()
        x_col = nd.columns[0]
        fig = px.bar(
            nd,
            x=x_col,
            y="count",
            title="Répartition NOVA",
            labels={x_col: "NOVA", "count": "Produits"},
        )
        st.plotly_chart(fig, width="stretch")

    st.markdown(
        _html_block(
            """
<div class="section-head" style="margin-top: 18px;">
  <h2>Répartition géographique</h2>
  <p>Vue globale basée sur le champ <b>countries</b> (pays de vente/déclaration OFF) — meilleur effort.</p>
</div>
"""
        ),
        unsafe_allow_html=True,
    )
    cc = _countries_counts(df)
    if cc.empty:
        st.info("Pas assez de données 'countries' pour afficher la carte.")
    else:
        total = int(cc["count"].sum()) if "count" in cc.columns else int(len(df))
        countries = int(len(cc))
        top_country = str(cc.iloc[0]["country"]) if countries else "—"
        top_share = (100.0 * float(cc.iloc[0]["count"]) / float(total)) if countries and total else 0.0

        st.markdown(
            _html_block(
                f"""
<div class="origin-map-metrics" style="margin-top: 6px;">
  <div class="origin-metric"><div class="big">{countries}</div><div class="small">Pays</div></div>
  <div class="origin-metric"><div class="big">{total}</div><div class="small">Produits</div></div>
  <div class="origin-metric"><div class="big">{top_share:.0f}%</div><div class="small">Top: {top_country}</div></div>
</div>
"""
            ),
            unsafe_allow_html=True,
        )

        fig = px.choropleth(
            cc,
            locations="country",
            locationmode="country names",
            color="count",
            hover_name="country",
        )
        fig.update_geos(
            projection_type="natural earth",
            showframe=False,
            showcoastlines=False,
            showcountries=False,
            bgcolor="rgba(0,0,0,0)",
        )
        fig.update_layout(
            height=460,
            margin=dict(l=0, r=0, t=10, b=0),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            coloraxis_colorbar=dict(title="", thickness=12, len=0.55),
        )
        st.plotly_chart(fig, width="stretch")


# Top-level navigation
tab_home, tab_reporting = st.tabs(["Accueil", "Reporting"])

with tab_reporting:
    _render_reporting_tab()

st.markdown(
        dedent(
                """
<style>
    :root {
        --app-radius: 18px;
        --accent: #2ECC71;
        --ink: rgba(15, 23, 42, 0.92);
        --muted: rgba(15, 23, 42, 0.70);
    }
    /* Rounded, friendly typography (no external font download) */
    html, body, [data-testid="stAppViewContainer"] {
        font-family: ui-rounded, "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
    }
    /* Center content like a product landing page */
    .main .block-container { max-width: 1100px; padding-top: 0.6rem; }
    h1 { line-height: 1.10; margin-bottom: 0.25rem; }
    h2 { margin-top: 1.25rem; }
    /* Remove extra top padding introduced by default header */
    [data-testid="stAppViewContainer"] > .main { padding-top: 0rem; }

    /* Card-like containers */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: var(--app-radius);
        border-color: rgba(31, 41, 55, 0.08);
        background: var(--secondary-background-color);
    }

    /* KPI tiles */
    .kpi {
        padding: 16px 16px;
        border-radius: var(--app-radius);
        background: var(--secondary-background-color);
        border: 1px solid rgba(31, 41, 55, 0.08);
    }
    .kpi-label { font-size: 0.9rem; opacity: 0.75; margin-bottom: 4px; }
    .kpi-value { font-size: 1.8rem; font-weight: 700; line-height: 1.1; }
    .kpi-sub { font-size: 0.85rem; opacity: 0.65; margin-top: 6px; }

    /* Key indicators (card layout like the reference) */
    .key-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 22px;
        margin-top: 10px;
        margin-bottom: 18px;
    }
    .key-card {
        border-radius: 26px;
        padding: 24px;
        background: rgba(255, 255, 255, 0.78);
        border: 1px solid rgba(31, 41, 55, 0.08);
        box-shadow: 0 14px 30px rgba(15, 23, 42, 0.06);
        min-height: 210px;
    }
    .key-card.bg-green {
        background: linear-gradient(130deg, rgba(46, 204, 113, 0.12), rgba(255, 255, 255, 0.78));
    }
    .key-card.bg-blue {
        background: linear-gradient(130deg, rgba(221, 241, 255, 0.92), rgba(255, 255, 255, 0.78));
    }
    .key-card.bg-amber {
        background: linear-gradient(130deg, rgba(255, 245, 214, 0.90), rgba(255, 255, 255, 0.78));
    }
    .key-card.bg-pink {
        background: linear-gradient(130deg, rgba(255, 90, 95, 0.10), rgba(255, 255, 255, 0.78));
    }
    .key-top {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 16px;
    }
    .key-ico {
        width: 56px;
        height: 56px;
        border-radius: 18px;
        display: grid;
        place-items: center;
        background: rgba(255, 255, 255, 0.70);
        border: 1px solid rgba(31, 41, 55, 0.08);
        font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", ui-rounded, "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
        font-size: 2.05rem;
        line-height: 1;
        filter: saturate(1.25) contrast(1.15);
        text-shadow: 0 1px 0 rgba(15, 23, 42, 0.10);
    }
    .key-title {
        font-size: 1.02rem;
        color: rgba(15, 23, 42, 0.72);
        margin: 0;
        font-weight: 600;
    }
    .key-big {
        margin: 0;
        font-size: 2.8rem;
        letter-spacing: -0.03em;
        font-weight: 800;
        color: rgba(15, 23, 42, 0.92);
        line-height: 1.0;
    }
    .key-range {
        margin-left: 8px;
        font-size: 1.05rem;
        color: rgba(15, 23, 42, 0.68);
        font-weight: 600;
    }
    .key-desc {
        margin: 14px 0 0 0;
        color: rgba(15, 23, 42, 0.68);
        font-size: 0.98rem;
        line-height: 1.45;
    }
    .mini-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 22px;
        margin-top: 8px;
        margin-bottom: 6px;
    }
    .mini-card {
        border-radius: 26px;
        padding: 18px 22px;
        background: rgba(255, 255, 255, 0.78);
        border: 1px solid rgba(31, 41, 55, 0.08);
        box-shadow: 0 14px 30px rgba(15, 23, 42, 0.06);
        min-height: 96px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        text-align: center;
        gap: 6px;
    }
    .mini-card a { color: inherit; text-decoration: none; }
    .mini-ico {
        font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", ui-rounded, "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
        font-size: 1.75rem;
        line-height: 1;
        margin-bottom: 2px;
        filter: saturate(1.25) contrast(1.15);
        text-shadow: 0 1px 0 rgba(15, 23, 42, 0.10);
    }
    .mini-title { font-weight: 800; color: rgba(15, 23, 42, 0.90); }
    .mini-sub { color: rgba(15, 23, 42, 0.65); font-size: 0.95rem; }

    @media (max-width: 1100px) {
        .key-grid { grid-template-columns: 1fr 1fr; }
        .mini-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
        .key-grid { grid-template-columns: 1fr; }
    }

        /* Top navigation */
        .topnav {
                position: sticky;
                top: 0;
                z-index: 50;
                margin: 0 0 14px 0;
        }
        .topnav .nav-inner {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 10px 14px;
                border-radius: 18px;
                border: 1px solid rgba(31, 41, 55, 0.08);
                background: rgba(255, 255, 255, 0.70);
                backdrop-filter: blur(10px);
        }
        .brand {
                display: inline-flex;
                align-items: center;
                gap: 10px;
                font-weight: 800;
                color: var(--ink);
                letter-spacing: -0.01em;
        }
        .brand .mark {
                width: 30px;
                height: 30px;
                border-radius: 999px;
                background: rgba(46, 204, 113, 0.18);
                display: grid;
                place-items: center;
                border: 1px solid rgba(31, 41, 55, 0.08);
        }
        .nav-links {
                display: flex;
                gap: 18px;
                align-items: center;
                color: rgba(15, 23, 42, 0.70);
                font-size: 0.95rem;
        }
        .nav-links a {
                text-decoration: none;
                color: rgba(15, 23, 42, 0.70);
        }
        .nav-links a:hover { color: rgba(15, 23, 42, 0.92); }
        .nav-cta {
                text-decoration: none;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                padding: 10px 16px;
                border-radius: 999px;
                background: rgba(46, 204, 113, 0.95);
                color: white;
                font-weight: 700;
                border: 1px solid rgba(31, 41, 55, 0.08);
        }

        /* Hero (landing) */
        .hero2 {
                position: relative;
                overflow: hidden;
                border-radius: 26px;
                border: 1px solid rgba(31, 41, 55, 0.08);
                background:
                        radial-gradient(900px 500px at 10% 25%, rgba(46, 204, 113, 0.18), rgba(234, 247, 255, 0.0) 60%),
                        radial-gradient(800px 450px at 90% 70%, rgba(221, 241, 255, 0.95), rgba(238, 248, 255, 0.0) 60%),
                        linear-gradient(120deg, rgba(238, 248, 255, 1.0), rgba(234, 247, 255, 1.0));
                padding: 34px 26px;
                margin-bottom: 26px;
        }
        .hero2-inner {
                display: grid;
                grid-template-columns: 1.1fr 0.9fr;
                gap: 22px;
                align-items: center;
        }
        .pill {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                padding: 8px 12px;
                border-radius: 999px;
                background: rgba(46, 204, 113, 0.12);
                border: 1px solid rgba(31, 41, 55, 0.06);
                color: rgba(46, 204, 113, 0.95);
                font-weight: 700;
                font-size: 0.95rem;
                width: fit-content;
        }
        .hero2 h1 {
                margin: 14px 0 10px 0;
                font-size: 3.2rem;
                letter-spacing: -0.03em;
                color: var(--ink);
        }
        .hero2 h1 .accent {
                color: rgba(46, 204, 113, 0.98);
        }
        .hero2 p {
                margin: 0;
                max-width: 55ch;
                font-size: 1.05rem;
                color: var(--muted);
                line-height: 1.55;
        }
        .hero2-actions {
                margin-top: 18px;
                display: flex;
                gap: 12px;
                flex-wrap: wrap;
        }
        .btn {
                text-decoration: none;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 12px 18px;
                border-radius: 999px;
                border: 1px solid rgba(31, 41, 55, 0.08);
                font-weight: 700;
        }
        .btn.primary {
                background: rgba(46, 204, 113, 0.95);
                color: #ffffff;
        }
        .btn.ghost {
                background: rgba(255, 255, 255, 0.60);
                color: rgba(15, 23, 42, 0.86);
                backdrop-filter: blur(10px);
        }
        .hero2-stats {
                margin-top: 18px;
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 14px;
                max-width: 520px;
        }
        .stat {
                padding: 10px 12px;
                border-radius: 16px;
                background: rgba(255, 255, 255, 0.60);
                border: 1px solid rgba(31, 41, 55, 0.06);
                backdrop-filter: blur(10px);
        }
        .stat b { display: block; font-size: 1.4rem; color: var(--ink); }
        .stat span { color: rgba(15, 23, 42, 0.65); font-size: 0.9rem; }

        /* Phone mock + floating chips */
        .hero2-right { position: relative; min-height: 340px; }
        .phone {
                position: absolute;
                right: 14px;
                top: 16px;
                width: 320px;
                max-width: 100%;
                filter: drop-shadow(0 18px 30px rgba(15, 23, 42, 0.12));
        }
        .chip {
                position: absolute;
            width: 66px;
            height: 66px;
                border-radius: 16px;
                background: rgba(255, 255, 255, 0.70);
                border: 1px solid rgba(31, 41, 55, 0.06);
                backdrop-filter: blur(10px);
                display: grid;
                place-items: center;
                filter: drop-shadow(0 10px 16px rgba(15, 23, 42, 0.10));
        }
        .chip.c1 { left: 24px; top: 70px; }
        .chip.c2 { left: 110px; top: 170px; }
        .chip.c3 { right: 10px; top: 195px; }
        .chip.c4 { right: 118px; top: 92px; }
        .chip .chip-emo {
            font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", ui-rounded, "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
            font-size: 2.0rem;
            line-height: 1;
            filter: saturate(1.35) contrast(1.20);
            text-shadow: 0 1px 0 rgba(15, 23, 42, 0.12);
        }

        /* Origins section (map layout like the reference) */
        .pill.blue {
            background: rgba(221, 241, 255, 0.85);
            border-color: rgba(31, 41, 55, 0.06);
            color: rgba(0, 122, 255, 0.92);
        }
        .origins-grid {
            display: grid;
            grid-template-columns: 1fr 1.25fr;
            gap: 22px;
            align-items: start;
            margin-top: 10px;
            margin-bottom: 12px;
        }
        .origins-title {
            margin: 12px 0 10px 0;
            font-size: 2.6rem;
            letter-spacing: -0.03em;
            color: var(--ink);
            line-height: 1.05;
        }
        .origins-desc {
            margin: 0 0 16px 0;
            max-width: 60ch;
            color: var(--muted);
            font-size: 1.02rem;
            line-height: 1.55;
        }
        .origin-score {
            border-radius: 22px;
            padding: 16px 16px;
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(31, 41, 55, 0.08);
            box-shadow: 0 14px 30px rgba(15, 23, 42, 0.06);
        }
        .origin-score-top {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 12px;
        }
        .origin-score-label { color: rgba(15, 23, 42, 0.72); font-weight: 700; }
        .origin-score-value { color: rgba(0, 122, 255, 0.92); font-weight: 900; font-size: 1.5rem; }
        .origin-bar {
            margin-top: 12px;
            height: 10px;
            border-radius: 999px;
            background: rgba(31, 41, 55, 0.08);
            overflow: hidden;
        }
        .origin-bar > div {
            height: 100%;
            border-radius: 999px;
            background: rgba(46, 204, 113, 0.92);
        }
        .origin-foot {
            margin-top: 10px;
            color: rgba(15, 23, 42, 0.70);
        }

        .origin-map-metrics {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 14px;
            margin: 6px 0 10px 0;
        }
        .origin-metric {
            min-width: 96px;
            border-radius: 18px;
            padding: 12px 14px;
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(31, 41, 55, 0.08);
            box-shadow: 0 14px 30px rgba(15, 23, 42, 0.06);
            text-align: left;
        }
        .origin-metric .big { font-weight: 900; font-size: 1.6rem; color: rgba(0, 122, 255, 0.92); line-height: 1.0; }
        .origin-metric .small { color: rgba(15, 23, 42, 0.65); font-weight: 700; margin-top: 4px; }

        /* Make the pydeck map look like a rounded card */
        div[data-testid="stDeckGlJsonChart"] {
            border-radius: 26px;
            overflow: hidden;
            border: 1px solid rgba(31, 41, 55, 0.08);
            background:
                radial-gradient(700px 420px at 50% 45%, rgba(46, 204, 113, 0.14), rgba(234, 247, 255, 0.0) 62%),
                linear-gradient(120deg, rgba(238, 248, 255, 1.0), rgba(234, 247, 255, 1.0));
            box-shadow: 0 18px 34px rgba(15, 23, 42, 0.08);
        }
        div[data-testid="stDeckGlJsonChart"] canvas {
            border-radius: 22px;
        }
        .origin-legend {
            display: flex;
            justify-content: center;
            gap: 18px;
            margin-top: 10px;
            color: rgba(15, 23, 42, 0.65);
            font-weight: 700;
            font-size: 0.95rem;
        }
        .origin-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 999px;
            margin-right: 8px;
            vertical-align: middle;
        }
        .origin-dot.local { background: rgba(46, 204, 113, 0.92); }
        .origin-dot.eu { background: rgba(255, 149, 0, 0.92); }
        .origin-dot.world { background: rgba(255, 90, 95, 0.92); }

        /* Sections */
        .section-head {
                text-align: center;
                margin: 14px 0 18px 0;
        }
        .section-head h2 {
                margin: 0;
                font-size: 2.0rem;
                letter-spacing: -0.02em;
                color: var(--ink);
        }
        .section-head p {
                margin: 8px auto 0 auto;
                max-width: 70ch;
                color: var(--muted);
                font-size: 1.02rem;
        }
        .steps {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 16px;
                margin-bottom: 22px;
        }
        .step-card {
                position: relative;
                border-radius: 22px;
                padding: 22px;
                background: rgba(255, 255, 255, 0.70);
                border: 1px solid rgba(31, 41, 55, 0.08);
                backdrop-filter: blur(10px);
                min-height: 160px;
        }
        .step-card.primary {
                background: rgba(46, 204, 113, 0.88);
                color: #ffffff;
                border-color: rgba(46, 204, 113, 0.45);
        }
        .step-card .step-ico {
                width: 52px;
                height: 52px;
                border-radius: 18px;
                display: grid;
                place-items: center;
                background: rgba(46, 204, 113, 0.14);
                border: 1px solid rgba(31, 41, 55, 0.06);
                margin-bottom: 14px;
        }
        .step-card.primary .step-ico {
                background: rgba(255, 255, 255, 0.18);
                border-color: rgba(255, 255, 255, 0.18);
        }
        .step-card h3 { margin: 0 0 6px 0; font-size: 1.25rem; }
        .step-card p { margin: 0; color: rgba(15, 23, 42, 0.68); }
        .step-card.primary p { color: rgba(255, 255, 255, 0.88); }
        .step-card .num {
                position: absolute;
                right: 16px;
                top: 14px;
                font-size: 3.2rem;
                font-weight: 800;
                opacity: 0.10;
        }

        @media (max-width: 980px) {
                .hero2-inner { grid-template-columns: 1fr; }
                .hero2-right { min-height: 280px; }
                .phone { position: relative; right: auto; top: auto; margin: 10px auto 0 auto; }
                .chip { display: none; }
                .steps { grid-template-columns: 1fr; }
                .hero2-stats { grid-template-columns: 1fr; }
                .nav-links { display: none; }
        }
</style>
"""
    ),
        unsafe_allow_html=True,
)

st.markdown(
        _html_block(
            """
<div class="topnav">
    <div class="nav-inner">
        <div class="brand">
            <span class="mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" width="18" height="18">
                    <path d="M12 21s7-4.2 7-11a7 7 0 1 0-14 0c0 6.8 7 11 7 11Z" stroke="#2ECC71" stroke-width="1.8"/>
                    <path d="M12 12.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4Z" stroke="#2ECC71" stroke-width="1.8"/>
                </svg>
            </span>
            <span>FoodTrack</span>
        </div>
        <div class="nav-links">
            <a href="#fonctionnalites">Fonctionnalités</a>
            <a href="#scores">Scores</a>
            <a href="#origines">Origines</a>
        </div>
        <a class="nav-cta" href="#commencer">Commencer</a>
    </div>
</div>

<div class="hero2">
    <div class="hero2-inner">
        <div>
            <div class="pill">Nutrition intelligente</div>
            <h1>Fais les <span class="accent">bons choix</span></h1>
            <p>Scanne, compose, analyse. Découvre l'impact de ton alimentation sur ta santé et la planète.</p>
            <div class="hero2-actions">
                <a class="btn primary" href="#commencer">Scanner un produit</a>
                <a class="btn ghost" href="#fonctionnalites">En savoir plus</a>
            </div>
            <div class="hero2-stats">
                <div class="stat"><b>1M+</b><span>Produits</span></div>
                <div class="stat"><b>7</b><span>Indicateurs clés</span></div>
                <div class="stat"><b>100%</b><span>Transparent</span></div>
            </div>
        </div>

        <div class="hero2-right">
            <div class="chip c1" aria-hidden="true">
                <span class="chip-emo">🍎</span>
            </div>
            <div class="chip c2" aria-hidden="true">
                <span class="chip-emo">🥑</span>
            </div>
            <div class="chip c3" aria-hidden="true">
                <span class="chip-emo">🥗</span>
            </div>
            <div class="chip c4" aria-hidden="true">
                <span class="chip-emo">🍋</span>
            </div>

            <div class="phone" aria-hidden="true">
                <svg viewBox="0 0 360 420" xmlns="http://www.w3.org/2000/svg">
                    <rect x="70" y="18" width="220" height="384" rx="44" fill="rgba(255,255,255,0.70)" stroke="rgba(31,41,55,0.12)"/>
                    <rect x="95" y="56" width="170" height="24" rx="12" fill="rgba(31,41,55,0.08)"/>
                    <circle cx="112" cy="68" r="10" fill="rgba(46,204,113,0.90)"/>
                    <rect x="95" y="100" width="170" height="58" rx="18" fill="rgba(46,204,113,0.12)" stroke="rgba(31,41,55,0.08)"/>
                    <text x="112" y="130" font-family="ui-rounded, Segoe UI, system-ui" font-size="14" fill="rgba(15,23,42,0.78)">Score santé</text>
                    <text x="240" y="134" font-family="ui-rounded, Segoe UI, system-ui" font-size="18" font-weight="800" fill="rgba(46,204,113,0.95)">A</text>

                    <rect x="95" y="170" width="170" height="58" rx="18" fill="rgba(221,241,255,0.80)" stroke="rgba(31,41,55,0.08)"/>
                    <text x="112" y="200" font-family="ui-rounded, Segoe UI, system-ui" font-size="14" fill="rgba(15,23,42,0.78)">Eco-Score</text>
                    <text x="240" y="204" font-family="ui-rounded, Segoe UI, system-ui" font-size="18" font-weight="800" fill="rgba(0,122,255,0.85)">B</text>

                    <rect x="95" y="240" width="170" height="58" rx="18" fill="rgba(255, 245, 214, 0.70)" stroke="rgba(31,41,55,0.08)"/>
                    <text x="112" y="270" font-family="ui-rounded, Segoe UI, system-ui" font-size="14" fill="rgba(15,23,42,0.78)">NOVA</text>
                    <text x="235" y="274" font-family="ui-rounded, Segoe UI, system-ui" font-size="18" font-weight="800" fill="rgba(255, 149, 0, 0.90)">1</text>

                    <rect x="95" y="314" width="170" height="70" rx="18" fill="rgba(31,41,55,0.04)"/>
                    <rect x="150" y="340" width="60" height="38" rx="10" fill="rgba(221,241,255,0.80)"/>
                </svg>
            </div>
        </div>
    </div>
</div>

<div id="fonctionnalites"></div>
<div class="section-head">
    <h2>Comment ça marche ?</h2>
    <p>En trois étapes simples, prends le contrôle de ton alimentation.</p>
</div>
<div class="steps">
    <div class="step-card">
        <div class="num">1</div>
        <div class="step-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M4 7h16" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M7 7v10" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M17 7v10" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M6 17h12" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
        </div>
        <h3>Scanne</h3>
        <p>Scanne le code-barres ou recherche par nom pour ajouter tes produits.</p>
    </div>
    <div class="step-card primary">
        <div class="num">2</div>
        <div class="step-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M8 6h10" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M8 12h10" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M8 18h10" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M6 6h.01M6 12h.01M6 18h.01" stroke="#ffffff" stroke-width="4" stroke-linecap="round"/>
            </svg>
        </div>
        <h3>Compose</h3>
        <p>Compose tes repas en sélectionnant les produits. Garde une trace de tout ce que tu manges.</p>
    </div>
    <div class="step-card">
        <div class="num">3</div>
        <div class="step-ico" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M5 19V5" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M9 19V11" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M13 19V8" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
                <path d="M17 19V14" stroke="#2ECC71" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
        </div>
        <h3>Analyse</h3>
        <p>Découvre les scores nutritionnels, l'impact environnemental et les origines.</p>
    </div>
</div>
"""
                ),
                unsafe_allow_html=True,
)

if "selected_codes" not in st.session_state:
    st.session_state.selected_codes = []


def _add_code_to_selection(code: str) -> None:
    code = str(code).strip()
    if not code:
        return
    if code not in st.session_state.selected_codes:
        st.session_state.selected_codes.append(code)


def _remove_code_from_selection(code: str) -> None:
    code = str(code).strip()
    if not code:
        return
    st.session_state.selected_codes = [c for c in st.session_state.selected_codes if str(c).strip() != code]


def _clear_selection() -> None:
    st.session_state.selected_codes = []


def _grade_to_score(grade: str | None) -> float | None:
    if not grade:
        return None
    g = str(grade).strip().upper()
    mapping = {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1}
    return float(mapping.get(g)) if g in mapping else None


def _render_selected_products(
    codes: list[str],
    *,
    key_prefix: str,
    allow_remove: bool,
) -> None:
    if not codes:
        st.info("Aucun produit sélectionné.")
        return

    df = get_products_by_codes(codes)
    found = {str(row.get("code") or ""): row for _, row in df.iterrows()} if not df.empty else {}

    for idx, code in enumerate(codes):
        code = str(code).strip()
        row = found.get(code)
        if row is None:
            st.write(f"(inconnu) ({code})")
            continue
        name = str(row.get("product_name") or "").strip()
        brands = str(row.get("brands") or "").strip()
        thumb = _thumb_from_raw(row.get("raw_json"))
        suffix = f" — {brands}" if brands else ""
        label = f"{name} ({code}){suffix}" if name else f"({code}){suffix}"

        if allow_remove:
            cols = st.columns([1, 8, 2])
            with cols[0]:
                _render_thumb(thumb, width=36)
            cols[1].write(label)
            if cols[2].button("Supprimer", key=f"{key_prefix}_rm_{code}_{idx}"):
                _remove_code_from_selection(code)
                st.rerun()
        else:
            cols = st.columns([1, 10])
            with cols[0]:
                _render_thumb(thumb, width=36)
            cols[1].write(label)


def _thumb_from_raw(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    url = obj.get("image_front_small_url") or obj.get("image_small_url")
    return str(url) if url else None


def _render_consumed_products_today(*, key_prefix: str) -> None:
    df_today = read_consumed_items_today()
    if df_today.empty:
        st.caption("Aucun produit ajouté aujourd'hui.")
        return

    # Group by code so user can delete a product globally.
    g = (
        df_today.groupby("code", dropna=False)
        .agg(
            product_name=("product_name", "first"),
            brands=("brands", "first"),
            raw_json=("raw_json", "first"),
            occurrences=("code", "size"),
        )
        .reset_index()
    )

    for idx, r in g.iterrows():
        code = str(r.get("code") or "").strip()
        name = str(r.get("product_name") or "").strip()
        brands = str(r.get("brands") or "").strip()
        suffix = f" — {brands}" if brands else ""
        label = f"{name} ({code}){suffix}" if name else f"({code}){suffix}"
        thumb = _thumb_from_raw(r.get("raw_json"))
        count = int(r.get("occurrences") or 0)

        cols = st.columns([1, 7, 1.4, 2.6])
        with cols[0]:
            _render_thumb(thumb, width=36)
        cols[1].write(label)
        cols[2].caption(f"×{count}")
        if cols[3].button("Supprimer", key=f"{key_prefix}_del_{code}_{idx}"):
            deleted = delete_code_from_all_meals(code)
            st.success(f"Supprimé: {deleted} occurrence(s)")
            st.rerun()


@st.cache_data(show_spinner=False, ttl=3600)
def _fetch_image_bytes(url: str) -> bytes | None:
    url = (url or "").strip()
    if not url:
        return None
    try:
        verify: bool | str
        if OFF_CA_BUNDLE:
            verify = OFF_CA_BUNDLE
        else:
            verify = OFF_SSL_VERIFY

        r = requests.get(url, timeout=15, verify=verify, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()

        # Guardrail: avoid very large payloads.
        content_length = r.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > 2_000_000:
                    return None
            except Exception:
                pass
        if len(r.content) > 2_000_000:
            return None

        return r.content
    except Exception:
        return None


def _render_thumb(url: str | None, *, width: int = 48) -> None:
    if not url:
        st.write("")
        return
    data = _fetch_image_bytes(str(url))
    if data:
        st.image(data, width=width)
    else:
        st.write("")


st.markdown('<div id="commencer"></div>', unsafe_allow_html=True)
st.subheader("Commencer")

col_left, col_right = st.columns([3, 2], gap="large")

with col_left:
    with st.container(border=True):
        st.markdown("### Rechercher par nom")
        q = st.text_input("Recherche", placeholder="Ex: chocolat", label_visibility="collapsed")

        q = (q or "").strip()
        if q and len(q) < 3:
            st.caption("Tape au moins 3 caractères.")

        if q and len(q) >= 3:
            # Scrollable result area (prevents the page from getting too tall)
            results_box = st.container(height=420)
            with results_box:
                results = search_products_by_name(q, limit=25)
                shown_codes: set[str] = set()

                if not results.empty:
                    st.caption(f"Cache local: {len(results)}")
                    for _, row in results.iterrows():
                        code = str(row.get("code") or "").strip()
                        if not code:
                            continue
                        shown_codes.add(code)

                        name = str(row.get("product_name") or "")
                        brands = str(row.get("brands") or "")
                        suffix = f" — {brands}" if brands else ""
                        label = f"{name} ({code}){suffix}".strip()
                        thumb = _thumb_from_raw(row.get("raw_json"))

                        cols = st.columns([1, 6, 2])
                        with cols[0]:
                            _render_thumb(thumb, width=48)
                        cols[1].write(label)
                        if cols[2].button("Ajouter", key=f"add_cache_{code}"):
                            _add_code_to_selection(code)
                else:
                    st.caption("Cache local: 0")

                # Full database search (online)
                try:
                    online = search_products_by_name_online(q, limit=25)
                except Exception:
                    online = []
                    st.warning(
                        "Recherche en ligne indisponible (réseau/SSL). "
                        "Si tu es derrière un proxy/certificat corporate: "
                        "relance Streamlit avec `$env:OFF_SSL_VERIFY='0'` (session PowerShell) "
                        "ou configure `$env:OFF_CA_BUNDLE='C:\\path\\corp-ca.pem'`."
                    )

                extra = [
                    p
                    for p in online
                    if str(p.get("code") or "").strip()
                    and str(p.get("code") or "").strip() not in shown_codes
                ]
                if extra:
                    st.caption(f"En ligne: {len(extra)}")
                    for p in extra:
                        code = str(p.get("code") or "").strip()
                        name = str(p.get("product_name") or "")
                        brands = str(p.get("brands") or "")
                        suffix = f" — {brands}" if brands else ""
                        label = f"{name} ({code}){suffix}".strip()
                        thumb = p.get("image_front_small_url") or p.get("image_small_url")

                        cols = st.columns([1, 6, 2])
                        with cols[0]:
                            _render_thumb(thumb, width=48)
                        cols[1].write(label)
                        if cols[2].button("Ajouter", key=f"add_online_{code}"):
                            full = fetch_product_by_code(code)
                            if full is not None:
                                upsert_products([full])
                            _add_code_to_selection(code)

with col_right:
    with st.container(border=True):
        st.markdown("### Sélection en cours")
        if not st.session_state.selected_codes:
            st.info("Aucun produit sélectionné pour l’instant.")
        else:
            header = st.columns([1, 8, 2])
            header[1].caption(f"{len(st.session_state.selected_codes)} produit(s)")
            if header[2].button("Tout supprimer", key="sel_clear", type="secondary"):
                _clear_selection()
                st.rerun()

            # Scrollable selection list
            sel_box = st.container(height=320)
            with sel_box:
                _render_selected_products(
                    st.session_state.selected_codes,
                    key_prefix="step1",
                    allow_remove=True,
                )

        st.divider()
        st.markdown("#### Produits déjà ajoutés (aujourd'hui)")
        st.caption("Ce sont les produits enregistrés dans tes repas. Ils alimentent le récap et la carte.")
        hist_box = st.container(height=260)
        with hist_box:
            _render_consumed_products_today(key_prefix="today_hist")

        st.divider()
        st.markdown("#### Réinitialiser")
        confirm_reset = st.checkbox("Confirmer la réinitialisation", value=False)
        actions = st.columns([1, 1], gap="small")
        if actions[0].button("Effacer repas (aujourd'hui)", disabled=not confirm_reset):
            deleted = delete_meals_today()
            _clear_selection()
            st.success(f"Repas du jour supprimés: {deleted}")
            st.rerun()
        if actions[1].button("Effacer tout l'historique", disabled=not confirm_reset):
            deleted = delete_all_meals()
            _clear_selection()
            st.success(f"Repas supprimés (total): {deleted}")
            st.rerun()

    with st.container(border=True):
        st.markdown("### Ajouter par code-barres")
        codes_text = st.text_area(
            "Codes-barres",
            height=120,
            placeholder="Ex: 3017620422003\n…",
            label_visibility="collapsed",
        )
        if st.button("Ajouter ces codes", type="primary"):
            codes = [c for c in re.split(r"\s+", codes_text.strip()) if c]
            added, fetched = 0, 0
            for code in codes:
                _add_code_to_selection(code)
                added += 1

                p = fetch_product_by_code(code)
                if p is not None:
                    upsert_products([p])
                    fetched += 1
            st.success(f"Sélection: +{added} codes (fetched: {fetched})")

    with st.container(border=True):
        st.markdown("### 2) Ajouter ce repas")
        if not st.session_state.selected_codes:
            st.info("Sélectionne des produits à gauche, puis ajoute le repas ici.")
        else:
            st.caption(f"Produits sélectionnés: {len(st.session_state.selected_codes)}")
            # Read-only here (deletion happens in Step 1)
            _render_selected_products(
                st.session_state.selected_codes,
                key_prefix="step2",
                allow_remove=False,
            )

            if st.button("Ajouter ce repas", type="primary"):
                add_meal(st.session_state.selected_codes)
                st.session_state.selected_codes = []
                st.success("Repas ajouté.")
                st.rerun()


st.markdown('<div id="scores"></div>', unsafe_allow_html=True)
st.divider()
st.markdown(
    """
<div class="section-head" style="margin-top: 10px;">
  <h2>Tes indicateurs clés</h2>
  <p>Analyse de ton repas : santé, impact, origines — et un suivi clair au même endroit.</p>
</div>
""",
    unsafe_allow_html=True,
)

df = read_consumed_items_today()
df_week = read_consumed_items_since(7)
if df.empty:
    st.info("Aucun repas ajouté aujourd'hui.")
    st.stop()

df = df.copy()
# Always compute averages: if OFF doesn't provide the metric (unknown/None),
# we use a neutral default (middle value) so the recap shows a number.
df["score_sante"] = df["nutriscore_grade"].apply(_grade_to_score).astype("float64").fillna(3.0)
df["score_planete"] = df["ecoscore_grade"].apply(_grade_to_score).astype("float64").fillna(3.0)

# Transformation alimentaire: NOVA (1-4)
df["transformation_nova"] = pd.to_numeric(df.get("nova_group"), errors="coerce").astype("float64").fillna(2.5)

# Empreinte carbone: gCO2e/100g (best-effort)
df["empreinte_carbone_gco2e_100g"] = (
    pd.to_numeric(df.get("carbon_footprint_gco2e_100g"), errors="coerce").astype("float64").fillna(0.0)
)

df_week = df_week.copy()
if not df_week.empty:
    df_week["empreinte_carbone_gco2e_100g"] = (
        pd.to_numeric(df_week.get("carbon_footprint_gco2e_100g"), errors="coerce").astype("float64").fillna(0.0)
    )


def _score_to_grade(score_1_to_5: float) -> str:
        try:
                s = float(score_1_to_5)
        except Exception:
                return "?"
        if s >= 4.5:
                return "A"
        if s >= 3.5:
                return "B"
        if s >= 2.5:
                return "C"
        if s >= 1.5:
                return "D"
        return "E"


def _additives_count_to_score_0_to_5(n: float | int | None) -> int | None:
        if n is None:
                return None
        try:
                v = int(n)
        except Exception:
                return None
        if v <= 0:
                return 5
        if v <= 2:
                return 4
        if v <= 5:
                return 3
        if v <= 10:
                return 2
        if v <= 20:
                return 1
        return 0


def _render_key_indicators(df_day: pd.DataFrame, df_week_: pd.DataFrame) -> None:
        health_mean = float(df_day["score_sante"].mean())
        planet_mean = float(df_day["score_planete"].mean())
        carbon_mean = float(df_day["empreinte_carbone_gco2e_100g"].mean())
        nova_mean = float(df_day["transformation_nova"].mean())

        health_grade = _score_to_grade(health_mean)
        planet_grade = _score_to_grade(planet_mean)

        additives_col = df_day.get("additives_n")
        if additives_col is None:
            add_score = 3
        else:
            add_n = pd.to_numeric(additives_col, errors="coerce")
            add_scores_num = pd.to_numeric(add_n.map(_additives_count_to_score_0_to_5), errors="coerce")
            add_score = int(round(float(add_scores_num.mean()))) if add_scores_num.notna().any() else 3

        nova_col = df_day.get("nova_group")
        if nova_col is None:
            known = 0
            ultra = 0
        else:
            nova_known = pd.to_numeric(nova_col, errors="coerce")
            known = int(nova_known.notna().sum())
            ultra = int((nova_known == 4).sum())

        total_day = float(df_day["empreinte_carbone_gco2e_100g"].sum())
        total_week = float(df_week_["empreinte_carbone_gco2e_100g"].sum()) if not df_week_.empty else 0.0

        categories_col = df_day.get("categories")
        if categories_col is None:
            diversity = 0
        else:
            cats = categories_col.fillna("").astype(str)
            cat_first = cats.apply(lambda s: (s.split(",")[0].strip() if s else "").lower())
            diversity = int(cat_first[cat_first != ""].nunique())

        st.markdown(
            _html_block(
                f"""
<div class="key-grid">
    <div class="key-card bg-green">
        <div class="key-top">
            <div class="key-ico" aria-hidden="true">🍎</div>
            <div>
                <div class="key-title">Score Santé</div>
            </div>
        </div>
        <div><span class="key-big">{health_grade}</span><span class="key-range">à E</span></div>
        <p class="key-desc">Évalue la qualité nutritionnelle de tes produits sur une échelle de A (excellent) à E.</p>
    </div>

    <div class="key-card bg-blue">
        <div class="key-top">
            <div class="key-ico" aria-hidden="true">🍋</div>
            <div>
                <div class="key-title">Score Planète</div>
            </div>
        </div>
        <div><span class="key-big">{planet_grade}</span><span class="key-range">Eco-Score</span></div>
        <p class="key-desc">Mesure l'empreinte environnementale et l'impact CO2 de chaque produit.</p>
    </div>

    <div class="key-card bg-amber">
        <div class="key-top">
            <div class="key-ico" aria-hidden="true">🥗</div>
            <div>
                <div class="key-title">Transformation NOVA</div>
            </div>
        </div>
        <div><span class="key-big">{nova_mean:.1f}</span><span class="key-range">à 4</span></div>
        <p class="key-desc">Indique le niveau de transformation des aliments, de 1 (naturel) à 4 (ultra-transformé).</p>
    </div>

    <div class="key-card bg-pink">
        <div class="key-top">
            <div class="key-ico" aria-hidden="true">🥑</div>
            <div>
                <div class="key-title">Score Additifs</div>
            </div>
        </div>
        <div><span class="key-big">{add_score}</span><span class="key-range">/ 5</span></div>
        <p class="key-desc">Analyse la présence d'additifs dans tes produits (score proxy basé sur le nombre d'additifs).</p>
    </div>
</div>

<div class="mini-grid">
    <div class="mini-card">
        <div class="mini-ico" aria-hidden="true">🗺️</div>
        <div class="mini-title"><a href="#origines">Carte des origines</a></div>
        <div class="mini-sub">Visualise d'où viennent tes aliments</div>
    </div>
    <div class="mini-card">
        <div class="mini-ico" aria-hidden="true">🥗</div>
        <div class="mini-title">Diversité nutritionnelle</div>
        <div class="mini-sub">{diversity} catégories principales détectées</div>
    </div>
    <div class="mini-card">
        <div class="mini-ico" aria-hidden="true">☁️</div>
        <div class="mini-title">Empreinte carbone</div>
        <div class="mini-sub">Total aujourd'hui: {total_day:.0f} gCO2e | 7 jours: {total_week:.0f} gCO2e</div>
    </div>
</div>
"""
    ),
        unsafe_allow_html=True,
    )

        with st.expander("Détails des indicateurs", expanded=False):
                st.markdown("### Empreinte carbone")
                st.write(f"Moyenne: **{carbon_mean:.1f} gCO2e/100g**")
                st.caption("Valeur gCO2e/100g disponible (proxy si portions inconnues).")

                st.markdown("### Transformation alimentaire (NOVA)")
                st.write(f"Produits ultra-transformés (NOVA 4): **{ultra}/{known}**")
                share_ultra = float(ultra / known) if known else 0.0
                st.progress(min(max(share_ultra, 0.0), 1.0))


_render_key_indicators(df, df_week)

st.markdown('<div id="origines"></div>', unsafe_allow_html=True)
st.markdown("### Carte des origines")
user_country = st.selectbox("Ton pays", ["France", "Belgique", "Suisse", "Canada"], index=0)

def _origin_country_from_raw(raw: str | None, fallback_countries: str | None) -> str | None:
    if raw:
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {}
        origins = (obj.get("origins") or "").strip()
        if origins:
            # take first listed origin
            return origins.split(",")[0].strip()
        origins_tags = obj.get("origins_tags")
        if isinstance(origins_tags, list) and origins_tags:
            # tags look like "en:france"; keep the last part
            tag = str(origins_tags[0])
            return tag.split(":", 1)[-1].replace("-", " ").title().strip() or None
        mp = (obj.get("manufacturing_places") or "").strip()
        if mp:
            return mp.split(",")[0].strip()

    if fallback_countries:
        # Not origin, but better than nothing
        return str(fallback_countries).split(",")[0].strip()
    return None


def _thumb_from_product_raw(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    url = obj.get("image_front_small_url") or obj.get("image_small_url")
    return str(url) if url else None

def _norm_country(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    # Europe
    "france": (46.2276, 2.2137),
    "belgium": (50.5039, 4.4699),
    "belgique": (50.5039, 4.4699),
    "switzerland": (46.8182, 8.2275),
    "suisse": (46.8182, 8.2275),
    "germany": (51.1657, 10.4515),
    "allemagne": (51.1657, 10.4515),
    "spain": (40.4637, -3.7492),
    "espagne": (40.4637, -3.7492),
    "italy": (41.8719, 12.5674),
    "italie": (41.8719, 12.5674),
    "united kingdom": (55.3781, -3.4360),
    "royaume uni": (55.3781, -3.4360),
    "netherlands": (52.1326, 5.2913),
    "pays bas": (52.1326, 5.2913),
    "portugal": (39.3999, -8.2245),
    "ireland": (53.1424, -7.6921),
    "irelande": (53.1424, -7.6921),
    # North America
    "canada": (56.1304, -106.3468),
    "united states": (37.0902, -95.7129),
    "usa": (37.0902, -95.7129),
    "mexico": (23.6345, -102.5528),
    "mexique": (23.6345, -102.5528),
    # South America
    "brazil": (-14.2350, -51.9253),
    "bresil": (-14.2350, -51.9253),
    "argentina": (-38.4161, -63.6167),
    "argentine": (-38.4161, -63.6167),
    "chile": (-35.6751, -71.5430),
    # Africa
    "morocco": (31.7917, -7.0926),
    "maroc": (31.7917, -7.0926),
    "tunisia": (33.8869, 9.5375),
    "tunisie": (33.8869, 9.5375),
    "algeria": (28.0339, 1.6596),
    "algerie": (28.0339, 1.6596),
    "south africa": (-30.5595, 22.9375),
    "afrique du sud": (-30.5595, 22.9375),
    # Asia
    "china": (35.8617, 104.1954),
    "chine": (35.8617, 104.1954),
    "japan": (36.2048, 138.2529),
    "japon": (36.2048, 138.2529),
    "india": (20.5937, 78.9629),
    "inde": (20.5937, 78.9629),
    "turkey": (38.9637, 35.2433),
    "turquie": (38.9637, 35.2433),
    # Oceania
    "australia": (-25.2744, 133.7751),
    "new zealand": (-40.9006, 174.8860),
    "nouvelle zelande": (-40.9006, 174.8860),
}


def _country_to_latlon(country: str | None) -> tuple[float, float] | None:
    if not country:
        return None
    key = _norm_country(str(country))
    if key in _COUNTRY_CENTROIDS:
        return _COUNTRY_CENTROIDS[key]

    # Try to match a contained country name (handles strings like "France (Bretagne)")
    for k, v in _COUNTRY_CENTROIDS.items():
        if k and k in key:
            return v
    return None


def _jitter_latlon(lat: float, lon: float, seed: str) -> tuple[float, float]:
    """Small deterministic jitter so multiple products in same country don't overlap."""
    h = abs(hash(seed)) % 10_000
    # ~ +/- 0.45 degrees
    j1 = ((h % 97) / 97.0 - 0.5) * 0.9
    j2 = (((h // 97) % 97) / 97.0 - 0.5) * 0.9
    lat2 = max(min(lat + j1, 85.0), -85.0)
    lon2 = lon + j2
    if lon2 > 180.0:
        lon2 -= 360.0
    if lon2 < -180.0:
        lon2 += 360.0
    return lat2, lon2


df_loc = df[["product_name", "code", "countries", "raw_json"]].copy()
df_loc["origin_country"] = df_loc.apply(
    lambda r: _origin_country_from_raw(r.get("raw_json"), r.get("countries")), axis=1
)
df_loc["thumbnail"] = df_loc["raw_json"].apply(_thumb_from_product_raw)
df_loc["is_local"] = df_loc["origin_country"].fillna("").str.contains(user_country, case=False, na=False)

local_count = int(df_loc["is_local"].sum())
total_count = int(len(df_loc))
locality_score = 5.0 * (local_count / total_count) if total_count else 0.0

local_pct = (100.0 * local_count / total_count) if total_count else 0.0
carbon_avg = float(df["empreinte_carbone_gco2e_100g"].mean()) if "empreinte_carbone_gco2e_100g" in df.columns else 0.0

country_flag = {
    "France": "🇫🇷",
    "Belgique": "🇧🇪",
    "Suisse": "🇨🇭",
    "Canada": "🇨🇦",
}.get(user_country, "")

map_df = df_loc.dropna(subset=["origin_country"]).copy()
total_items = int(len(df_loc))
unique_countries = int(map_df["origin_country"].nunique()) if not map_df.empty else 0

col_left, col_right = st.columns([1.05, 1.25], gap="large")
with col_left:
    st.markdown(
        _html_block(
            f"""
<div class="pill blue"><span aria-hidden="true">🌐</span>Traçabilité</div>
<div class="origins-title">D'où viennent tes aliments&nbsp;?</div>
<p class="origins-desc">Visualise sur une carte interactive l'origine de chaque produit que tu consommes. Plus c'est local, meilleur c'est pour la planète.</p>

<div class="origin-score">
  <div class="origin-score-top">
    <div class="origin-score-label">Score localité</div>
    <div class="origin-score-value">{locality_score:.1f} / 5</div>
  </div>
  <div class="origin-bar"><div style="width:{max(0.0, min(local_pct, 100.0)):.0f}%;"></div></div>
  <div class="origin-foot">{local_pct:.0f}% de tes produits proviennent de {user_country} {country_flag}</div>
</div>

<div style="margin-top: 14px;">
  <a class="btn primary" href="#origines-map">Explorer la carte</a>
</div>
"""
        ),
        unsafe_allow_html=True,
    )

with col_right:
    st.markdown('<div id="origines-map"></div>', unsafe_allow_html=True)

    st.markdown(
        _html_block(
            f"""
<div class="origin-map-metrics">
  <div class="origin-metric"><div class="big">{unique_countries}</div><div class="small">Pays</div></div>
  <div class="origin-metric"><div class="big">{carbon_avg:.0f}g</div><div class="small">CO2e/100g</div></div>
</div>
"""
        ),
        unsafe_allow_html=True,
    )

    if map_df.empty:
        st.info("Pas assez d'infos d'origine pour afficher une carte.")
    else:
        latlon = map_df["origin_country"].apply(_country_to_latlon)
        map_df["lat"] = latlon.apply(lambda t: t[0] if t else None)
        map_df["lon"] = latlon.apply(lambda t: t[1] if t else None)

        unknown = map_df[map_df[["lat", "lon"]].isna().any(axis=1)].copy()
        map_df = map_df.dropna(subset=["lat", "lon"]).copy()

        if map_df.empty:
            st.info(
                "Carte indisponible: pays non reconnus (centroïdes manquants). "
                "Dis-moi 2–3 exemples de valeurs dans *Origines* et j'ajoute les correspondances."
            )
        else:
            europe_keys = {
                "france",
                "belgium",
                "belgique",
                "switzerland",
                "suisse",
                "germany",
                "allemagne",
                "spain",
                "espagne",
                "italy",
                "italie",
                "united kingdom",
                "royaume uni",
                "netherlands",
                "pays bas",
                "portugal",
                "ireland",
                "irelande",
            }

            def _bucket_for_origin(origin: str | None) -> str:
                if not origin:
                    return "Monde"
                o = str(origin)
                if user_country and user_country.lower() in o.lower():
                    return "Local"
                if _norm_country(o) in europe_keys:
                    return "Europe"
                return "Monde"

            pts = map_df[["product_name", "code", "origin_country", "lat", "lon"]].copy()
            pts["bucket"] = pts["origin_country"].apply(_bucket_for_origin)

            def _color_for_bucket(b: str) -> list[int]:
                if b == "Local":
                    return [46, 204, 113, 180]
                if b == "Europe":
                    return [255, 149, 0, 175]
                return [255, 90, 95, 175]

            pts["fill_color"] = pts["bucket"].apply(_color_for_bucket)

            pts["lat2"] = pts.apply(
                lambda r: _jitter_latlon(
                    float(r["lat"]),
                    float(r["lon"]),
                    str(r.get("code") or r.get("product_name") or ""),
                )[0],
                axis=1,
            )
            pts["lon2"] = pts.apply(
                lambda r: _jitter_latlon(
                    float(r["lat"]),
                    float(r["lon"]),
                    str(r.get("code") or r.get("product_name") or ""),
                )[1],
                axis=1,
            )
            deck_df = pts.rename(columns={"lat2": "latitude", "lon2": "longitude"})

            center = _country_to_latlon(user_country) or (
                float(deck_df["latitude"].mean()),
                float(deck_df["longitude"].mean()),
            )
            deck = pdk.Deck(
                initial_view_state=pdk.ViewState(
                    latitude=float(center[0]),
                    longitude=float(center[1]),
                    zoom=1.6,
                    pitch=0,
                ),
                map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=deck_df,
                        get_position="[longitude, latitude]",
                        get_radius=170000,
                        get_fill_color="fill_color",
                        pickable=True,
                    )
                ],
                tooltip={"text": "{product_name}\n{origin_country}\n{bucket}\n{code}"},
            )
            st.pydeck_chart(deck, height=390)

            st.markdown(
                _html_block(
                    """
<div class="origin-legend">
  <span><span class="origin-dot local"></span>Local</span>
  <span><span class="origin-dot eu"></span>Europe</span>
  <span><span class="origin-dot world"></span>Monde</span>
</div>
"""
                ),
                unsafe_allow_html=True,
            )

        if len(unknown) > 0:
            with st.expander(f"Produits sans pays reconnu ({len(unknown)})"):
                for _, r in unknown.iterrows():
                    st.write(f"{r.get('product_name','')} ({r.get('code','')}) — {r.get('origin_country','')}")

st.markdown("### Indice de diversité nutritionnelle")
nutrient_keys = [
    "proteins_100g",
    "fiber_100g",
    "fat_100g",
    "carbohydrates_100g",
    "sugars_100g",
    "salt_100g",
    "saturated-fat_100g",
]

present: set[str] = set()
for raw in df.get("nutriments_json", []):
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        obj = {}
    for k in nutrient_keys:
        v = obj.get(k)
        if v is None:
            continue
        try:
            if float(v) > 0:
                present.add(k)
        except Exception:
            continue

div_score = (len(present) / len(nutrient_keys)) if nutrient_keys else 0.0
st.write(f"Diversité: **{div_score*100:.0f}%** ({len(present)}/{len(nutrient_keys)} nutriments détectés)")
st.caption("Basé sur la présence de nutriments dans les données OFF (par 100g).")

st.markdown("### Score “Additifs”")

def _additives_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        obj = json.loads(raw)
    except Exception:
        return 0
    tags = obj.get("additives_tags")
    if isinstance(tags, list):
        return len(tags)
    n = obj.get("additives_n")
    try:
        return int(n) if n is not None else 0
    except Exception:
        return 0


df_add = df[["raw_json"]].copy()
df_add["additives_n"] = df_add["raw_json"].apply(_additives_count)
total_add = int(df_add["additives_n"].sum())
with_add = int((df_add["additives_n"] > 0).sum())

def _add_score(n: float) -> float:
    if n <= 0:
        return 5.0
    if n <= 2:
        return 4.0
    if n <= 4:
        return 3.0
    if n <= 7:
        return 2.0
    return 1.0


avg_add_score = float(df_add["additives_n"].apply(_add_score).mean()) if not df_add.empty else 0.0
st.write(f"Produits avec additifs: **{with_add}/{len(df_add)}** | Total additifs détectés: **{total_add}**")
st.write(f"Score additifs (proxy): **{avg_add_score:.2f}/5**")
st.caption("Pondération simple par quantité (pas de classification de risque intégrée).")

st.markdown("### Indice “Impact eau”")
water_path = Path("data") / "water_footprint_by_category.csv"
if not water_path.exists():
    st.info(
        "Optionnel: ajoute un fichier `data/water_footprint_by_category.csv` "
        "(colonnes: category, water_l_per_kg) pour activer cet indicateur."
    )
else:
    try:
        wf = pd.read_csv(water_path)
        wf = wf.dropna(subset=["category", "water_l_per_kg"]).copy()
        wf["category"] = wf["category"].astype(str).str.lower()
        wf["water_l_per_kg"] = pd.to_numeric(wf["water_l_per_kg"], errors="coerce")
        wf = wf.dropna(subset=["water_l_per_kg"]).copy()
        map_w = {r["category"]: float(r["water_l_per_kg"]) for _, r in wf.iterrows()}

        def _first_category(cat: str | None) -> str | None:
            if not cat:
                return None
            return str(cat).split(",")[0].strip().lower() or None

        cats = df.get("categories").apply(_first_category)
        water_l_per_kg = cats.map(map_w)
        # Proxy total: assume 100g per product -> 0.1 kg each
        total_water_l = float((water_l_per_kg.fillna(0.0) * 0.1).sum())
        st.write(f"Impact eau (proxy): **{total_water_l:.0f} L** (hypothèse 100g par produit)")
    except Exception:
        st.warning("Fichier impact eau présent, mais format illisible.")
