@echo off
echo Waking up the bot to process immediately...
wmic process where "name='python.exe' and commandline like '%%daemon.py%%'" call terminate >nul 2>&1
call venv\Scripts\activate
python main.py
python quote_manager.py
start run_hidden.vbs
echo Done! Bot returned to background mode.
