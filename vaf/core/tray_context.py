
import time
import threading
from typing import Dict, Any, Optional
from vaf.core.config import Config

class TrayContext:
    """
    Shared state between System Tray App, Web Server, and CLI Agent.
    Manages the 'Activity State' used to decide when to load/unload the model.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TrayContext, cls).__new__(cls)
                    cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "initialized", False):
            return
            
        self.initialized = True
        
        # Configuration
        # Load timeout from config, default to 10 seconds
        self.idle_timeout = Config.get("server_idle_timeout", 10)
        
        # State
        self.active_websockets = 0
        self.last_heartbeat = 0.0
        self.model_loaded = False
        self.server_port = 8001
        self.should_exit = False
        
        # Callbacks (for Tray App to update UI)
        self.on_status_change = None

    def register_activity(self):
        """Register any activity (heartbeat, websocket message, etc)."""
        self.last_heartbeat = time.time()

    def set_websocket_count(self, count: int):
        """Update active websocket count."""
        old_count = self.active_websockets
        self.active_websockets = count
        if count > 0:
            self.register_activity()
        
        # If transitioning from 0 to >0, we might need to wake up
        if old_count == 0 and count > 0:
            pass # The loop in tray.py checks is_active()

    def is_active(self) -> bool:
        """
        Check if the system is currently 'Active'.
        Active means:
        1. At least one WebSocket connection is open (WebUI)
        OR
        2. A heartbeat was received recently (CLI)
        """
        # 1. WebSocket Connections
        if self.active_websockets > 0:
            return True
        
        # 2. Recent Heartbeat (within timeout window + buffer)
        # We give a small buffer (e.g. 2s) to avoid flickering
        if time.time() - self.last_heartbeat < (self.idle_timeout + 2.0):
            return True
            
        return False

    def is_persistent(self) -> bool:
        """Check if persistent mode is enabled in Config."""
        return Config.get("persist_server", False)
    
    def set_persistent(self, enabled: bool):
        """Update persistent mode setting."""
        Config.set("persist_server", enabled)
        # Notify listener
        if self.on_status_change:
            self.on_status_change()

    def set_model_loaded(self, loaded: bool):
        """Update model loaded state."""
        if self.model_loaded != loaded:
            self.model_loaded = loaded
            # Notify listener
            if self.on_status_change:
                self.on_status_change()
