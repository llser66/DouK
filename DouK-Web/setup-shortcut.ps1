param([string]$DesktopPath = [Environment]::GetFolderPath('Desktop'),
      [string]$BackupDirectory = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'DouK\ShortcutBackups'),
      [switch]$NoMessage)

$ErrorActionPreference = 'Stop'
function Get-ShortcutDigest([string]$Path) {
    $shortcutHash = [Security.Cryptography.SHA256]::Create()
    $shortcutStream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($shortcutHash.ComputeHash($shortcutStream)).Replace('-', '') }
    finally { $shortcutStream.Dispose(); $shortcutHash.Dispose() }
}
$shortcutLauncher = Join-Path $PSScriptRoot 'open-workspace.ps1'
if (-not (Test-Path -LiteralPath $shortcutLauncher -PathType Leaf)) { throw 'Keep this script inside the complete DouK-Web folder.' }
if (-not (Test-Path -LiteralPath $DesktopPath -PathType Container)) { throw 'Desktop folder does not exist.' }
$shortcutPath = Join-Path $DesktopPath 'DouK 老板工作台.lnk'
$shortcutBackup = ''
if (Test-Path -LiteralPath $shortcutPath) {
    $shortcutStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
    $shortcutBackupRoot = Join-Path $BackupDirectory $shortcutStamp
    New-Item -ItemType Directory -Path $shortcutBackupRoot -Force | Out-Null
    $shortcutBackup = Join-Path $shortcutBackupRoot 'DouK 老板工作台.lnk'
    Copy-Item -LiteralPath $shortcutPath -Destination $shortcutBackup
    if ((Get-ShortcutDigest $shortcutPath) -ne (Get-ShortcutDigest $shortcutBackup)) { throw 'Shortcut backup verification failed.' }
    @{ original = $shortcutPath; backup = $shortcutBackup; sha256 = (Get-ShortcutDigest $shortcutBackup) } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $shortcutBackupRoot 'manifest.json') -Encoding UTF8
}
$shortcutShell = New-Object -ComObject WScript.Shell
try {
    $shortcutLink = $shortcutShell.CreateShortcut($shortcutPath)
    $shortcutLink.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcutLink.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $shortcutLauncher + '"'
    $shortcutLink.WorkingDirectory = $PSScriptRoot
    $shortcutLink.Description = '启动 DouK 后台并打开老板工作台'
    $shortcutLink.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',14'
    $shortcutLink.Save()
    $shortcutVerify = $shortcutShell.CreateShortcut($shortcutPath)
    if ($shortcutVerify.Arguments -ne $shortcutLink.Arguments -or $shortcutVerify.WorkingDirectory -ne $PSScriptRoot) { throw 'Shortcut verification failed.' }
} finally {
    [Runtime.InteropServices.Marshal]::ReleaseComObject($shortcutShell) | Out-Null
}
Write-Output $shortcutPath
if ($shortcutBackup) { Write-Output ('Original shortcut backup: ' + $shortcutBackup) }
if (-not $NoMessage) {
    Add-Type -AssemblyName System.Windows.Forms
    $shortcutMessage = "桌面入口已创建：DouK 老板工作台。`n`n环境安装好后，双击即可启动后台并打开网页。以后请保留项目所在文件夹，不要移动或删除。`n`n员工访问和 Windows 登录后自动启动，由部署人员按「老板交付说明.md」配置。"
    if ($shortcutBackup) { $shortcutMessage += "`n`n旧入口备份：$shortcutBackup" }
    [System.Windows.Forms.MessageBox]::Show($shortcutMessage, 'DouK 桌面入口', 'OK', 'Information') | Out-Null
}
