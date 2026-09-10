$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$archivePath = Join-Path $projectDir "01_data_portable.tar.gz"
$dataDir = Join-Path $projectDir "01_数据整理"

if (-not (Test-Path -LiteralPath $archivePath)) {
    throw "Missing portable data archive: $archivePath. Run 'git lfs pull' first."
}

if (Test-Path -LiteralPath $dataDir) {
    Write-Host "Data directory already exists: $dataDir"
    Write-Host "Remove or rename it before restoring if you need a clean copy."
    exit 0
}

tar.exe -xzf $archivePath -C $projectDir
if ($LASTEXITCODE -ne 0) {
    throw "Failed to extract $archivePath"
}

Write-Host "Portable data restored to: $dataDir"
