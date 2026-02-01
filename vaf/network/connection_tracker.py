"""
Connection Tracker for VAF Network Topology.

Tracks active WebSocket and HTTP connections to visualize the network map.
Stores active sessions in memory.
"""

import logging
import time
import threading
from typing import Dict, Optional, List
from enum import Enum
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

class ConnectionType(str, Enum):
    WEBSOCKET = "websocket"
    HTTP = "http"

class DeviceType(str, Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"
    TABLET = "tablet"
    SERVER = "server"
    UNKNOWN = "unknown"

@dataclass
class ConnectionInfo:
    id: str
    type: ConnectionType
    ip: str
    connected_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    username: Optional[str] = None
    role: Optional[str] = None
    device_type: DeviceType = DeviceType.UNKNOWN
    user_agent: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

def detect_device_type(user_agent: str) -> DeviceType:
    if not user_agent:
        return DeviceType.UNKNOWN
    ua = user_agent.lower()
    if "mobile" in ua:
        return DeviceType.MOBILE
    if "tablet" in ua or "ipad" in ua:
        return DeviceType.TABLET
    if "linux" in ua or "windows" in ua or "macintosh" in ua:
        return DeviceType.DESKTOP
    return DeviceType.UNKNOWN

class ConnectionTracker:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ConnectionTracker, cls).__new__(cls)
                cls._instance.connections = {}  # Dict[str, ConnectionInfo]
                cls._instance._start_cleanup_thread()
        return cls._instance

    def _start_cleanup_thread(self):
        def cleanup_loop():
            while True:
                time.sleep(30)
                self._cleanup_stale()
        
        t = threading.Thread(target=cleanup_loop, daemon=True, name="ConnectionTrackerCleanup")
        t.start()

    def _cleanup_stale(self):
        """Remove connections inactive for > 5 minutes (except WebSockets which stay open)."""
        now = time.time()
        to_remove = []
        with self._lock:
            for cid, info in self.connections.items():
                timeout = 300 # 5 mins default
                if info.type == ConnectionType.WEBSOCKET:
                    timeout = 3600 * 24 # 24 hours for WS (they maintain heartbeat usually)
                
                if now - info.last_active > timeout:
                    to_remove.append(cid)
            
            for cid in to_remove:
                del self.connections[cid]

    def register_connection(
        self, 
        connection_id: str, 
        connection_type: ConnectionType, 
        ip: str, 
        device_type: DeviceType = DeviceType.UNKNOWN,
        user_agent: str = None,
        username: str = None,
        service_name: str = "WebUI", # unused but kept for compatibility
        metadata: dict = None
    ):
        with self._lock:
            self.connections[connection_id] = ConnectionInfo(
                id=connection_id,
                type=connection_type,
                ip=ip,
                device_type=device_type,
                user_agent=user_agent,
                username=username,
                metadata=metadata or {}
            )
            # logger.info(f"New connection tracked: {connection_id} ({ip})")

    def unregister_connection(self, connection_id: str):
        with self._lock:
            if connection_id in self.connections:
                del self.connections[connection_id]

    def update_activity(self, connection_id: str):
        with self._lock:
            if connection_id in self.connections:
                self.connections[connection_id].last_active = time.time()

    def get_active_connections(self) -> List[dict]:
        with self._lock:
            return [asdict(c) for c in self.connections.values()]

def get_tracker():
    return ConnectionTracker()
