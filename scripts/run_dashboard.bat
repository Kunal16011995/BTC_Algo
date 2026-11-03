@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

echo Starting BTC dashboard on http://127.0.0.1:5556 ...
start "BTC Dashboard" python -m src.fvg.dashboard
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5556"
echo Dashboard is running. Close the "BTC Dashboard" window to stop.
pause
