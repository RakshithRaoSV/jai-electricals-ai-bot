import time
import subprocess
import sys
import os
from datetime import datetime, time as dt_time

# 1. PATH PROTECTION
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
os.chdir(BASE_DIR)

# 2. VIRTUAL ENV PROTECTION
PYTHON_EXEC = os.path.join(BASE_DIR, 'venv', 'Scripts', 'python.exe')

# Configuration
START_TIME = dt_time(9, 0)      # 9:00 AM
END_TIME = dt_time(20, 30)      # 8:30 PM
WORK_DAYS = [0, 1, 2, 3, 4, 5]  # Mon-Sat
CHECK_INTERVAL = 120            # 2 minutes between checks

def run_script(script_name):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Launching {script_name}...")
    try:
        # The "-u" flag forces Python to stream output live instead of buffering it
        subprocess.run(
            [PYTHON_EXEC, "-u", script_name],
            cwd=BASE_DIR,
            timeout=300
            # Removed capture_output so it prints directly to the live terminal!
        )
    except subprocess.TimeoutExpired:
        print(f"  | [TIMEOUT] {script_name} froze. Force killed to protect system.")
    except Exception as e:
        print(f"  | [SYSTEM CRASH] {e}")

if __name__ == "__main__":
    os.system('cls' if os.name == 'nt' else 'clear')
    print("=====================================================")
    print(" JAI ELECTRICALS VIRTUAL EMPLOYEE - MASTER LOOP")
    print(" (To stop the bot, simply close this window)")
    print("=====================================================\n")
    
    while True:
        now = datetime.now()
        current_time = now.time()
        
        if now.weekday() in WORK_DAYS and START_TIME <= current_time <= END_TIME:
            print(f"\n--- Starting Cycle at {now.strftime('%H:%M:%S')} ---")
            run_script("main.py")
            run_script("quote_manager.py")
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Cycle Complete. Sleeping for 2 minutes...")
            time.sleep(CHECK_INTERVAL)
        else:
            print(f"\n[{now.strftime('%H:%M:%S')}] Outside business hours. Sleeping...")
            time.sleep(600)