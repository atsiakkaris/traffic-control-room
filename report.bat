@echo off
cd /d "%~dp0"
py runner/report.py
if %errorlevel% == 0 (
    start "" "reports\latest.html"
) else (
    pause
)