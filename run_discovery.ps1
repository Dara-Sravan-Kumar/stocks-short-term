# StockBot daily strategy discovery — invoked by the "StockBot Strategy
# Discovery" scheduled task (once daily, evening, after the market-close run).
# Proposes published swing strategies, gates them on historical backtests, and
# registers survivors into the DISCOVERED fleet. Logs to data\logs and keeps
# the last 30 discovery logs. Mirrors run_stockbot.ps1's pattern.

Set-Location "C:\Users\srava\stocks-short-term"
New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = "data\logs\discovery_$stamp.log"

"[$(Get-Date -Format o)] Strategy discovery starting" | Out-File $log -Encoding utf8
& ".\.venv\Scripts\python.exe" discover_strategies.py 2>&1 | Out-File $log -Append -Encoding utf8
"[$(Get-Date -Format o)] Strategy discovery finished (exit $LASTEXITCODE)" |
    Out-File $log -Append -Encoding utf8

# Keep only the newest 30 discovery logs
Get-ChildItem "data\logs\discovery_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
