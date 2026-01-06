from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from off_cache.cache_db import get_db_path, read_products_dataframe, read_meta


st.set_page_config(page_title="OpenFoodFacts – Cache local", layout="wide")

st.title("OpenFoodFacts – Cache local")

meta = read_meta()
last_sync = meta.get("last_sync_utc")

col1, col2, col3 = st.columns(3)
col1.metric("Cache SQLite", str(get_db_path()))
col2.metric("Dernière synchro (UTC)", last_sync or "—")
col3.metric("Version schéma", meta.get("schema_version", "—"))

st.divider()

with st.spinner("Lecture du cache..."):
    df = read_products_dataframe(limit=200_000)

if df.empty:
    st.warning("Cache vide. Lance d’abord: `python -m off_cache.update --country fr --recent-pages 3 --page-size 200`")
    st.stop()

# Nettoyage minimal
for c in ["brands", "categories", "nutriscore_grade", "countries"]:
    if c in df.columns:
        df[c] = df[c].fillna("")

st.subheader("Aperçu")

c1, c2, c3, c4 = st.columns(4)

c1.metric("Produits", f"{len(df):,}".replace(",", " "))

with pd.option_context("mode.use_inf_as_na", True):
    sugar = pd.to_numeric(df.get("sugars_100g"), errors="coerce")

c2.metric("Sucre médian (g/100g)", f"{sugar.median():.1f}" if sugar.notna().any() else "—")

nutri = df.get("nutriscore_grade", pd.Series(dtype=str)).str.upper()
nutri_counts = nutri.value_counts()

c3.metric("Nutri-Score A (%)", f"{(nutri_counts.get('A', 0) / max(1, len(df)) * 100):.1f}")

last_mod = pd.to_numeric(df.get("last_modified_t"), errors="coerce")
if last_mod.notna().any():
    last_date = dt.datetime.utcfromtimestamp(int(last_mod.max())).strftime("%Y-%m-%d %H:%M")
else:
    last_date = "—"

c4.metric("Dernière modif produit", last_date)

st.divider()

st.subheader("Répartitions")

left, right = st.columns(2)

with left:
    if not nutri_counts.empty:
        fig = px.bar(
            nutri_counts.reset_index(),
            x="index",
            y="count",
            labels={"index": "Nutri-Score", "count": "Produits"},
            title="Nutri-Score (sur le cache)",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Pas de Nutri-Score dans les données disponibles.")

with right:
    brands = df.get("brands", pd.Series(dtype=str))
    top_brands = (
        brands[brands.str.len() > 0]
        .str.split(",")
        .explode()
        .str.strip()
        .value_counts()
        .head(20)
    )
    if not top_brands.empty:
        fig = px.bar(
            top_brands.reset_index(),
            x="index",
            y="count",
            labels={"index": "Marque", "count": "Produits"},
            title="Top marques (20)",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Pas de marques exploitables.")

st.divider()

st.subheader("Table")

cols = [
    c
    for c in [
        "code",
        "product_name",
        "brands",
        "countries",
        "nutriscore_grade",
        "energy-kcal_100g",
        "sugars_100g",
        "salt_100g",
        "last_modified_t",
    ]
    if c in df.columns
]

st.dataframe(df[cols].head(500), use_container_width=True)
