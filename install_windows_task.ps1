<#
.SYNOPSIS
  Registers (or removes) a Windows Task Scheduler task that keeps the upload worker running.

.DESCRIPTION
  Default: starts when YOU sign in to Windows (no admin rights needed).
  -AtStartup: starts when the PC boots, even before anyone signs in. Requires an elevated
              PowerShell and asks for your Windows password in a Windows credential dialog,
              because the encrypted YouTube token (DPAPI) can only be read with your credentials.
  The task runs worker_supervisor.py with the project's .venv pythonw.exe (no console window),
  which restarts the worker if it crashes; Task Scheduler also restarts the task on failure.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install_windows_task.ps1
  powershell -ExecutionPolicy Bypass -File .\install_windows_task.ps1 -AtStartup
  powershell -ExecutionPolicy Bypass -File .\install_windows_task.ps1 -Uninstall
#>
param(
    [switch]$AtStartup,
    [switch]$Uninstall,
    [string]$TaskName = "ShortsAutopilotWorker"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonW = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Supervisor = Join-Path $ProjectDir "worker_supervisor.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "Task '$TaskName' is not installed."
    }
    exit 0
}

if (-not (Test-Path $PythonW)) {
    Write-Error "Virtual environment not found at $PythonW. Run setup.bat first."
}

$action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Supervisor`"" -WorkingDirectory $ProjectDir
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$user = "$env:USERDOMAIN\$env:USERNAME"

if ($AtStartup) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "-AtStartup needs an elevated PowerShell (Run as administrator)."
    }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT1M"  # give the network a minute to come up
    $cred = Get-Credential -UserName $user -Message "Windows password for $user (stored by Task Scheduler, needed to read the encrypted YouTube token)"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited `
        -Description "Shorts Autopilot upload worker ($ProjectDir)" -Force | Out-Null
    Write-Host "Registered '$TaskName' to start at boot (runs whether or not you are signed in)."
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principalObj = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principalObj -Description "Shorts Autopilot upload worker ($ProjectDir)" -Force | Out-Null
    Write-Host "Registered '$TaskName' to start when $user signs in."
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Task started. Check the dashboard sidebar for 'Worker online'."
Write-Host "Keep the PC on, awake and online at the upload time (Settings > System > Power: Sleep = Never while plugged in)."
