param([switch]$Dev)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$venvPython = Join-Path $projectRoot '.venv-local\Scripts\python.exe'

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.venv-local'
        $syncArgs = @('sync', '--locked', '--extra', 'news-worker', '--cache-dir', '.cache/uv')
        if ($Dev) { $syncArgs += @('--extra', 'dev') }
        & uv @syncArgs
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. See the error above.' }
    } finally {
        $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    }
} else {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (Get-Command py -ErrorAction SilentlyContinue) { $pythonCommand = 'py' }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { $pythonCommand = 'python' }
        else { throw 'Install Python 3.12+ from python.org (with PATH), then run setup again.' }
        & $pythonCommand -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
        & $pythonCommand -m venv .venv-local
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv-local.' }
    }
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Could not install pip in .venv-local.' }
    $extras = '.[news-worker]'
    if ($Dev) { $extras = '.[news-worker,dev]' }
    & $venvPython -m pip install --upgrade -e $extras
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination (Join-Path $projectRoot '.env')
    Write-Host 'Created .env. Fill BOT_TOKEN, ADMIN_IDS, CHANNELS_JSON and HOME_WORKER_LMSTUDIO_MODEL.'
} else {
    Write-Host 'Existing .env preserved. See README for LM Studio settings.'
}
Write-Host 'Dependencies installed. Start the LM Studio server, then run:'
Write-Host '  .\deploy\run-local.ps1 -Check'
Write-Host '  .\deploy\run-local.ps1 -SmokeTest'
Write-Host '  .\start-local.cmd'
