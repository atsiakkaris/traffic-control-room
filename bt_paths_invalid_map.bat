@echo off
cd /d "%~dp0"
python runner/bt_paths_invalid_map.py
if %errorlevel% == 0 (
    start "" "reports\bt_paths_invalid_map.html"
) else (
    pause
)
