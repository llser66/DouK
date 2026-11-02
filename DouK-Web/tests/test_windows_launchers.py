"""Exercise Windows launch/desktop handoff without changing a user's desktop or startup tasks."""
import json
from pathlib import Path
import shutil
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

    def test_shortcut_works_in_chinese_path_and_existing_entry_is_backed_up(self):
        with tempfile.TemporaryDirectory(prefix='DouK-shortcut-') as temp:
            base=Path(temp)/'老板 项目';base.mkdir()
            desktop=Path(temp)/'桌面';desktop.mkdir()
            backup=Path(temp)/'入口备份'
            for name in ('setup-shortcut.ps1','open-workspace.ps1'):
                shutil.copy2(ROOT/name,base/name)
            call=f"& {self.quote(base/'setup-shortcut.ps1')} -DesktopPath {self.quote(desktop)} -BackupDirectory {self.quote(backup)} -NoMessage;"
            self.run_ps(call)
            shortcut=desktop/'DouK 老板工作台.lnk'
            before=shortcut.read_bytes()
            self.run_ps(call)
            manifest=list(backup.glob('*/manifest.json'))
            self.assertEqual(len(manifest),1)
            record=json.loads(manifest[0].read_text(encoding='utf-8-sig'))
            self.assertEqual(Path(record['backup']).read_bytes(),before)
            query=f"$s=New-Object -ComObject WScript.Shell;$l=$s.CreateShortcut({self.quote(shortcut)});@{{target=$l.TargetPath;arguments=$l.Arguments;directory=$l.WorkingDirectory}}|ConvertTo-Json -Compress"
            info=json.loads(self.run_ps(query).stdout)
            self.assertTrue(info['target'].lower().endswith('powershell.exe'))
            self.assertIn('-WindowStyle Hidden',info['arguments'])
            self.assertIn('"'+str((base/'open-workspace.ps1').resolve())+'"',info['arguments'])
            self.assertEqual(Path(info['directory']).resolve(),base.resolve())

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
