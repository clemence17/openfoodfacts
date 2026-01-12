# OpenFoodFacts – Cache local + Streamlit (Windows)

Ce projet maintient un **cache local** de produits OpenFoodFacts dans un fichier **SQLite**, puis affiche des **données + calculs** dans une app **Streamlit**.

## 1) Installer

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) Mettre à jour le cache

Télécharge les produits les plus récemment modifiés (paramétrable) et fait un *upsert* dans SQLite.

```powershell
python -m off_cache.update --country fr --recent-pages 3 --page-size 200
```

### Si erreur SSL (CERTIFICATE_VERIFY_FAILED)

Dans certains environnements (proxy/antivirus d’entreprise), Python ne fait pas confiance au certificat intercepté.

Options possibles:
- Recommandé: fournir un CA bundle (certificat racine d’entreprise au format PEM):

```powershell
python -m off_cache.update --ca-bundle C:\chemin\corp-ca.pem --country fr --recent-pages 3 --page-size 200
```

- Dépannage rapide (moins sûr): désactiver la vérification SSL:

```powershell
python -m off_cache.update --insecure --country fr --recent-pages 3 --page-size 200
```

Le cache est écrit dans `data/off_cache.sqlite`.

## 3) Lancer le site (Streamlit)

```powershell
./.venv/Scripts/python.exe -m streamlit run app.py
```

Si le port 8501 est déjà utilisé, spécifie un autre port:

```powershell
./.venv/Scripts/python.exe -m streamlit run app.py --server.port 8502
```

## Mise en ligne gratuite (mensuelle) via GitHub Pages

Si une mise à jour **1×/mois** te suffit, le plus simple/gratuit est:

- GitHub Actions met à jour le cache + génère un rapport **HTML statique** dans `docs/`
- GitHub Pages publie `docs/index.html`

Ce mode évite d'héberger un serveur Python en continu.

### Étapes

1) Pousser le repo sur GitHub
2) Activer GitHub Pages: Settings → Pages → **Deploy from a branch** → Branch `main` (ou `master`) / folder `docs`
3) Lancer le workflow une première fois (Actions → **Monthly OFF report** → Run workflow)

Le workflow est dans `.github/workflows/monthly_pages_report.yml` et tourne ensuite automatiquement chaque mois.

## 4) Mise à jour régulière (Task Scheduler)

Idée simple: créer une tâche Windows qui exécute (dans le dossier du projet) :

```powershell
.\.venv\Scripts\python.exe -m off_cache.update --country fr --recent-pages 3 --page-size 200
```

Notes:
- Par défaut on synchronise seulement les **pages récentes** triées par date de modification. C’est léger et suffisant pour un cache qui se “rafraîchit” régulièrement.
- Si tu veux un cache plus complet, augmente `--recent-pages` et `--page-size`.

## Licence / ODbL

OpenFoodFacts est sous ODbL: évite de mélanger ces données avec d’autres sources propriétaires sans vérifier les obligations (attribution / partage à l’identique).
