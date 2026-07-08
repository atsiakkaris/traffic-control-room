@echo off
cd /d "%~dp0"
python runner/update_projects.py
if %errorlevel% neq 0 pause
