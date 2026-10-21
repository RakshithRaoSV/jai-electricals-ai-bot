@echo off
title Stopping Quote Bot...
echo Shutting down the Virtual Employee...

wmic process where "name='python.exe' and commandline like '%%daemon.py%%'" call terminate >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%main.py%%'" call terminate >nul 2>&1
wmic process where "name='python.exe' and commandline like '%%quote_manager.py%%'" call terminate >nul 2>&1

echo.
echo Bot successfully stopped!
pause