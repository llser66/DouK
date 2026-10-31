$ErrorActionPreference = 'Stop'
# Match this checkout's absolute script names; never kill an arbitrary port owner.
$stopRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$stopScripts = @('server.py','tools_server.py','enhanced_api.py') | ForEach-Object { Join-Path $stopRoot $_ }
$stopProcesses = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine
})
foreach ($stopProcess in $stopProcesses) {
    foreach ($stopScript in $stopScripts) {
        if ($stopProcess.CommandLine.IndexOf($stopScript, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            Stop-Process -Id $stopProcess.ProcessId -ErrorAction Stop
            Write-Host "Stopped this checkout's process $($stopProcess.ProcessId)."
            break
        }
    }
}
Write-Host 'Stopped matching DouK processes. Run start.ps1 to restart; queued work resumes.'
