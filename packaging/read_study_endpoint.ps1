# Equivalente Windows di read_study_endpoint.sh
#
#   powershell -ExecutionPolicy Bypass -File packaging\read_study_endpoint.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\read_study_endpoint.ps1 -OutFile dist\EcoMonitor\study_endpoint
#
# Senza -OutFile l'indirizzo finisce sullo standard output

[CmdletBinding()]
param(
    [string]$OutFile
)

$ErrorActionPreference = "Stop"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $PackagingDir
$EndpointFile = Join-Path $ProjectRoot ".study_endpoint"

$endpoint = $env:ECOMONITOR_STUDY_ENDPOINT
if (-not $endpoint -and (Test-Path $EndpointFile)) {
    $endpoint = (Get-Content $EndpointFile -Raw) -replace '\s', ''
}

if (-not $endpoint) {
    throw "nessun indirizzo del server dello studio: metterlo in $EndpointFile oppure in ECOMONITOR_STUDY_ENDPOINT."
}

# il token viaggia nell'intestazione
if (-not $endpoint.StartsWith("https://")) {
    throw "l'indirizzo del server dello studio deve iniziare con https://"
}
$endpoint = $endpoint.TrimEnd('/')

if (-not $OutFile) {
    Write-Host -NoNewline $endpoint
    exit 0
}

$directory = Split-Path -Parent $OutFile
if ($directory -and -not (Test-Path $directory)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
# senza BOM, come la chiave
[System.IO.File]::WriteAllText($OutFile, $endpoint, (New-Object System.Text.UTF8Encoding($false)))
