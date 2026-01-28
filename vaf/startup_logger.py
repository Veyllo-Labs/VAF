import os
import datetime
import sys

LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "startup_trace.txt")

def log(component, message):
    """Writes a log entry to the central startup trace file."""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [{component}] {message}\n"
        
        # Write to file immediately (flush)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
            
        # Also print to console for CLI visibility
        print(f"DEBUG: {entry.strip()}")
    except Exception as e:
        print(f"!!! FAILED TO WRITE LOG: {e}")

def clear_log():
    """Clears the log file at startup."""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"--- STARTUP TRACE STARTED AT {datetime.datetime.now()} ---\n")
            f.write(f"Python: {sys.executable}\n")
            f.write(f"Platform: {sys.platform}\n")
    except:
        pass
