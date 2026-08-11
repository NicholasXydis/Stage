param(
    [string]$StageExe = "$PSScriptRoot\..\.venv\Scripts\stage.exe",
    [string]$LogDir   = "$env:LOCALAPPDATA\stage\logs",
    [int]$Limit       = 40
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$PSDefaultParameterValues["Tee-Object:Encoding"] = "utf8"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ("discover-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
$env:PYTHONIOENCODING = "utf-8"

& $StageExe discover --unregistered --apply --limit $Limit *>&1 | Tee-Object -FilePath $log

Get-ChildItem $LogDir -Filter "discover-*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 12 |
    Remove-Item -Force
exit 0
