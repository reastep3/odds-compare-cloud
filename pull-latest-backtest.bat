@echo off
cd /d "%~dp0"
python pull_latest.py backtest
echo.
pause
