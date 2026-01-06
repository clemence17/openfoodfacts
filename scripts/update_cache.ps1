Param(
  [string]$Country = "fr",
  [int]$RecentPages = 3,
  [int]$PageSize = 200
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Virtualenv not found at .venv. Create it: python -m venv .venv ; pip install -r requirements.txt"
}

& $py -m off_cache.update --country $Country --recent-pages $RecentPages --page-size $PageSize
