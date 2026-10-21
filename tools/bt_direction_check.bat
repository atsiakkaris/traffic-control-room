@echo off
cd /d "%~dp0.."
python runner/bt_direction_check.py %*
pause
