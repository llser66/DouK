param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$toolsUrl = 'http://127.0.0.1:8879'
$toolsData = Join-Path $PSScriptRoot 'data\tools'
$toolsPython = Join-Path $PSScriptRoot '.venv-api\Scripts\python.exe'
$toolsMutex = [System.Threading.Mutex]::new($false, 'Local\DouKTools8879')
$toolsLockAcquired = $false
try {
$toolsLockAcquired = $toolsMutex.WaitOne(20000)
if (-not $toolsLockAcquired) { throw 'Another tools launcher is still starting.' }
$toolsReady = $false
try { $toolsReady = (Invoke-RestMethod "$toolsUrl/api/health" -TimeoutSec 2).app -eq 'DouK Tools' } catch {}
if (-not $toolsReady) {
    if (Get-NetTCPConnection -LocalPort 8879 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8879 is occupied by another service.' }
    New-Item -ItemType Directory -Path $toolsData -Force | Out-Null
    Start-Process -FilePath $toolsPython -ArgumentList @('"' + (Join-Path $PSScriptRoot 'tools_server.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $toolsData 'server-output.log') -RedirectStandardError (Join-Path $toolsData 'server-error.log') | Out-Null
    $toolsDeadline = [DateTime]::UtcNow.AddSeconds(20)
    while (-not $toolsReady) {
        if ([DateTime]::UtcNow -gt $toolsDeadline) { throw 'Tools startup timed out. Check data\tools\server-error.log.' }
        Start-Sleep -Milliseconds 250
        try { $toolsReady = (Invoke-RestMethod "$toolsUrl/api/health" -TimeoutSec 2).app -eq 'DouK Tools' } catch {}
    }
}
Write-Host "DouK Tools is ready: $toolsUrl"
if (-not $NoBrowser) { Start-Process $toolsUrl }
} finally {
    if ($toolsLockAcquired) { $toolsMutex.ReleaseMutex() }
    $toolsMutex.Dispose()
}
