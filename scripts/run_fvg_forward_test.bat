@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist logs mkdir logs
if not exist data mkdir data

echo Starting FVG paper forward test...
echo Dashboard: http://127.0.0.1:5556
echo Health:    http://127.0.0.1:5557/health

:: Kill any stale bot/dashboard Python processes before starting
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.fvg.bot*' -or \$_.CommandLine -like '*src.fvg.dashboard*' -or \$_.CommandLine -like '*fvg_paper_bot.py*' -or \$_.CommandLine -like '*fvg_dashboard.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>nul
:: Wait briefly so Windows releases the health/dashboard ports
ping 127.0.0.1 -n 3 >nul

:: Start dashboard in a visible window (no redirect so you can see Flask output)
start "FVG Dashboard" cmd /k "python -m src.fvg.dashboard"

:: Give dashboard a moment to start, then open the browser
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5556

:: Bot with auto-restart wrapper
:loop
echo [+] Starting FVG paper bot at %date% %time% ...
python -m src.fvg.bot
set exitcode=%errorlevel%

:: Ctrl+C or clean exit -> stop the wrapper
if %exitcode% lss 0 goto end
if %exitcode% == 0 goto end

echo [!] Bot crashed with exit code %exitcode%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop

:end
echo Bot stopped intentionally. Closing.
