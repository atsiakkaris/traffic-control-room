@echo off
cd /d "%~dp0.."
rem Preview the API's CURRENT path geometry on the map (read-only, nothing saved to the database).
python runner/bt_paths_map.py --live-geometry %*
if %errorlevel% == 0 (
    start "" "reports\bt_paths_map_live.html"
)
pause
