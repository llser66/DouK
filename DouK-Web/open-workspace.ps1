param([switch]$NoBrowser, [switch]$NoDialog)

$ErrorActionPreference = 'Stop'
$workspaceEntry = 'http://127.0.0.1:8877/admin'
try {
    & (Join-Path $PSScriptRoot 'start.ps1') -NoBrowser
    $workspaceReady = Invoke-RestMethod -Uri 'http://127.0.0.1:8877/api/auth/status' -TimeoutSec 5
    if ($workspaceReady.app -ne 'DouK Workspace') { throw 'Port 8877 is not the DouK website.' }
    if (-not $NoBrowser) { Start-Process -FilePath $workspaceEntry }
    Write-Output $workspaceEntry
} catch {
    $workspaceFailure = $_.Exception.Message
    $workspaceLog = Join-Path $PSScriptRoot 'data\launcher-error.log'
    try {
        New-Item -ItemType Directory -Path (Split-Path -Parent $workspaceLog) -Force | Out-Null
        ('{0:o} {1}' -f [DateTimeOffset]::Now, $workspaceFailure) | Add-Content -LiteralPath $workspaceLog -Encoding UTF8
    } catch { }
    $workspaceMessage = "网站暂时没有启动成功。`n`n首次使用，请让部署人员先运行「安装环境.cmd」；已经安装过，请把本提示发给维护人员。`n`n不要删除 data 文件夹或反复解压覆盖。`n日志：$workspaceLog`n原因：$workspaceFailure"
    if ($NoDialog) { throw $workspaceMessage }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($workspaceMessage, 'DouK 启动提示', 'OK', 'Warning') | Out-Null
    exit 1
}
