
#Amministratore
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -TargetPath "...\EcoMonitor.exe" -UserId "DOMINIO\utente"
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -TargetPath "..." -UserId "..." -Arguments "--background"
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Unregister -UserId "DOMINIO\utente"
#
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Unregister,
    [string]$TargetPath,
    [string]$UserId,
    [string]$Arguments
)

$ErrorActionPreference = "Stop"

$isElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isElevated) { throw "Serve un processo elevato" }

# UAC puo' elevare con un account amministratore diverso
if (-not $UserId) { $UserId = "$env:USERDOMAIN\$env:USERNAME" }

#un'attivita' per utente
$TaskName = "EcoMonitor ($($UserId.Replace('\', '-')))"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Avvio automatico rimosso."
    } else {
        Write-Host "Nessun avvio automatico da rimuovere."
    }
    exit 0
}

if (-not $Register) { throw "Indicare -Register oppure -Unregister." }
if (-not $TargetPath) { throw "-Register richiede -TargetPath." }
if (-not (Test-Path $TargetPath)) { throw "Non trovo l'eseguibile: $TargetPath" }

#-Argument con una stringa vuota registrerebbe un argomento vuoto invece di nessuno
if ($Arguments) {
    $action = New-ScheduledTaskAction -Execute $TargetPath -Argument $Arguments
} else {
    $action = New-ScheduledTaskAction -Execute $TargetPath
}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Avvia EcoMonitor all'accesso" | Out-Null

Write-Host "Avvio automatico registrato per $UserId."
