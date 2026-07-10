@echo off
cd /d "%~dp0"
python runner/qa.py --group Bluetooth --max-dist 300 ^
    --ref "QA Locations.xlsx::Bluetooth"
if %errorlevel% == 0 (
    start "" "reports\qa_bluetooth.html"
) else (
    pause
)
