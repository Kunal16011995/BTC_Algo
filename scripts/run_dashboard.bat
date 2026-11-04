@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

echo Updating backtest data (this may take 20-40 seconds) ...
python -m src.fvg.compute_backtest
if errorlevel 1 (
    echo Backtest update failed, starting dashboard with existing data ...
) else (
    echo Backtest data updated.
)

echo Starting BTC dashboard on http://127.0.0.1:5556 ...
start "BTC Dashboard" python -m src.fvg.dashboard
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5556"
echo Dashboard is running. Close the "BTC Dashboard" window to stop.
pause
