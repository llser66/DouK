param([string]$PythonPath, [switch]$SkipDependencies)
$ErrorActionPreference = 'Stop'
$installCandidates = @()
if ($PythonPath) { $installCandidates += $PythonPath }
$installPy = Get-Command py -ErrorAction SilentlyContinue
if ($installPy) {
    $installLocated = & $installPy.Source -3 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0 -and $installLocated) { $installCandidates += $installLocated.Trim() }
}
$installPythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($installPythonCommand -and $installPythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
    $installCandidates += $installPythonCommand.Source
}
$installPython = $null
foreach ($installCandidate in $installCandidates) {
    if (Test-Path -LiteralPath $installCandidate) {
        & $installCandidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $installPython = $installCandidate; break }
    }
}
if (-not $installPython) { throw 'Python 3.12+ is required. Install from python.org with the Python launcher, then retry.' }
$installUpstream = Join-Path (Split-Path -Parent $PSScriptRoot) 'TikTokDownloader'
if (-not (Test-Path -LiteralPath (Join-Path $installUpstream 'requirements.txt'))) {
    throw 'Keep DouK-Web and TikTokDownloader beside each other inside the extracted release folder.'
}
$installVirtual = Join-Path $PSScriptRoot '.venv-api\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $installVirtual)) {
    & $installPython -m venv (Join-Path $PSScriptRoot '.venv-api')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $installVirtual -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'The existing virtual environment is incompatible. Preserve it separately and create a Python 3.12+ environment.' }
if (-not $SkipDependencies) {
    & $installVirtual -m pip install -r (Join-Path $installUpstream 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check Internet access and the pip output; do not start collection yet.' }
    & $installVirtual -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
}
$installNode = Get-Command node -ErrorAction SilentlyContinue
if (-not $installNode) { Write-Warning 'Node.js 18+ is required for upstream JavaScript support. Install the current LTS from nodejs.org.' }
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'data') -Force | Out-Null
Write-Host 'Environment ready. Run start.ps1, or double-click the workspace launcher.'
Write-Host 'This installer does not create an administrator, copy credentials, or change firewall rules.'
