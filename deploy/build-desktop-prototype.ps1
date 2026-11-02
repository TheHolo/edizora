param(
    [string]$Compiler = '',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = if ($Python) { $Python } else { Join-Path $taskRoot '.venv\Scripts\python.exe' }
if (!(Test-Path -LiteralPath $taskPython)) { throw 'The Python build environment is missing.' }
if (!$Compiler) { $Compiler = Join-Path $taskRoot 'work\build-tools\inno\ISCC.exe' }
if (!(Test-Path -LiteralPath $Compiler)) { throw 'Supply the Inno Setup compiler path with -Compiler.' }
$buildId = [guid]::NewGuid().ToString('N')
$buildRoot = Join-Path $taskRoot "output\desktop-prototype\$buildId"
$payload = Join-Path $buildRoot 'payload'
$env:CARGO_TARGET_DIR = Join-Path $taskRoot 'work\tauri-target'
$env:TEMP = Join-Path $taskRoot 'work\desktop-temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
Push-Location frontend
try { npm run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } }
finally { Pop-Location }
# Keep module discovery away from any checkout-local .env or bot settings.
$taskBuildCwd = Join-Path $buildRoot 'isolated-build-cwd'
New-Item -ItemType Directory -Path $taskBuildCwd | Out-Null
$taskOldPythonPath = $env:PYTHONPATH
$taskPrivateEnvironment = @{}
Get-ChildItem Env: | Where-Object {
    $_.Name -match '^(PUBLISHER_|EDIZORA_)|TOKEN|SECRET|PASSWORD|API_KEY|PIXIV_COOKIES|PROXY|^(DATABASE_URL|STORAGE_PATH|ADMIN_IDS|CHANNELS_JSON)$'
} | ForEach-Object {
    $taskPrivateEnvironment[$_.Name] = $_.Value
    Remove-Item -LiteralPath ('Env:' + $_.Name)
}
try {
    $env:PYTHONPATH = $taskRoot
    Push-Location $taskBuildCwd
    try {
        & $taskPython (Join-Path $taskRoot 'deploy/prepare-desktop.py') --version 0.1
        if ($LASTEXITCODE -ne 0) { throw 'Metadata generation failed.' }
        & $taskPython -m PyInstaller --noconfirm --log-level WARN --distpath "$buildRoot\sidecar" --workpath (Join-Path $taskRoot 'work/pyinstaller-sidecar') (Join-Path $taskRoot 'deploy/edizora-sidecar.spec')
        if ($LASTEXITCODE -ne 0) { throw 'Backend build failed.' }
    } finally { Pop-Location }
} finally {
    if ($null -eq $taskOldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $taskOldPythonPath }
    foreach ($taskName in $taskPrivateEnvironment.Keys) {
        Set-Item -LiteralPath ('Env:' + $taskName) -Value $taskPrivateEnvironment[$taskName]
    }
}
cargo build --release --locked --manifest-path frontend/src-tauri/Cargo.toml
if ($LASTEXITCODE -ne 0) { throw 'Shell build failed.' }
New-Item -ItemType Directory -Force -Path "$payload\backend" | Out-Null
Copy-Item -LiteralPath "$env:CARGO_TARGET_DIR\release\edizora-desktop.exe" -Destination "$payload\edizora-prototype.exe"
Copy-Item -Path "$buildRoot\sidecar\edizora-backend\*" -Destination "$payload\backend" -Recurse
& $Compiler '/Q' "/DPayloadDir=$payload" "/DOutputRoot=$buildRoot" 'deploy\edizora-prototype.iss'
if ($LASTEXITCODE -ne 0) { throw 'Prototype installer build failed.' }
Get-FileHash -LiteralPath "$buildRoot\edizora-prototype-setup.exe" -Algorithm SHA256
Write-Output "Prototype build: $buildRoot"
