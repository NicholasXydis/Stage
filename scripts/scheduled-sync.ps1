param(
    [string]$StageExe = "$PSScriptRoot\..\.venv\Scripts\stage.exe",
    [string]$LogDir   = "$env:LOCALAPPDATA\stage\logs"
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$PSDefaultParameterValues["Tee-Object:Encoding"] = "utf8"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $LogDir "sync-$stamp.log"
$env:PYTHONIOENCODING = "utf-8"

& $StageExe sync *>&1 | Tee-Object -FilePath $log
$syncExit = $LASTEXITCODE

& $StageExe doctor *>&1 | Tee-Object -FilePath $log -Append
$doctorExit = $LASTEXITCODE

Get-ChildItem $LogDir -Filter "sync-*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force

if ($doctorExit -ne 0) { exit $doctorExit }
exit $syncExit
