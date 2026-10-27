param(
    [Parameter(Mandatory=$true)][string]$ReleaseArchive,
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [Parameter(Mandatory=$true)][guid]$SystemId,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Edizora')
)
$ErrorActionPreference = 'Stop'
$archive = (Resolve-Path -LiteralPath $ReleaseArchive).Path
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $ExpectedSha256) { throw 'Release SHA-256 mismatch.' }
$resolvedRoot = [IO.Path]::GetFullPath($InstallRoot)
if ($resolvedRoot.StartsWith('\\') -or $resolvedRoot -match '(?i)\\OneDrive(?:\\|$)') { throw 'Choose a local folder outside OneDrive or a network share.' }
if ((Test-Path -LiteralPath $resolvedRoot) -and (Get-ChildItem -LiteralPath $resolvedRoot -Force | Select-Object -First 1) -and -not (Test-Path -LiteralPath (Join-Path $resolvedRoot '.publisher-installation'))) { throw 'Choose an empty dedicated application folder. Existing unrelated files will not be changed.' }
New-Item -ItemType Directory -Path $resolvedRoot -Force | Out-Null
'Edizora managed installation v1' | Set-Content -LiteralPath (Join-Path $resolvedRoot '.publisher-installation') -Encoding ASCII
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $resolvedRoot '/inheritance:r' '/grant:r' ('*' + $currentSid + ':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to make the installation directory private to this Windows user.' }
$stage = Join-Path $resolvedRoot ('releases\.install-' + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$bundle = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    $seen = @{}
    [long]$expanded = 0
    foreach ($entry in $bundle.Entries) {
        $key = $entry.FullName
        if ($seen.ContainsKey($key.ToLowerInvariant())) { throw 'Duplicate release entry.' }
        $seen[$key.ToLowerInvariant()] = $true
        $expanded += $entry.Length
        if ($expanded -gt 2GB) { throw 'Release exceeds extraction limit.' }
        if ($key.Contains('\') -or $key.Contains(':') -or $key.StartsWith('/') -or ($key.Split('/') -contains '..')) { throw 'Unsafe release path.' }
        if ((($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Release symlinks are forbidden.' }
        $target = [IO.Path]::GetFullPath((Join-Path $stage $key))
        if (-not $target.StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Release path escapes staging.' }
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
    }
} finally { $bundle.Dispose() }
$manifest = Get-Content -LiteralPath (Join-Path $stage 'release-manifest.json') -Raw | ConvertFrom-Json
if ($manifest.format -ne 'publisher-release-v1' -or $manifest.release_id -notmatch '^(edizora|publisher)-[0-9a-f]{16}$') { throw 'Invalid release manifest.' }
if ($seen.Count -ne ($manifest.files.Count + 1)) { throw 'Unexpected release files.' }
foreach ($record in $manifest.files) {
    $path = Join-Path $stage $record.key
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne $record.size -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $record.sha256) { throw ('Release verification failed: ' + $record.key) }
}
$release = Join-Path $resolvedRoot ('releases\' + $manifest.release_id)
$installedConfiguration = Join-Path $resolvedRoot 'installation.json'
if (Test-Path -LiteralPath $installedConfiguration) {
    $previous = Get-Content -LiteralPath $installedConfiguration -Raw | ConvertFrom-Json
    if ($previous.release -ne $release) { throw 'Another release is current. Perform the documented stopped upgrade before changing it.' }
}
if (Test-Path -LiteralPath $release) {
    foreach ($record in $manifest.files) {
        $existing = Join-Path $release $record.key
        if (-not (Test-Path -LiteralPath $existing -PathType Leaf) -or (Get-FileHash -LiteralPath $existing -Algorithm SHA256).Hash -ne $record.sha256) { throw 'Existing release differs; refusing overwrite.' }
    }
    Write-Output 'The same verified release is already installed.'
} else { Move-Item -LiteralPath $stage -Destination $release }
$venv = Join-Path $resolvedRoot ('venvs\' + $manifest.release_id)
& py -3.12 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 with the Python launcher, then retry.' }
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install 'uv==0.11.30'
if ($LASTEXITCODE -ne 0) { throw 'Unable to install pinned dependency manager.' }
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = $venv
    & (Join-Path $venv 'Scripts\uv.exe') sync --directory $release --frozen --no-dev --no-editable --python $python
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
} finally { $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment }
$control = Join-Path $resolvedRoot 'control'
$data = Join-Path $resolvedRoot 'data'
New-Item -ItemType Directory -Path $control,$data -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $control 'node.json'))) {
    Push-Location -LiteralPath $release
    try {
        & $python -m app.deployment.cli --control-dir $control initialize --system-id $SystemId.ToString() --data-root $data --release-id $manifest.release_id
        if ($LASTEXITCODE -ne 0) { throw 'Node identity setup failed.' }
    } finally { Pop-Location }
}
$installation = @{ release=$release; python=$python; control=$control; data=$data; frontend=(Join-Path $release 'frontend\dist') }
$archiveDirectory = Join-Path $resolvedRoot 'archives'
New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
$retainedArchive = Join-Path $archiveDirectory ($manifest.release_id + '.zip')
if (Test-Path -LiteralPath $retainedArchive) {
    if ((Get-FileHash -LiteralPath $retainedArchive -Algorithm SHA256).Hash -ne $ExpectedSha256) { throw 'Stored release archive differs; refusing overwrite.' }
} else { Copy-Item -LiteralPath $archive -Destination $retainedArchive }
& $python -m app.deployment.cli --control-dir $control register-release --archive $retainedArchive
if ($LASTEXITCODE -ne 0) { throw 'Unable to register verified release archive.' }
$installation | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resolvedRoot 'installation.json') -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $release 'deploy\manage-web-windows.ps1') -Destination (Join-Path $resolvedRoot 'manage.ps1')
Write-Output ('Installed standby host: ' + $resolvedRoot)
Write-Output 'Start: powershell -File <install-root>\manage.ps1 -Action Start'
Write-Output 'Working data was not moved and publication authority was not granted.'
