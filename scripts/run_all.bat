@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist logs mkdir logs
if not exist data mkdir data

echo ============================================================
echo  BTC Momentum + WD Gann + A.3 Backtest combined launcher
echo ============================================================
echo NOTE: FVG momentum bot trades BTCUSDT on the OLD Bybit demo account.
echo       WD Gann forward bot trades BTCUSDT on the NEW Bybit demo account.
echo       Because they use separate Bybit accounts, they can run
echo       simultaneously without position conflicts.
echo.
echo This launcher starts:
echo   - Flask dashboard      (http://127.0.0.1:5556)
echo   - FVG momentum bot     (health: http://127.0.0.1:5557/health)
echo   - WD Gann forward bot  (health: http://127.0.0.1:5558/health)
echo   - A.3 backtest tab     (data/a3_backtest.json, $100k capital)
echo ============================================================
echo.

:: Kill any stale bot/dashboard processes before starting
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.fvg.bot*' -or \$_.CommandLine -like '*src.gann.forward_bot*' -or \$_.CommandLine -like '*src.fvg.dashboard*' -or \$_.CommandLine -like '*fvg_paper_bot.py*' -or \$_.CommandLine -like '*gann_forward_bot.py*' -or \$_.CommandLine -like '*fvg_dashboard.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>nul

:: Wait so Windows releases the dashboard/bot/health ports
timeout /t 4 /nobreak >nul

:: Regenerate A.3 Green/Red Weekly backtest data ($100k capital) for dashboard
echo Regenerating A.3 backtest JSON...
python -m src.research.generate_a3_dashboard_json

:: Start the Flask dashboard in its own window
start "FVG Dashboard" cmd /k "python -m src.fvg.dashboard"

:: Give dashboard a moment to start, then open the browser
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5556

:: Start the FVG momentum forward-test bot with auto-restart
start "FVG Momentum Bot" cmd /k "scripts\start_fvg_bot.bat"

:: Start the WD Gann overnight forward-test bot with auto-restart
start "WD Gann Forward Bot" cmd /k "scripts\start_gann_bot.bat"

echo.
echo All three windows should now be starting.
echo Close each window individually to stop that component.
echo.
pause
