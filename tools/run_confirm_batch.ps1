# Unattended confirm-and-calibrate batch, for Windows Task Scheduler.
#
# Runs `tradeup candidates confirm-batch --top 3` from the repository root (so
# .env and tradeup.db resolve) and appends all output to a dated log under
# artifacts\logs\. Read-only against every venue; nothing here can spend.
#
# Register (every 8 hours, runs only while the user is logged on):
#   schtasks /Create /F /TN "CS2 Tradeup Confirm Batch" /SC HOURLY /MO 8 /ST 08:00 ^
#     /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"<repo>\tools\run_confirm_batch.ps1\""

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "artifacts\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd"
$log = Join-Path $logDir "confirm-batch-$stamp.log"

"=== $(Get-Date -Format o) confirm-batch start ===" | Add-Content -Path $log -Encoding utf8
$output = & (Join-Path $repo ".venv\Scripts\tradeup.exe") candidates confirm-batch --top 3 2>&1 |
    ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE
$output | Add-Content -Path $log -Encoding utf8
"=== $(Get-Date -Format o) confirm-batch exit $code ===" | Add-Content -Path $log -Encoding utf8
exit $code
