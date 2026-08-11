param(
    [switch]$Uninstall,
    [string]$SyncTime     = "07:30",
    [string]$DiscoverDay  = "SUN",
    [string]$DiscoverTime = "08:30",
    [int]$DiscoverLimit   = 40
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tasks = @("Stage sync", "Stage discover")

if ($Uninstall) {
    foreach ($name in $tasks) {
        schtasks /Delete /TN $name /F 2>$null
        if ($LASTEXITCODE -eq 0) { "removed $name" } else { "$name was not registered" }
    }
    exit 0
}

$sync = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$here\scheduled-sync.ps1`""
$disc = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$here\weekly-discover.ps1`"" +
        " -Limit $DiscoverLimit"

schtasks /Create /F /SC DAILY  /ST $SyncTime /TN $tasks[0] /TR $sync
schtasks /Create /F /SC WEEKLY /D $DiscoverDay /ST $DiscoverTime /TN $tasks[1] /TR $disc

""
"registered:"
foreach ($name in $tasks) {
    schtasks /Query /TN $name /FO LIST | Select-String "TaskName|Next Run Time"
}
""
"run one now : schtasks /Run /TN `"Stage sync`""
"remove both : powershell -File scripts\install-tasks.ps1 -Uninstall"
