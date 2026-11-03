@echo off
setlocal

:: Project root is one level above scripts/
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist logs mkdir logs

echo Starting FVG momentum paper bot only ...
echo Health: http://127.0.0.1:5557/health

:: Kill any stale FVG paper bot processes before starting
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*src.fvg.bot*' -or \$_.CommandLine -like '*fvg_paper_bot.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" 2>nul

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
