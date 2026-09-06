# Equivalente Windows di read_api_key.sh
#
#   powershell -ExecutionPolicy Bypass -File packaging\read_api_key.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\read_api_key.ps1 -OutFile dist\EcoMonitor\api_key
#
# Senza -OutFile la chiave finisce sullo standard output

[CmdletBinding()]
param(
    [string]$OutFile
)

$ErrorActionPreference = "Stop"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $PackagingDir
$EnvFile = Join-Path $ProjectRoot ".env"

$key = $env:ECOMONITOR_EM_API_KEY
if (-not $key -and (Test-Path $EnvFile)) {
    # -Raw perche' senza il file di una riga sola torna un array
    $key = (Get-Content $EnvFile -Raw) -replace '\s', ''
}

if (-not $key) {
    throw "nessuna chiave ElectricityMaps: metterla in $EnvFile oppure in ECOMONITOR_EM_API_KEY."
}

if ($key -match '=') {
    throw "$EnvFile deve contenere solo la chiave, non NOME=valore."
}

if (-not $OutFile) {
    Write-Host -NoNewline $key
    exit 0
}


$directory = Split-Path -Parent $OutFile
if ($directory -and -not (Test-Path $directory)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
[System.IO.File]::WriteAllText($OutFile, $key, (New-Object System.Text.UTF8Encoding($false)))
