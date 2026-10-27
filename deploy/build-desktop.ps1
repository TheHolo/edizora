param(
    [string]$Compiler = '',
    [string]$Version = '0.1',
    [switch]$BundleOnly
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $taskPython)) { throw 'Prepare the build environment with uv sync --extra news-worker --extra desktop --extra desktop-build.' }
if (!(Test-Path 'frontend\dist\index.html')) { throw 'Build frontend before packaging.' }
New-Item -ItemType Directory -Force -Path 'work\desktop-temp' | Out-Null
$env:TEMP = Join-Path $taskRoot 'work\desktop-temp'
$env:TMP = $env:TEMP
& $taskPython 'deploy\prepare-desktop.py' --version $Version
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare package metadata.' }
& $taskPython -m PyInstaller --noconfirm --log-level WARN --distpath 'output\desktop\bundle' --workpath 'work\pyinstaller' 'deploy\edizora.spec'
if ($LASTEXITCODE -ne 0) { throw 'Desktop bundle failed.' }
if (!$BundleOnly) {
    if (!$Compiler) {
        $taskCandidates = @('work\build-tools\inno\ISCC.exe', "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
        $Compiler = $taskCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (!$Compiler) { throw 'Inno Setup compiler is required to build Setup.exe. Pass -Compiler with its path.' }
    & $Compiler '/Q' "/DAppVersion=$Version" 'deploy\edizora.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer failed.' }
    Get-ChildItem 'output\desktop\edizora.exe' | Get-FileHash -Algorithm SHA256 | ForEach-Object {
        ($_.Hash.ToLowerInvariant() + '  ' + (Split-Path $_.Path -Leaf)) | Set-Content -Encoding ascii ($_.Path + '.sha256')
    }
}
