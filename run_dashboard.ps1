# Starts the StockBot web dashboard if it isn't already running (port 8501 check).
# Registered as the "StockBot Web Dashboard" scheduled task (logon trigger).
#
# Streamlit is launched DETACHED (Start-Process) so this script returns
# immediately and the task goes back to Ready. The old model ran streamlit in
# the foreground, so the task sat "Running" forever; when streamlit later died
# the instance hung, and MultipleInstances=IgnoreNew then silently swallowed
# every restart (manual OR logon). Detached launch = clean restart + reliable
# start on boot.

$ErrorActionPreference = "SilentlyContinue"
$root = "C:\Users\srava\stocks-short-term"

# Already serving? don't double-start.
if (Get-NetTCPConnection -LocalPort 8501 -State Listen) { exit 0 }

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format yyyyMMdd
$out = Join-Path $logDir "dashboard_$stamp.log"
$err = Join-Path $logDir "dashboard_$stamp.err"

# Keep only the newest 14 days of dashboard logs.
Get-ChildItem $logDir -Filter "dashboard_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force

Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
    -ArgumentList "-m", "streamlit", "run", "dashboard_web.py", `
        "--server.headless", "true", "--server.port", "8501" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err
