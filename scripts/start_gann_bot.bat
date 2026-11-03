@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist logs mkdir logs

echo Starting WD Gann Overnight forward-test bot...
echo Health: http://127.0.0.1:5558/health

:: Kill any stale Gann forward bot processes before starting
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.gann.forward_bot*' -or \$_.CommandLine -like '*gann_forward_bot.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>nul

:: Wait briefly so Windows releases the health port before rebinding
timeout /t 3 /nobreak >nul

:: Bot with auto-restart wrapper
:loop
echo [+] Starting Gann forward bot at %date% %time% ...
python -m src.gann.forward_bot
set exitcode=%errorlevel%

:: Ctrl+C or clean exit -> stop the wrapper
if %exitcode% lss 0 goto end
if %exitcode% == 0 goto end

echo [!] Bot crashed with exit code %exitcode%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop

:end
echo Bot stopped intentionally. Closing.
