@echo off
cd /d "%~dp0"
python runner/bt_paths_map.py --publish
if %errorlevel% == 0 (
    echo.
    echo Now run: git add docs\bt-paths-map.html
    echo          git commit -m "chore: update shared BT paths map"
    echo          git push
) else (
    pause
)
