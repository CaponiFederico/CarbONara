
#Amministratore
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -TargetPath "...\CarbONara.exe" -UserId "DOMINIO\utente"
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -TargetPath "..." -UserId "..." -Arguments "--background"
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Unregister -UserId "DOMINIO\utente"
#
#Con -Launcher la stessa cosa per l'attivita' di lancio
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Register -Launcher -TargetPath "..." -UserId "..."
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Unregister -UnlessTargetPath "...\CarbONara.exe" -UserId "..."
#Con -LegacyTargetPath un'attivita' sull'eseguibile di prima viene spostata invece che tolta
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Unregister -UnlessTargetPath "...\CarbONara.exe" -LegacyTargetPath "...\EcoMonitor.exe" -UserId "..."
#
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Unregister,
    [switch]$Launcher,
    [string]$TargetPath,
    [string]$UnlessTargetPath,
    [string]$LegacyTargetPath,
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
$LauncherTaskName = "EcoMonitor avvio ($($UserId.Replace('\', '-')))"
if ($Launcher) { $TaskName = $LauncherTaskName }

function Get-FullPath([string]$Path) {
    [System.IO.Path]::GetFullPath($Path.Trim().Trim('"'))
}

if ($Unregister) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Nessuna attivita' $TaskName da rimuovere."
        exit 0
    }
    if ($UnlessTargetPath) {
        $registered = @($task.Actions | Where-Object { $_.Execute } | ForEach-Object { Get-FullPath $_.Execute })
        if ($registered -contains (Get-FullPath $UnlessTargetPath)) {
            Write-Host "Attivita' $TaskName gia' su questo eseguibile: resta."
            exit 0
        }
        if ($LegacyTargetPath -and ($registered -contains (Get-FullPath $LegacyTargetPath))) {
            $Arguments = @($task.Actions | Where-Object { $_.Execute })[0].Arguments
            $TargetPath = $UnlessTargetPath
            $Register = $true
        }
    }
    if (-not $Register) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Attivita' $TaskName rimossa."
        exit 0
    }
    Write-Host "Attivita' $TaskName spostata su $TargetPath."
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
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest

if ($Launcher) {
    #un secondo avvio mentre l'app gira bussa alla sua pipe ed esce: niente da ignorare
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances Parallel
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Principal $principal -Settings $settings -Force `
        -Description "Avvia CarbO'Nara con i sensori leggibili, senza UAC" | Out-Null
    Write-Host "Attivita' di lancio registrata per $UserId."
    exit 0
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Avvia CarbO'Nara all'accesso" | Out-Null

Write-Host "Avvio automatico registrato per $UserId."
