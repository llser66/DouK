param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$workspaceUrl = 'http://127.0.0.1:8877'
$workspaceServer = Join-Path $PSScriptRoot 'server.py'
$workspaceData = Join-Path $PSScriptRoot 'data'
$workspaceDownloads = Join-Path $PSScriptRoot 'Downloads'
$workspaceMutex = [System.Threading.Mutex]::new($false, 'Local\DouKWorkspace8877')
$workspaceLockAcquired = $false

function Test-WorkspaceReady {
    try {
        $workspaceStatus = Invoke-RestMethod -Uri "$workspaceUrl/api/auth/status" -TimeoutSec 2
        return $workspaceStatus.app -eq 'DouK Workspace'
    } catch {
        return $false
    }
}

try {
    $workspaceLockAcquired = $workspaceMutex.WaitOne(20000)
    if (-not $workspaceLockAcquired) {
        throw 'Another DouK launcher is still starting. Please try again in a moment.'
    }

    if (-not (Test-WorkspaceReady)) {
        $workspaceListener = Get-NetTCPConnection -LocalPort 8877 -State Listen -ErrorAction SilentlyContinue
        if ($workspaceListener) {
            throw 'Port 8877 is occupied, but DouK is not responding. Check data\server-error.log.'
        }

        $workspaceCandidates = @(
            (Join-Path $PSScriptRoot '.venv-api\Scripts\python.exe'),
            (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
        )
        $workspaceInstalledPython = Get-Command python -ErrorAction SilentlyContinue
        if ($workspaceInstalledPython -and $workspaceInstalledPython.Source -notlike '*\Microsoft\WindowsApps\*') {
            $workspaceCandidates += $workspaceInstalledPython.Source
        }
        $workspacePython = $null
        foreach ($workspaceCandidate in $workspaceCandidates) {
            if (Test-Path -LiteralPath $workspaceCandidate) {
                & $workspaceCandidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $workspacePython = $workspaceCandidate
                    break
                }
            }
        }
        if (-not $workspacePython) {
            throw 'Python 3.12 or newer was not found. Install Python, then start DouK again.'
        }

        New-Item -ItemType Directory -Path $workspaceData -Force | Out-Null
        $workspaceProcess = Start-Process -FilePath $workspacePython `
            -ArgumentList @('"' + $workspaceServer + '"') `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $workspaceData 'server-output.log') `
            -RedirectStandardError (Join-Path $workspaceData 'server-error.log') -PassThru

        $workspaceDeadline = [DateTime]::UtcNow.AddSeconds(20)
        while (-not (Test-WorkspaceReady)) {
            $workspaceProcess.Refresh()
            if ($workspaceProcess.HasExited) {
                throw 'DouK stopped during startup. Check data\server-error.log.'
            }
            if ([DateTime]::UtcNow -gt $workspaceDeadline) {
                throw 'DouK startup timed out. Check data\server-error.log.'
            }
            Start-Sleep -Milliseconds 250
        }
    }

    Write-Host "DouK management: $workspaceUrl/admin"
    Write-Host "Employee reporting: $workspaceUrl/report"
    $workspaceLanAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $_.IPAddress -notlike '127.*' -and $_.InterfaceAlias -notmatch 'vEthernet|tun|Loopback' -and
        ($_.IPAddress -like '10.*' -or $_.IPAddress -like '192.168.*' -or $_.IPAddress -match '^172\.(1[6-9]|2[0-9]|3[01])\.')
    })
    foreach ($workspaceLanAddress in $workspaceLanAddresses) {
        Write-Host "Department LAN: http://$($workspaceLanAddress.IPAddress):8877/report"
    }
    & (Join-Path $PSScriptRoot 'start-tools.ps1') -NoBrowser
    if (-not $NoBrowser) {
        Start-Process "$workspaceUrl/admin"
    }
} finally {
    if ($workspaceLockAcquired) { $workspaceMutex.ReleaseMutex() }
    $workspaceMutex.Dispose()
}
