param([string]$InterfaceAlias, [switch]$NoAutostart)

$ErrorActionPreference = 'Stop'
$lanData = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $lanData -Force | Out-Null
$lanStatusPath = Join-Path $lanData 'lan-status.json'
$lanPrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $lanPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    @{ configured = $false; message = '请用管理员 PowerShell 运行 setup-lan.ps1，配置仅限本地子网的访问规则。' } |
        ConvertTo-Json | Set-Content -LiteralPath $lanStatusPath -Encoding UTF8
    throw 'Run this setup script from an administrator PowerShell window.'
}

$lanPythonCandidate = Join-Path $PSScriptRoot '.venv-api\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $lanPythonCandidate)) {
    $lanPythonCandidate = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $lanPythonCandidate)) { throw 'The workspace Python runtime was not found.' }
# Windows virtualenv launches the base executable; the firewall must match that process.
$lanPython = (& $lanPythonCandidate -c 'import sys; print(sys._base_executable or sys.executable)').Trim()
if (-not (Test-Path -LiteralPath $lanPython)) { throw 'The base Python executable was not found.' }
if (-not $InterfaceAlias) {
    $lanAdapters = @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' })
    if ($lanAdapters.Count -ne 1) { throw 'Specify -InterfaceAlias for the department LAN adapter.' }
    $InterfaceAlias = $lanAdapters[0].Name
}
$lanAdapter = Get-NetAdapter -Name $InterfaceAlias -ErrorAction Stop
if ($lanAdapter.Status -ne 'Up') { throw 'The selected LAN adapter is not connected.' }
$lanRuleName = 'DouK-Business-8877-LAN'
$lanRule = Get-NetFirewallRule -Name $lanRuleName -ErrorAction SilentlyContinue
if ($lanRule) {
    # This named rule belongs to this installer. Keep its scope precise on repeated runs.
    $lanRule | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Private,Public -InterfaceAlias $InterfaceAlias
    $lanRule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 8877
    $lanRule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
    $lanRule | Get-NetFirewallApplicationFilter | Set-NetFirewallApplicationFilter -Program $lanPython
} else {
    New-NetFirewallRule -Name $lanRuleName -DisplayName 'DouK business reporting (LAN only, TCP 8877)' `
        -Description 'Employee reporting and authenticated business management. Exact Python executable and selected LAN adapter; LocalSubnet only. Tools 8879 remains local.' `
        -Direction Inbound -Action Allow -Enabled True -Profile Private,Public -Protocol TCP -LocalPort 8877 `
        -RemoteAddress LocalSubnet -InterfaceAlias $InterfaceAlias -Program $lanPython | Out-Null
}
$lanAdvertisedAddresses = @(Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'
} | Select-Object -ExpandProperty IPAddress)
@{ listen_host = '0.0.0.0'; advertise_addresses = $lanAdvertisedAddresses } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $lanData 'network.json') -Encoding UTF8

$lanTaskName = 'DouK-Department-Workspace'
$lanAutostart = $false
if (-not $NoAutostart) {
    $lanUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $lanScript = Join-Path $PSScriptRoot 'start.ps1'
    $lanTask = Get-ScheduledTask -TaskName $lanTaskName -ErrorAction SilentlyContinue
    $lanArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $lanScript + '" -NoBrowser'
    if ($lanTask) {
        if (-not ($lanTask.Actions | Where-Object { $_.Arguments -eq $lanArguments })) {
            throw 'A different task uses the DouK task name. It was not overwritten.'
        }
        Enable-ScheduledTask -TaskName $lanTaskName | Out-Null
    } else {
        $lanAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $lanArguments -WorkingDirectory $PSScriptRoot
        $lanTrigger = New-ScheduledTaskTrigger -AtLogOn -User $lanUser
        $lanTaskPrincipal = New-ScheduledTaskPrincipal -UserId $lanUser -LogonType Interactive -RunLevel Limited
        $lanTaskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $lanTaskName -Action $lanAction -Trigger $lanTrigger -Principal $lanTaskPrincipal `
            -Settings $lanTaskSettings -Description 'Starts the DouK department reporting service after this Windows user signs in. No website passwords are stored in this task.' | Out-Null
    }
    $lanAutostart = $true
}
@{ configured = $true; rule_name = $lanRuleName; interface_alias = $InterfaceAlias;
   remote_scope = 'LocalSubnet'; profiles = @('Private', 'Public'); port = 8877;
   autostart = $lanAutostart; task_name = $(if ($lanAutostart) { $lanTaskName } else { '' });
   message = '已配置指定网卡、本地子网 TCP 8877 访问；独立采集工具 8879 仍仅本机访问。跨设备访问需在员工设备上验证。';
   configured_at = [DateTimeOffset]::Now.ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath $lanStatusPath -Encoding UTF8
Write-Host "LAN firewall configured: $InterfaceAlias / TCP 8877 / LocalSubnet"
Write-Host "Start after Windows sign-in: $lanAutostart"
