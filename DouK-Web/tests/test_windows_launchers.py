"""Exercise Windows launch/desktop handoff without changing a user's desktop or startup tasks."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
POWERSHELL=shutil.which('powershell.exe')


@unittest.skipUnless(POWERSHELL, 'Windows PowerShell required')
class WindowsLauncherTests(unittest.TestCase):
    def run_ps(self, script, expected=0):
        # UTF-16 command transport preserves Chinese paths under Windows PowerShell 5.1.
        import base64
        command="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';"+script
        result=subprocess.run([POWERSHELL,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                               '-EncodedCommand',base64.b64encode(command.encode('utf-16le')).decode()],
                              capture_output=True,text=True,encoding='utf-8',timeout=30)
        if expected==0:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0)
        return result

    @staticmethod
    def quote(value):
        return "'"+str(value).replace("'","''")+"'"

    @staticmethod
    def link_strings(path):
        # Read the documented Shell Link StringData independently of COM.
        # WScript.Shell also loses Chinese file names while reading on English Windows.
        body=path.read_bytes()
        assert struct.unpack_from('<I',body,0)[0]==0x4c
        flags=struct.unpack_from('<I',body,0x14)[0]
        assert flags & 0x80, 'Shell Link must preserve Unicode strings'
        cursor=0x4c
        if flags & 1:cursor+=2+struct.unpack_from('<H',body,cursor)[0]
        if flags & 2:cursor+=struct.unpack_from('<I',body,cursor)[0]
        values={}
        for flag,name in ((4,'name'),(8,'relative'),(16,'directory'),(32,'arguments'),(64,'icon')):
            if flags & flag:
                size=struct.unpack_from('<H',body,cursor)[0]*2
                cursor+=2
                values[name]=body[cursor:cursor+size].decode('utf-16le')
                cursor+=size
        return values

    def test_shortcut_works_in_chinese_path_and_existing_entry_is_backed_up(self):
        with tempfile.TemporaryDirectory(prefix='DouK-shortcut-') as temp:
            base=Path(temp)/'老板 项目';base.mkdir()
            desktop=Path(temp)/'桌面';desktop.mkdir()
            backup=Path(temp)/'入口备份'
            for name in ('setup-shortcut.ps1','open-workspace.ps1'):
                shutil.copy2(ROOT/name,base/name)
            (base/'open-workspace.ps1').write_text(
                "[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'launched.txt'),$PSScriptRoot)\n",
                encoding='utf-8-sig')
            call=f"& {self.quote(base/'setup-shortcut.ps1')} -DesktopPath {self.quote(desktop)} -BackupDirectory {self.quote(backup)} -NoMessage;"
            self.run_ps(call)
            shortcut=desktop/'DouK 老板工作台.lnk'
            before=shortcut.read_bytes()
            self.run_ps(call)
            manifest=list(backup.glob('*/manifest.json'))
            self.assertEqual(len(manifest),1)
            record=json.loads(manifest[0].read_text(encoding='utf-8-sig'))
            self.assertEqual(Path(record['backup']).read_bytes(),before)
            info=self.link_strings(shortcut)
            self.assertIn('-WindowStyle Hidden',info['arguments'])
            self.assertIn('"'+str((base/'open-workspace.ps1').resolve())+'"',info['arguments'])
            self.assertEqual(Path(info['directory']).resolve(),base.resolve())
            self.run_ps(f"Start-Process -FilePath {self.quote(shortcut)} -Wait")
            self.assertEqual(Path((base/'launched.txt').read_text(encoding='utf-8-sig')).resolve(),base.resolve())

    def test_launcher_opens_correct_entry_after_readiness_check_and_reports_failure(self):
        with tempfile.TemporaryDirectory(prefix='DouK-open-') as temp:
            base=Path(temp)/'老板 项目';base.mkdir()
            shutil.copy2(ROOT/'open-workspace.ps1',base/'open-workspace.ps1')
            (base/'start.ps1').write_text("param([switch]$NoBrowser)\nif(-not $NoBrowser){throw 'Unexpected browser'}",encoding='utf-8-sig')
            invoke=f"& {self.quote(base/'open-workspace.ps1')} -NoDialog"
            ready="function Invoke-RestMethod {return @{app='DouK Workspace'}};function Start-Process {param($FilePath) Write-Output ('OPEN '+$FilePath)};"
            result=self.run_ps(ready+invoke)
            self.assertIn('OPEN http://127.0.0.1:8877/admin',result.stdout)
            failed=self.run_ps("function Invoke-RestMethod {return @{app='different app'}};"+invoke,expected=1)
            self.assertIn('网站暂时没有启动成功',failed.stderr)
            self.assertTrue((base/'data'/'launcher-error.log').is_file())
            self.assertFalse((base/'data'/'workspace.sqlite').exists())

    def test_shipped_powershell_files_parse_on_windows_powershell(self):
        for name in ('setup-shortcut.ps1','open-workspace.ps1'):
            with self.subTest(script=name):
                command=f"$tokens=$null;$errors=$null;[Management.Automation.Language.Parser]::ParseFile({self.quote(ROOT/name)},[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){{throw ($errors|Out-String)}}"
                self.run_ps(command)


if __name__=='__main__':unittest.main()
