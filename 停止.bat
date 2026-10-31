@echo off
chcp 65001 >nul
title QingMing Updater - stop
cd /d "%~dp0"

set "PY="
python -c "import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo.
    echo  [ERROR] Python 3.7+ not found.
    echo.
    pause
    exit /b 1
)

%PY% "server\launcher.py" stop
echo.
pause
