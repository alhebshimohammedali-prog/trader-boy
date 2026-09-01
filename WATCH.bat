@echo off
REM Double-click to watch the agent live. Read-only -- safe to open and close
REM whenever you like, it does not touch the running agent.
REM
REM Follows the newest run's console log and keeps printing as new cycles
REM arrive. Close this window any time; the agent keeps going.

title Attention Weighted - LIVE LOG
cd /d "%~dp0"

powershell -ExecutionPolicy Bypass -NoProfile -Command ^
  "$d = Get-ChildItem 'runs' -Directory ^| Where-Object { $_.Name -match '^\d{8}-' } ^| Sort-Object LastWriteTime -Descending ^| Select-Object -First 1;" ^
  "if (-not $d) { Write-Host 'No run found. Start the agent first.'; Read-Host 'Enter to close'; exit }" ^
  "$f = Join-Path $d.FullName 'console.log';" ^
  "Write-Host ('watching ' + $f) -ForegroundColor Cyan;" ^
  "Write-Host 'Ctrl+C to stop watching (the agent keeps running)' -ForegroundColor DarkGray;" ^
  "Write-Host '';" ^
  "Get-Content $f -Wait -Tail 40"

pause
