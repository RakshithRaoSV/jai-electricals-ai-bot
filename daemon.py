import time
import subprocess
from datetime import datetime
import sys

def run_bot():
    print("=== Jai Electricals 24/7 AI Engine Started ===")
    print("Press Ctrl+C to stop the bot.\n")
    
    while True:
        current_time = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        print(f"[{current_time}] Waking up to check emails & sheets...")
        
        try:
            # 1. Run the Ingestion Engine
            subprocess.run([sys.executable, "main.py"], check=True)
            
            # 2. Run the Quote Generator & Archiver
            subprocess.run([sys.executable, "quote_manager.py"], check=True)
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle complete. Going to sleep for 5 minutes.")
            
        except subprocess.CalledProcessError as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] A script encountered an error. Retrying next cycle. Error: {e}")
        except KeyboardInterrupt:
            print("\nBot shutting down gracefully. Goodbye!")
            break
            
        # Sleep for 300 seconds (5 minutes)
        time.sleep(300)

if __name__ == '__main__':
    run_bot()