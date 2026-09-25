
#una sola finestra di UAC invece di una per compito.
#
#   powershell -ExecutionPolicy Bypass -File elevated_setup.ps1 -InstallPawnIO -PawnIOSetup "...\PawnIO_setup.exe"
#   powershell -ExecutionPolicy Bypass -File elevated_setup.ps1 -RegisterAutostart -AutostartScript "...\autostart.ps1" -TargetPath "...\EcoMonitor.exe" -UserId "DOMINIO\utente" [-AutostartArguments "--background"]
#
#Codice di uscita, a bit: 1 PawnIO non installato, 2 avvio automatico non
#registrato, 4 processo non elevato. 0 e' andato tutto bene
[CmdletBinding()]
param(
    [switch]$InstallPawnIO,
    [string]$PawnIOSetup,
    [switch]$RegisterAutostart,
    [string]$AutostartScript,
    [string]$TargetPath,
    [string]$UserId,
    [string]$AutostartArguments
)

$ErrorActionPreference = "Stop"

$PawnIOVersion = "2.2.0"
$PawnIOSha256 = "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032"
$PawnIOWingetId = "namazso.PawnIO"

$PawnIOFailed = 1
$AutostartFailed = 2
$NotElevated = 4

function Test-PawnIO {
    Test-Path "HKLM:\SYSTEM\CurrentControlSet\Services\PawnIO"
}

function Install-PawnIOFromFile {
    if (-not $PawnIOSetup -or -not (Test-Path $PawnIOSetup)) {
        Write-Host "PawnIO: nessun file scaricato"
        return $false
    }
    $hash = (Get-FileHash -Path $PawnIOSetup -Algorithm SHA256).Hash
    if ($hash -ne $PawnIOSha256) {
        Write-Warning "PawnIO: impronta diversa da quella attesa, file non eseguito"
        return $false
    }
    Start-Process -FilePath $PawnIOSetup -ArgumentList "-install", "-silent" -Wait | Out-Null
    return (Test-PawnIO)
}

function Install-PawnIOFromWinget {
    #ripiego: stesso file, stessa impronta, verificati da winget
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "PawnIO: winget non disponibile"
        return $false
    }
    & $winget.Source install --exact --id $PawnIOWingetId --version $PawnIOVersion --silent `
        --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
    return (Test-PawnIO)
}

#Prima di tutto: se non c'e' niente da fare non serve nemmeno essere elevati
$needsPawnIO = $InstallPawnIO -and -not (Test-PawnIO)
if (-not $needsPawnIO -and -not $RegisterAutostart) {
    Write-Host "Niente da fare."
    exit 0
}

$isElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isElevated) {
    Write-Warning "Serve un processo elevato"
    exit $NotElevated
}

$exitCode = 0

if ($needsPawnIO) {
    $installed = $false
    #due tentativi separati: un errore nel primo non deve saltare il ripiego
    try {
        $installed = Install-PawnIOFromFile
    } catch {
        Write-Warning "PawnIO dal file: $_"
    }
    if (-not $installed) {
        try {
            $installed = Install-PawnIOFromWinget
        } catch {
            Write-Warning "PawnIO da winget: $_"
        }
    }
    if ($installed) {
        Write-Host "PawnIO installato."
    } else {
        $exitCode = $exitCode -bor $PawnIOFailed
    }
}

if ($RegisterAutostart) {
    try {
        $autostart = @{ Register = $true; TargetPath = $TargetPath; UserId = $UserId }
        if ($AutostartArguments) { $autostart.Arguments = $AutostartArguments }
        & $AutostartScript @autostart
    } catch {
        Write-Warning "Avvio automatico: $_"
        $exitCode = $exitCode -bor $AutostartFailed
    }
}

exit $exitCode
