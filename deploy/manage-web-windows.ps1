param([ValidateSet('Start','Stop','Restart','Logs','InstallAutostart','RemoveAutostart')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
$configuration = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'installation.json') -Raw | ConvertFrom-Json
$env:PUBLISHER_CONTROL_DIR = $configuration.control
$env:PUBLISHER_DATA_ROOT = $configuration.data
$env:PUBLISHER_DATABASE_PATH = Join-Path $configuration.data 'publisher.sqlite'
$env:PUBLISHER_FRONTEND_DIR = $configuration.frontend
$env:PUBLISHER_HOST = '127.0.0.1'
$env:PUBLISHER_PORT = '8000'
$taskName = 'Publisher Host (' + $env:USERNAME + ')'
switch ($Action) {
    'InstallAutostart' {
        $arguments = '-NoProfile -WindowStyle Hidden -File "' + $PSCommandPath + '" -Action Start'
        $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Force | Out-Null
    }
    'RemoveAutostart' { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
    'Logs' {
        $logs = Join-Path $configuration.control 'logs'
        if (Test-Path -LiteralPath $logs) { Invoke-Item -LiteralPath $logs } else { Write-Output 'No log directory yet.' }
    }
    'Stop' { & $configuration.python -m app.web --stop }
    'Restart' {
        & $configuration.python -m app.web --stop
        if ($LASTEXITCODE -ne 0) { throw 'Host refused managed stop; inspect logs before restarting.' }
        Start-Process -FilePath $configuration.python -ArgumentList @('-m','app.web','--no-browser','--enable-bot') -WorkingDirectory $configuration.release -WindowStyle Hidden
    }
    default {
        Start-Process -FilePath $configuration.python -ArgumentList @('-m','app.web','--enable-bot') -WorkingDirectory $configuration.release -WindowStyle Hidden
    }
}
