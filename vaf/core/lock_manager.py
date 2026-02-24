"""
VAF Lock Manager - Prevents parallel execution of same tasks
Handles file-based locks for Automations and Thinking Mode.
"""
import os
import time
import json
from pathlib import Path
from typing import Optional
from vaf.core.platform import Platform

class LockManager:
    """Manages file-based locks to prevent redundant task execution."""
    
    LOCK_DIR = Platform.vaf_dir() / "locks"
    
    @staticmethod
    def _ensure_lock_dir():
        """Ensure the locks directory exists."""
        LockManager.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def acquire(lock_id: str, timeout_hours: float = 2.0) -> bool:
        """
        Attempt to acquire a lock.
        
        Args:
            lock_id: Unique identifier for the lock (e.g. 'automation_abc123')
            timeout_hours: If lock is older than this, it's considered stale.
            
        Returns:
            True if lock acquired, False if already locked and active.
        """
        LockManager._ensure_lock_dir()
        lock_file = LockManager.LOCK_DIR / f"{lock_id}.lock"
        
        now = time.time()
        
        if lock_file.exists():
            try:
                # Load lock data to check PID
                data = json.loads(lock_file.read_text(encoding="utf-8"))
                pid = data.get("pid", 0)
                
                # Check if process is still alive
                is_alive = Platform.is_process_running(pid) if pid > 0 else False
                
                if not is_alive:
                    # Process is dead, override lock
                    from vaf.core.log_helper import append_domain_log_always
                    append_domain_log_always("backend", f"[LOCK] Overriding orphaned lock (Process {pid} dead): {lock_id}")
                else:
                    # Check for stale lock by time
                    mtime = lock_file.stat().st_mtime
                    age_hours = (now - mtime) / 3600.0
                    
                    if age_hours > timeout_hours:
                        # Lock is stale, override it
                        from vaf.core.log_helper import append_domain_log_always
                        append_domain_log_always("backend", f"[LOCK] Overriding stale lock: {lock_id} (age: {age_hours:.1f}h)")
                    else:
                        # Active lock exists and process is alive
                        return False
            except Exception:
                # Fallback if parsing fails (treat as invalid lock)
                pass
        
        try:
            # Create/update lock file with PID
            lock_data = {
                "pid": os.getpid(),
                "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lock_id": lock_id
            }
            lock_file.write_text(json.dumps(lock_data), encoding="utf-8")
            return True
        except Exception:
            return False

    @staticmethod
    def release(lock_id: str):
        """Release a lock by deleting the lock file."""
        lock_file = LockManager.LOCK_DIR / f"{lock_id}.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

    @staticmethod
    def is_locked(lock_id: str, timeout_hours: float = 2.0) -> bool:
        """Check if a lock is currently active and the process is alive."""
        lock_file = LockManager.LOCK_DIR / f"{lock_id}.lock"
        if not lock_file.exists():
            return False
            
        try:
            # Load lock data to check PID
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = data.get("pid", 0)
            
            # Check if process is still alive
            is_alive = Platform.is_process_running(pid) if pid > 0 else False
            if not is_alive:
                return False
                
            # Check for stale lock by time
            mtime = lock_file.stat().st_mtime
            age_hours = (time.time() - mtime) / 3600.0
            return age_hours <= timeout_hours
        except Exception:
            return False
