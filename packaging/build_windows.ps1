# Costruisce il pacchetto Windows
#
# dalla root di progetto:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# venv di progetto con l'extra [packaging]:
#   .\.venv\Scripts\python.exe -m pip install -e ".[gui,windows,packaging]"
#
# serve anche Inno Setup:
#   winget install --id JRSoftware.InnoSetup -e
#
# Produce:
#   EcoMonitor-<versione>-windows-x64-setup.exe 

[CmdletBinding()]
param(
    # Chiede l'elevazione all'avvio invece di ripiegare sulla stima
    [switch]$RequireAdmin
)

$ErrorActionPreference = "Stop"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $PackagingDir
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Manca il venv di progetto: $Python" }

$pyproject = Get-Content (Join-Path $ProjectRoot "pyproject.toml") -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { throw "Non riesco a leggere la versione da pyproject.toml" }
$Version = $Matches[1]

$isccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
# meglio fermarsi subito che dopo tre minuti di PyInstaller
if (-not $iscc) { throw "Inno Setup non trovato: winget install --id JRSoftware.InnoSetup -e" }

$BuildDir = Join-Path $PackagingDir "build"
$DistDir = Join-Path $PackagingDir "dist"
$OutDir = Join-Path $PackagingDir "out"

if ($RequireAdmin) { $env:ECOMONITOR_UAC_ADMIN = "1" } else { Remove-Item Env:\ECOMONITOR_UAC_ADMIN -ErrorAction SilentlyContinue }

& $Python (Join-Path $PackagingDir "make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "generazione dell'icona fallita" }

& $Python -m PyInstaller --clean --noconfirm --distpath $DistDir --workpath $BuildDir (Join-Path $PackagingDir "ecomonitor.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallito" }

& (Join-Path $DistDir "ecomonitor-cli.exe") --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "l'eseguibile costruito non parte" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
& $iscc "/DAppVersion=$Version" (Join-Path $PackagingDir "ecomonitor.iss") | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Inno Setup ha fallito la compilazione" }

$SetupPath = Join-Path $OutDir "EcoMonitor-$Version-windows-x64-setup.exe"
$setupMb = [math]::Round((Get-Item $SetupPath).Length / 1MB, 1)
Write-Host ""
Write-Host "Setup pronto: $SetupPath ($setupMb MB)"
Write-Host "Per installarlo: doppio clic, la procedura guidata fa il resto."
