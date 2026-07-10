@echo off
cd /d "%~dp0"
REM 500m, matching GROUPS in runner/update_projects.py — VMS are sparse and their
REM reference coordinates are approximate. Keep the two in step.
python runner/qa.py --group VMS --max-dist 500 ^
    --ref "QA Locations.xlsx::VMS"
if %errorlevel% == 0 (
    start "" "reports\qa_vms.html"
) else (
    pause
)
