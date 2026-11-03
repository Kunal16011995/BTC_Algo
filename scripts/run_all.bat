@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist logs mkdir logs
if not exist data mkdir data

echo ============================================================
echo  BTC Momentum combined launcher
echo ============================================================
echo NOTE: FVG momentum bot trades BTCUSDT on the Bybit demo account.
echo.
echo This launcher starts:
echo   - Flask dashboard      (http://127.0.0.1:5556)
echo   - FVG momentum bot     (health: http://127.0.0.1:5557/health)
echo ============================================================
echo.

:: Kill any stale bot/dashboard processes before starting
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.fvg.bot*' -or \$_.CommandLine -like '*src.fvg.dashboard*' -or \$_.CommandLine -like '*fvg_paper_bot.py*' -or \$_.CommandLine -like '*fvg_dashboard.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>nul

:: Wait so Windows releases the dashboard/bot/health ports
timeout /t 4 /nobreak >nul

:: Start the Flask dashboard in its own window
start "FVG Dashboard" cmd /k "python -m src.fvg.dashboard"

:: Give dashboard a moment to start, then open the browser
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5556

:: Start the FVG momentum forward-test bot with auto-restart
start "FVG Momentum Bot" cmd /k "scripts\start_fvg_bot.bat"

echo.
echo Dashboard and FVG bot windows should now be starting.
echo Close each window individually to stop that component.
echo.
pause
