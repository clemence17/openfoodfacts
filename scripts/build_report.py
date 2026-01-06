from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd
import plotly.express as px

# Allow running as a script without installing the project.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from off_cache.cache_db import read_products_dataframe


def build_report(output_dir: Path, limit: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    df = read_products_dataframe(limit=limit)

    if df.empty:
        html = """<!doctype html>
<html lang=\"fr\"><head><meta charset=\"utf-8\"><title>OpenFoodFacts – Rapport</title></head>
<body><h1>OpenFoodFacts – Rapport</h1><p>Cache vide. Lance d'abord une mise à jour.</p></body></html>"""
        (output_dir / "index.html").write_text(html, encoding="utf-8")
        (output_dir / ".nojekyll").write_text("", encoding="utf-8")
        return

    # Metrics
    sugars = pd.to_numeric(df.get("sugars_100g"), errors="coerce")
    nutri = df.get("nutriscore_grade", pd.Series(dtype=str)).fillna("").astype(str).str.upper()
    nutri_counts = nutri.value_counts().sort_index()

    # Charts
    fig_nutri = None
    if not nutri_counts.empty:
        nutri_df = nutri_counts.reset_index()
        # With pandas, the first column is typically named after the original series (e.g. 'nutriscore_grade')
        nutri_x = nutri_df.columns[0]
        fig_nutri = px.bar(
            nutri_df,
            x=nutri_x,
            y="count",
            labels={nutri_x: "Nutri-Score", "count": "Produits"},
            title="Nutri-Score (sur le cache)",
        )

    brands = df.get("brands", pd.Series(dtype=str)).fillna("")
    top_brands = (
        brands[brands.str.len() > 0]
        .str.split(",")
        .explode()
        .str.strip()
        .value_counts()
        .head(20)
    )

    fig_brands = None
    if not top_brands.empty:
        brands_df = top_brands.reset_index()
        brands_x = brands_df.columns[0]
        fig_brands = px.bar(
            brands_df,
            x=brands_x,
            y="count",
            labels={brands_x: "Marque", "count": "Produits"},
            title="Top marques (20)",
        )

    # Build HTML
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="fr">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>OpenFoodFacts – Rapport</title>")
    parts.append(
        """
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:1100px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.card{border:1px solid #e5e7eb;border-radius:10px;padding:12px}
.small{color:#6b7280;font-size:12px}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){.grid{grid-template-columns:repeat(1,minmax(0,1fr))}}
</style>
"""
    )
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<h1>OpenFoodFacts – Rapport</h1>")
    parts.append('<p class="small">Généré automatiquement via GitHub Actions.</p>')

    parts.append('<div class="grid">')
    parts.append(f'<div class="card"><div class="small">Produits</div><div><b>{len(df):,}</b></div></div>'.replace(",", " "))
    parts.append(
        f'<div class="card"><div class="small">Sucre médian (g/100g)</div><div><b>{sugars.median():.1f}</b></div></div>'
        if sugars.notna().any()
        else '<div class="card"><div class="small">Sucre médian (g/100g)</div><div><b>—</b></div></div>'
    )
    a_pct = (nutri_counts.get("A", 0) / max(1, len(df)) * 100.0) if not nutri_counts.empty else 0.0
    parts.append(f'<div class="card"><div class="small">Nutri-Score A (%)</div><div><b>{a_pct:.1f}</b></div></div>')

    last_mod = pd.to_numeric(df.get("last_modified_t"), errors="coerce")
    if last_mod.notna().any():
        import datetime as dt

        last_date = dt.datetime.utcfromtimestamp(int(last_mod.max())).strftime("%Y-%m-%d %H:%M UTC")
    else:
        last_date = "—"
    parts.append(f'<div class="card"><div class="small">Dernière modif produit</div><div><b>{last_date}</b></div></div>')
    parts.append("</div>")

    if fig_nutri is not None:
        parts.append(fig_nutri.to_html(full_html=False, include_plotlyjs="cdn"))

    if fig_brands is not None:
        parts.append(fig_brands.to_html(full_html=False, include_plotlyjs=False))

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
    table = df[cols].head(200).copy()
    parts.append("<h2>Extrait (200)</h2>")
    parts.append(table.to_html(index=False, escape=True))

    parts.append("</body></html>")

    (output_dir / "index.html").write_text("\n".join(parts), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build static HTML report from local OFF cache")
    ap.add_argument("--out", default="docs", help="output directory (for GitHub Pages)")
    ap.add_argument("--limit", type=int, default=200_000, help="max products loaded from cache")
    args = ap.parse_args()

    build_report(Path(args.out), limit=args.limit)
    print(f"Wrote {Path(args.out) / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
