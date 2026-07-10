@echo off
cd /d "%~dp0"
python runner/qa.py --group "Traffic Detection" --max-dist 300 ^
    --ref "QA Locations.xlsx::Traffic Detection"
if %errorlevel% == 0 (
    start "" "reports\qa_traffic_detection.html"
) else (
    pause
)
