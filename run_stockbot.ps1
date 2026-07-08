# StockBot scheduled runner — invoked by Windows Task Scheduler (and by the
# Windows Task Dashboard's Run button at http://127.0.0.1:8787/).
# Logs each run to data\logs\run_<timestamp>.log and keeps the last 60 logs.
#
# -SkipNews: for the hourly in-between task ("StockBot Hourly Strategy Scan").
# Passes --skip-news through to run_daily.py — no news fetch, no fresh LLM
# sentiment calls, Discord stays quiet unless something actually happened.
# The twice-daily anchor task ("StockBot Daily Run") calls this with no
# switch, unchanged from before.

param(
    [switch]$SkipNews
)

Set-Location "C:\Users\srava\stocks-short-term"
New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = "data\logs\run_$stamp.log"

$pyArgs = @("run_daily.py")
if ($SkipNews) { $pyArgs += "--skip-news" }

"[$(Get-Date -Format o)] StockBot run starting$(if ($SkipNews) { ' (--skip-news)' })" |
    Out-File $log -Encoding utf8
& ".\.venv\Scripts\python.exe" @pyArgs 2>&1 | Out-File $log -Append -Encoding utf8
"[$(Get-Date -Format o)] StockBot run finished (exit $LASTEXITCODE)" |
    Out-File $log -Append -Encoding utf8

# Keep only the newest 60 logs
Get-ChildItem "data\logs\run_*.log" | Sort-Object Name -Descending |
    Select-Object -Skip 60 | Remove-Item -Force -ErrorAction SilentlyContinue
