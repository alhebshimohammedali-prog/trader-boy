@echo off
REM Double-click this to start the agent on the SCORED account.
REM
REM It opens a window and keeps running until the Thursday mark. Leave the
REM window open -- closing it stops the agent. Minimising it is fine.
REM
REM Nothing else is needed. The agent sleeps until the market opens, trades
REM each session, restarts itself if anything crashes, and stops on its own.

title Attention Weighted - COMPETITION ACCOUNT
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Attention Weighted - COMPETITION account (scored)
echo  ============================================================
echo.
echo   Orders placed here count toward your result.
echo   Leave this window OPEN. Minimising is fine.
echo.
echo   Next market open : Tue 16:30 Riyadh
echo   Stops by itself  : Thu 23:00 Riyadh (the mark)
echo.
echo   Press Ctrl+C twice to stop early.
echo  ============================================================
echo.

powershell -ExecutionPolicy Bypass -File "%~dp0keepalive.ps1" -Comp

echo.
echo  Agent has stopped. Scroll up to see why, or check runs\supervisor.log
echo.
pause
