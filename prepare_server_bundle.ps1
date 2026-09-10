param(
    [string]$Output = "..\1757Demo_server.tar.gz"
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ArchivePath = [IO.Path]::GetFullPath((Join-Path $ProjectDir $Output))

Push-Location $ProjectDir
try {
    # Use an allow-list so large local data/cache directories are never traversed.
    $AllowedDirectories = @(
        'assets',
        'case_library',
        'deploy',
        'fault_extractor',
        'knowledge_graph'
    )
    $TopLevelFiles = @(Get-ChildItem -LiteralPath $ProjectDir -File -Force | Where-Object {
        $_.Name -notlike '.env*' -and
        $_.Name -ne 'config.yaml' -and
        $_.Name -ne '批量下载.py' -and
        $_.Extension -notin @('.pem', '.ppk', '.key') -and
        $_.Extension -notin @('.log', '.pid') -and
        $_.Name -notlike '*.sqlite3*'
    } | ForEach-Object { $_.Name })
    $BundleItems = @($TopLevelFiles + $AllowedDirectories)

    if (Test-Path -LiteralPath $ArchivePath) {
        Remove-Item -LiteralPath $ArchivePath -Force
    }
    & tar.exe -czf $ArchivePath @BundleItems
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Server bundle created: $ArchivePath" -ForegroundColor Green
Write-Host 'Note: .env, config.yaml and 批量下载.py are excluded.' -ForegroundColor Yellow
