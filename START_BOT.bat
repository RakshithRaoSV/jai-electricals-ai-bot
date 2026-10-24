@echo off
TITLE Jai Electricals Virtual Employee
echo Waking up the Virtual Employee...

:: This command launches the master loop in a NEW window, forces it to start MINIMIZED,
:: and keeps it safely on the taskbar without interrupting the staff's typing.
start "Jai Electricals Bot" /min cmd /k "cd /d "D:\Quote Bot BE" && call venv\Scripts\activate && python master_loop.py"

exit