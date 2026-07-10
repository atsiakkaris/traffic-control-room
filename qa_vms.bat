@echo off
cd /d "%~dp0"
python runner/qa.py --group VMS --max-dist 300 ^
    --ref "QA Locations.xlsx::VMS"
if %errorlevel% == 0 (
    start "" "reports\qa_vms.html"
) else (
    pause
)
