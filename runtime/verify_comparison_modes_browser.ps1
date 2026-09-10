$keyPath = 'C:\Users\thinkpad\Desktop\key-mtr-phm-nigix.pem'
$sshArgs = @(
    '-N',
    '-o', 'ExitOnForwardFailure=yes',
    '-L', '18050:127.0.0.1:8050',
    '-i', $keyPath,
    'ec2-user@16.163.66.139'
)
$tunnelProc = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 2
    $chromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
    $profilePath = Join-Path (Resolve-Path 'runtime') ('compare-mode-tunnel-' + (Get-Date -Format 'yyyyMMddHHmmss'))
    New-Item -ItemType Directory -Path $profilePath | Out-Null
    $shotPath = Join-Path (Resolve-Path 'runtime') 'comparison_modes_tunnel.png'
    & $chromePath `
        --headless=new `
        --disable-gpu `
        --no-sandbox `
        --user-data-dir=$profilePath `
        --window-size=2048,1100 `
        --virtual-time-budget=18000 `
        --screenshot=$shotPath `
        'http://127.0.0.1:18050/?equipInstance=T25&equipUnit=09&eventTime=2026-05-18%2021%3A25%3A52&eventCode=1757'
    Get-Item -LiteralPath $shotPath | Select-Object FullName, Length
}
finally {
    Stop-Process -Id $tunnelProc.Id -ErrorAction SilentlyContinue
}
