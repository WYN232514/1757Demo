<#
.SYNOPSIS
    One-command refresh after replacing the work-order XLS file.

.EXAMPLE
    .\refresh_from_work_order.ps1
    .\refresh_from_work_order.ps1 -Full
    .\refresh_from_work_order.ps1 -SyncQdrant
#>

param(
    [switch]$Full,
    [switch]$SyncQdrant,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
$env:PYTHONIOENCODING = 'utf-8'

$Python = (Get-Command python -ErrorAction Stop).Source
# Build the Chinese base name from Unicode code points so Windows PowerShell 5
# can parse this UTF-8-without-BOM script reliably.
$WorkOrderBase = -join ([char[]](0x5DE5, 0x5355))
$WorkOrderXls = Join-Path $ScriptDir ($WorkOrderBase + '.xls')
$WorkOrderXlsx = Join-Path $ScriptDir ($WorkOrderBase + '.xlsx')
$MainScript = Join-Path $ScriptDir 'vcb_case_library_direct.py'
$ExtractorDir = Join-Path $ScriptDir 'fault_extractor\fault_extractor'
$ExtractorDemo = Join-Path $ExtractorDir 'demo_parse.py'
$CoverageCheck = Join-Path $ExtractorDir 'verify_reference_coverage.py'
$ReferenceOutput = Join-Path $ExtractorDir 'output\reference_data_with_cause_inferenced.json'

function Invoke-PythonStep {
    param(
        [string]$Title,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ScriptDir
    )
    Write-Host ''
    Write-Host "==== $Title ====" -ForegroundColor Cyan
    Push-Location $WorkingDirectory
    try {
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Title failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $WorkOrderXls)) {
    throw "Input file not found: $WorkOrderXls"
}

Write-Host "Input: $WorkOrderXls" -ForegroundColor Green
if ($Full) {
    Write-Host 'Mode: full rebuild' -ForegroundColor Green
    Invoke-PythonStep -Title '1/5 Rebuild case library' -Arguments @(
        $MainScript, 'extract-build', '--work-order-xlsx', $WorkOrderXls
    )
    Invoke-PythonStep -Title '2/5 Back up case library' -Arguments @($MainScript, 'backup')
}
else {
    Write-Host 'Mode: incremental update' -ForegroundColor Green
    $updateArgs = @(
        $MainScript, 'update-from-xlsx',
        '--work-order-xlsx', $WorkOrderXls,
        '--backup'
    )
    if (-not $SyncQdrant) {
        $updateArgs += '--no-qdrant'
    }
    Invoke-PythonStep -Title '1/5 Update case library' -Arguments $updateArgs
}

if ($Full -and $SyncQdrant) {
    Invoke-PythonStep -Title 'Sync case library to Qdrant' -Arguments @($MainScript, 'sync-qdrant')
}

if (-not (Test-Path -LiteralPath $WorkOrderXlsx)) {
    throw "Converted XLSX not found: $WorkOrderXlsx"
}

$parseArgs = @(
    $ExtractorDemo,
    '--input', $WorkOrderXlsx,
    '--output', $ReferenceOutput
)
if ($Full) {
    $parseArgs += '--regenerate-all'
}
Invoke-PythonStep -Title '3/5 Update structured work-order data' `
    -Arguments $parseArgs -WorkingDirectory $ExtractorDir

Invoke-PythonStep -Title '4/5 Verify structured work-order coverage' `
    -Arguments @($CoverageCheck, $WorkOrderXlsx, $ReferenceOutput) `
    -WorkingDirectory $ExtractorDir

if (-not $NoRestart) {
    Write-Host ''
    Write-Host '==== 5/5 Restart Dash ====' -ForegroundColor Cyan
    $listeners = Get-NetTCPConnection -LocalPort 8050 -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in @($listeners)) {
        Stop-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    }

    $stdout = Join-Path $ScriptDir 'scratch_app_out.log'
    $stderr = Join-Path $ScriptDir 'scratch_app_err.log'
    Start-Process -FilePath $Python -ArgumentList 'app_visualize.py' `
        -WorkingDirectory $ScriptDir -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $ready = Get-NetTCPConnection -LocalPort 8050 -State Listen -ErrorAction SilentlyContinue
    } until ($ready -or (Get-Date) -gt $deadline)

    if (-not $ready) {
        if (Test-Path -LiteralPath $stderr) {
            Get-Content -LiteralPath $stderr -Tail 80
        }
        throw 'Dash did not start within 30 seconds.'
    }
    Write-Host 'Dash is ready: http://127.0.0.1:8050' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Refresh completed.' -ForegroundColor Green
Write-Host 'New work orders:       .\refresh_from_work_order.ps1'
Write-Host 'Changed existing data: .\refresh_from_work_order.ps1 -Full'
