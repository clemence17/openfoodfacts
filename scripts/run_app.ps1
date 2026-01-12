Param(
  [int]$StartPort = 8501,
  [int]$MaxTries = 20
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Virtualenv not found at .venv. Create it: python -m venv .venv ; pip install -r requirements.txt"
}

function Test-PortFree([int]$Port) {
  $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  return -not [bool]$c
}

$port = $StartPort
for ($i = 0; $i -lt $MaxTries; $i++) {
  if (Test-PortFree -Port $port) { break }
  $port++
}

if (-not (Test-PortFree -Port $port)) {
  throw "No free port found in range $StartPort..$($StartPort + $MaxTries - 1)."
}

Write-Host "Starting Streamlit on http://localhost:$port" -ForegroundColor Cyan
& $py -m streamlit run app.py --server.port $port
