@echo off
cd /d "%~dp0"

for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v "^#" .env`) do (
    set "%%A=%%B"
)

python runner/run_tests.py
pause
