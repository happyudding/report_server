@echo off
chcp 65001>nul
cd /d "%~dp0"
set EVAL_DB_PATH=%~dp0data\eval_sample.db
python "tools\run_testbench.py"
echo.
pause
