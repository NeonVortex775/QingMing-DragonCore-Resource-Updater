@echo off
chcp 65001 >nul
title QingMing Resource Updater
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
    echo  [ERROR] Python 3.7+ not found on this computer.
    echo.
    echo  Install it: https://www.python.org/downloads/
    echo  During setup, tick  "Add Python to PATH".
    echo  Then run this file again.
    echo.
    pause
    exit /b 1
)

if not exist "config.ini" ( 
    echo.
    echo  [ERROR] config.ini is missing.
    echo  Unzip the whole package first, do not move files around.
    echo.
    pause
    exit /b 1
)

%PY% "server\launcher.py" start
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo  Launcher exited with code %RC%.
    echo  Read the messages above, fix config.ini, then try again.
    pause
)
exit /b %RC%
