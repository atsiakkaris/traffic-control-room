@echo off
cd /d "%~dp0"
REM Local app: BT paths map + saved flags + confirmed delete.
REM Opens in its own window (needs: pip install pywebview). Close the window or use Quit to stop.
python -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo App window not installed - opening in your browser. For the window: pip install pywebview
    python tools/control_room.py <nul
    if errorlevel 1 pause
    exit /b
)
start "" pythonw tools/control_room.py
