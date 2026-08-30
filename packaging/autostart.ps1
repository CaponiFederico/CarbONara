
#Amministratore
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -TargetPath "...\EcoMonitor.exe" -UserId "DOMINIO\utente"
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Unregister
#
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Unregister,
    [string]$TargetPath,
    [string]$UserId
)

$ErrorActionPreference = "Stop"
$TaskName = "EcoMonitor"

$isElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isElevated) { throw "Serve un processo elevato" }

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

# UAC puo' elevare con un account amministratore diverso 
if (-not $UserId) { $UserId = "$env:USERDOMAIN\$env:USERNAME" }

$action = New-ScheduledTaskAction -Execute $TargetPath
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
