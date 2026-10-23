@echo off
cd /d "%~dp0.."
python runner/omnia_nodes_map.py %*
pause
