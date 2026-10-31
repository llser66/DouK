$ErrorActionPreference = 'Stop'
$upstreamFolder = Join-Path (Split-Path -Parent $PSScriptRoot) 'TikTokDownloader'
$virtualPython = Join-Path $PSScriptRoot '.venv-api\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $virtualPython)) {
    & (Join-Path $PSScriptRoot 'install.ps1')
} else {
    & $virtualPython -m pip install -r (Join-Path $upstreamFolder 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Upstream dependencies could not be installed. Check the pip output.' }
}
Push-Location -LiteralPath $upstreamFolder
try { & $virtualPython (Join-Path $PSScriptRoot 'enhanced_api.py') } finally { Pop-Location }
