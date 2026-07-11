# Starts the StockBot web dashboard if it isn't already running (port 8501 check).
# Registered as the "StockBot Web Dashboard" scheduled task (at logon) -
# mirrors the OpenAlgo Server task's pattern (C:\Users\srava\openalgo\start_openalgo.ps1).

$listening = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
if ($listening) { exit 0 }

Set-Location "C:\Users\srava\stocks-short-term"
$logDir = "C:\Users\srava\stocks-short-term\data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("dashboard_" + (Get-Date -Format yyyyMMdd) + ".log")

# keep only the newest 14 dashboard logs
Get-ChildItem $logDir -Filter "dashboard_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

& .venv\Scripts\python.exe -m streamlit run dashboard_web.py --server.headless true --server.port 8501 2>&1 |
    Out-File $log -Append -Encoding utf8
