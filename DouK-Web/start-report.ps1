param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$reportPageUrl = 'http://127.0.0.1:8877/report'

try {
    Write-Host 'Checking and starting DouK...'
    & (Join-Path $PSScriptRoot 'start.ps1') -NoBrowser
    $reportPageStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/auth/status' -TimeoutSec 5
    if ($reportPageStatus.app -ne 'DouK Workspace') {
        throw 'Port 8877 is not the DouK website.'
    }
    $reportPageResponse = Invoke-WebRequest -Uri $reportPageUrl -UseBasicParsing -TimeoutSec 5
    if ($reportPageResponse.StatusCode -ne 200 -or $reportPageResponse.Content -notmatch 'id="report-form"') {
        throw 'The employee page is not ready. Check data\server-error.log.'
    }
    Write-Host "Employee reporting is ready: $reportPageUrl"
    if (-not $NoBrowser) {
        Start-Process -FilePath $reportPageUrl
    }
    exit 0
} catch {
    Write-Host ('Could not open DouK: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('Log file: ' + (Join-Path $PSScriptRoot 'data\server-error.log'))
    exit 1
}
