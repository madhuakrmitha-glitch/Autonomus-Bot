<#
.SYNOPSIS
  Register OMCA to start in the logged-on user's INTERACTIVE session (required for browser/desktop automation).

.DESCRIPTION
  Creates Task Scheduler tasks that run at log on of the current user, only while that user is logged on
  (interactive desktop), restart on failure, and never stop after a time limit. Services and
  "run whether user is logged on or not" tasks run in a non-interactive session where Playwright and
  desktop input automation do not work.

.EXAMPLE
  # single VM (P1): coordinator + in-process worker
  .\scripts\windows\register-tasks.ps1 -AppDir C:\OMCA\app -Mode all-in-one -WorkerId vm1

.EXAMPLE
  # additional VM (P3): worker only
  .\scripts\windows\register-tasks.ps1 -AppDir C:\OMCA\app -Mode worker
#>
param(
  [Parameter(Mandatory = $true)][string]$AppDir,
  [ValidateSet("all-in-one", "coordinator", "worker")][string]$Mode = "all-in-one",
  [string]$WorkerId = "vm1",
  [string]$Uv = "$env:USERPROFILE\.local\bin\uv.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Uv)) { $Uv = (Get-Command uv -ErrorAction Stop).Source }
if (-not (Test-Path (Join-Path $AppDir "pyproject.toml"))) { throw "$AppDir does not look like the OMCA repository" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable -MultipleInstances IgnoreNew

function Register-Omca([string]$Name, [string]$Arguments) {
  $action = New-ScheduledTaskAction -Execute $Uv -Argument $Arguments -WorkingDirectory $AppDir
  Register-ScheduledTask -TaskName $Name -TaskPath "\OMCA\" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
  Write-Host "registered \OMCA\$Name -> uv $Arguments"
}

switch ($Mode) {
  "all-in-one" { Register-Omca "Coordinator" "run omca run --with-worker $WorkerId" }
  "coordinator" { Register-Omca "Coordinator" "run omca run" }
  "worker" { Register-Omca "Worker" "run omca worker --config config\worker.yaml" }
}

Write-Host ""
Write-Host "Keep this VM awake (power plan: never sleep). Do not lock or close an RDP session: use the console,"
Write-Host "VNC, or 'tscon <session id> /dest:console' so input automation keeps a live desktop."
Write-Host "Start now with: Start-ScheduledTask -TaskPath '\OMCA\' -TaskName '$(if ($Mode -eq 'worker') {'Worker'} else {'Coordinator'})'"
