param(
    [switch]$Check,
    [switch]$SmokeTest,
    [switch]$Models,
    [string]$Model,
    [switch]$Backup
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$venvPython = Join-Path $projectRoot '.venv-local\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Run deploy/setup.ps1 first to install .venv-local.'
}
$launchArgs = @('-X', 'utf8', '-m', 'app.local')
if ($Check) { $launchArgs += '--check' }
if ($SmokeTest) { $launchArgs += '--smoke-test' }
if ($Models) { $launchArgs += '--models' }
if ($Model) { $launchArgs += @('--select-model', $Model) }
if ($Backup) { $launchArgs += '--backup' }
& $venvPython @launchArgs
exit $LASTEXITCODE
