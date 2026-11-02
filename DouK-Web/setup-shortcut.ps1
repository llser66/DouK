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
# WScript.Shell.Save can replace Chinese characters on non-Chinese Windows.
# IShellLinkW and IPersistFile keep both the file name and target in Unicode.
if (-not ('DouK.DesktopLink' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace DouK {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, IntPtr findData, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int show);
        void SetShowCmd(int show);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    public static class DesktopLink {
        public static void Save(string file, string target, string arguments, string directory, string description, string icon) {
            object instance = new ShellLink();
            try {
                IShellLinkW link = (IShellLinkW)instance;
                link.SetPath(target);
                link.SetArguments(arguments);
                link.SetWorkingDirectory(directory);
                link.SetDescription(description);
                link.SetIconLocation(icon, 14);
                IPersistFile persisted = (IPersistFile)instance;
                persisted.Save(file, true);
                persisted.Load(file, 0);
                StringBuilder actualArguments = new StringBuilder(32768);
                StringBuilder actualDirectory = new StringBuilder(32768);
                link.GetArguments(actualArguments, actualArguments.Capacity);
                link.GetWorkingDirectory(actualDirectory, actualDirectory.Capacity);
                if (actualArguments.ToString() != arguments || !String.Equals(actualDirectory.ToString(), directory, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Shortcut verification failed.");
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
    }
}
'@
}
$shortcutTarget = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcutArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $shortcutLauncher + '"'
[DouK.DesktopLink]::Save($shortcutPath, $shortcutTarget, $shortcutArguments, $PSScriptRoot,
    '启动 DouK 后台并打开老板工作台', (Join-Path $env:SystemRoot 'System32\shell32.dll'))
Write-Output $shortcutPath
if ($shortcutBackup) { Write-Output ('Original shortcut backup: ' + $shortcutBackup) }
if (-not $NoMessage) {
    Add-Type -AssemblyName System.Windows.Forms
    $shortcutMessage = "桌面入口已创建：DouK 老板工作台。`n`n环境安装好后，双击即可启动后台并打开网页。以后请保留项目所在文件夹，不要移动或删除。`n`n员工访问和 Windows 登录后自动启动，由部署人员按「老板交付说明.md」配置。"
    if ($shortcutBackup) { $shortcutMessage += "`n`n旧入口备份：$shortcutBackup" }
    [System.Windows.Forms.MessageBox]::Show($shortcutMessage, 'DouK 桌面入口', 'OK', 'Information') | Out-Null
}
