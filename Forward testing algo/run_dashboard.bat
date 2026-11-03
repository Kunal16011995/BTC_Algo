@echo off
cd /d "C:\Algos\Crypto\BTC"
echo Starting BTC dashboard on http://127.0.0.1:5556 ...
start "BTC Dashboard" python fvg_dashboard.py
timeout /t 2 >nul
start "" "http://127.0.0.1:5556"
echo Dashboard is running. Close the "BTC Dashboard" window to stop.
pause
