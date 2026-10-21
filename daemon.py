import time
import subprocess
import sys
import logging
import os
from datetime import datetime, time as dt_time

# --- CONFIGURATION ---
START_TIME = dt_time(9, 0)      # 9:00 AM
END_TIME = dt_time(20, 30)      # 8:30 PM
WORK_DAYS = [0, 1, 2, 3, 4, 5]  # 0=Monday, 5=Saturday
CHECK_INTERVAL_SECONDS = 120    # Runs every 2 minutes
PROCESS_TIMEOUT = 300           # Kills a script if it freezes for 5 minutes

# 1. PATH PROTECTION: Force execution in the correct project folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# 2. LOGGING: Record everything to bot_log.txt
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot_log.txt"), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def run_script(script_name):
    """Executes a Python script in a secure, isolated subprocess with timeouts."""
    logging.info(f"-> Running {script_name}...")
    try:
        # sys.executable ensures it uses your 'venv' Python, not the global Windows Python
        result = subprocess.run(
            [sys.executable, script_name],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT,
            check=True
        )
        return True
    
    except subprocess.TimeoutExpired:
        logging.error(f"[TIMEOUT] {script_name} froze for >{PROCESS_TIMEOUT}s. Process killed.")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"[CRASH] {script_name} failed (Exit Code {e.returncode}).")
        logging.error(f"Error Details: {e.stderr.strip()}")
        return False
    except Exception as e:
        logging.error(f"[SYSTEM ERROR] Could not execute {script_name}: {e}")
        return False

def run_pipeline():
    logging.info("Starting Automation Cycle...")
    success_main = run_script("main.py")
    success_quote = run_script("quote_manager.py")
    
    if success_main and success_quote:
        logging.info("Cycle Completed Successfully.")
    else:
        logging.warning("Cycle completed with soft errors. Recovering on next loop.")

if __name__ == "__main__":
    logging.info("=====================================================")
    logging.info(" Jai Electricals Bulletproof Daemon Started")
    logging.info(f" Operating Hours: {START_TIME.strftime('%I:%M %p')} to {END_TIME.strftime('%I:%M %p')} | Mon-Sat")
    logging.info("=====================================================")
    
    # 3. INFINITE LOOP WITH MASTER RECOVERY
    while True:
        try:
            now = datetime.now()
            current_time = now.time()
            
            if now.weekday() in WORK_DAYS:
                if START_TIME <= current_time <= END_TIME:
                    run_pipeline()
                    time.sleep(CHECK_INTERVAL_SECONDS)
                else:
                    logging.info("Outside business hours. Sleeping for 10 mins...")
                    time.sleep(600)
            else:
                logging.info("Day off. Sleeping for 1 hour...")
                time.sleep(3600)
                
        except KeyboardInterrupt:
            logging.info("Daemon stopped manually by User.")
            sys.exit(0)
        except Exception as e:
            # If the daemon itself crashes, it catches the error and restarts
            logging.error(f"CRITICAL DAEMON EXCEPTION: {e}")
            logging.info("Sleeping for 60 seconds before attempting auto-recovery...")
            time.sleep(60)