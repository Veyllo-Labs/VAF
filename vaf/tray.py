
import os
import sys

# CRITICAL FIX: Patch stdout/stderr/stdin IMMEDIATELY for pythonw (no console)
# This prevents crashes in logging/uvicorn which assume sys.stdout exists.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
if sys.stdin is None:
    sys.stdin = open(os.devnull, 'r')

import time
import threading
import signal
import platform
import webbrowser
from pathlib import Path
from vaf.core.config import Config
from vaf.core.backend import ServerManager
from vaf.core.web_server import app
from vaf.core.tray_context import TrayContext
import uvicorn
from vaf.startup_logger import log, clear_log
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


import logging

# Configure Logging
log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "tray_debug.log")
logger = logging.getLogger("VAF_Tray")
logger.setLevel(logging.DEBUG)
# Force file handler
fh = logging.FileHandler(log_file)
fh.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
logger.addHandler(fh)

# Also add to root logger so we catch other errors?
# No, let's keep it specific to avoid noise, but maybe add root error capture.
logging.getLogger().addHandler(fh)

# Global state
server_mgr = ServerManager()
tray_context = TrayContext()
server_thread = None

def check_singleton():
    """Ensure only one instance runs. If another instance is running, notify it to open browser."""
    log("Tray", "Checking singleton status...")
    import socket
    try:
        # Try to bind to port 8002 to ensure singleton
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        # Windows: DO NOT use SO_REUSEADDR for singleton checks as it allows multiple binds.
        # Unix: SO_REUSEADDR is fine/needed to restart quickly.
        if platform.system() != "Windows":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
        s.bind(("127.0.0.1", 8002))
        s.listen(5)
        log("Tray", "Singleton check passed (Port 8002 bound)")
        return s
    except socket.error as e:
        log("Tray", f"Singleton check failed: {e}")
        # Port is busy, another instance is running.
        # Try to notify it.
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(("127.0.0.1", 8002))
            client.sendall(b"ACTIVATE")
            client.close()
            logger.info("[Tray] Sent ACTIVATE signal to existing instance.")
            log("Tray", "Sent ACTIVATE signal to existing instance")
        except Exception as e:
            logger.error(f"[Tray] Failed to notify existing instance: {e}")
            log("Tray", f"Failed to notify existing instance: {e}")
        
        print("VAF is already running. Notifying existing instance...")
        return None

def command_listener(lock_socket):
    """Listens for 'ACTIVATE' signals from other instances."""
    log("Tray", "Starting command listener thread")
    while not tray_context.should_exit:
        try:
            lock_socket.settimeout(1.0)
            conn, addr = lock_socket.accept()
            with conn:
                data = conn.recv(1024)
                if b"ACTIVATE" in data:
                    logger.info("[Tray] Received ACTIVATE signal. Opening Web UI.")
                    log("Tray", "Received ACTIVATE signal")
                    # Run opening in a thread to not block the listener
                    threading.Thread(target=open_webui, args=(None,), daemon=True).start()
        except socket.timeout:
            continue
        except Exception as e:
            if not tray_context.should_exit:
                logger.error(f"[Tray] Command listener error: {e}")
                log("Tray", f"Command listener error: {e}")
            break

def start_uvicorn():
    """Start uvicorn server in a separate thread."""
    log("Tray", "start_uvicorn thread started")
    try:
        import asyncio
        import pythoncom
        import sys
        import os

        # Ensure UTF-8 output for background threads (prevents Unicode log crashes)
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

        # CRITICAL FIX: Patch stdout/stderr/stdin if running in pythonw (no console)
        # Uvicorn crashes if sys.stdout is None because it checks .isatty()
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w')
        if sys.stdin is None:
            sys.stdin = open(os.devnull, 'r')

        pythoncom.CoInitialize()
        
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log("Tray", "Event loop created for Uvicorn")
        
        # Check if port 8001 is available
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 8001))
        sock.close()
        if result == 0:
            log("Tray", "CRITICAL WARNING: Port 8001 is ALREADY IN USE! Server will likely fail.")
        else:
            log("Tray", "Port 8001 is free.")

        print("[Tray] Starting Uvicorn thread on port 8001 (0.0.0.0)...")
        log("Tray", "Initializing Uvicorn Config (0.0.0.0:8001)...")
        
        # Listen on 0.0.0.0 to support both IPv4 (127.0.0.1) and IPv6 (::1/localhost)
        # log_level="info" to see startup errors
        # use_colors=False to avoid further isatty checks
        config = uvicorn.Config(app, host="0.0.0.0", port=8001, log_level="info", use_colors=False)
        server = uvicorn.Server(config)
        
        # Manually disable signal handlers to prevent main thread interference
        server.install_signal_handlers = lambda: None
        
        # Force reset exit flag in case it was set by signal handlers during init
        server.should_exit = False
        
        log("Tray", "Running Uvicorn server (serve)...")
        try:
            loop.run_until_complete(server.serve())
        except Exception as serve_error:
            log("Tray", f"Uvicorn serve() failed: {serve_error}")
            raise serve_error
            
        log("Tray", "Uvicorn server stopped gracefully (loop ended)")
        log("Tray", "Uvicorn server stopped gracefully")
        
    except Exception as e:
        print(f"Web server thread failed: {e}")
        logger.error(f"Web server failed: {e}")
        log("Tray", f"CRITICAL: Web server thread crashed: {e}")
        import traceback
        log("Tray", traceback.format_exc())

def get_icon_path(status):
    """Generate and return path to an icon for the given status using the VAF logo."""
    if not Image: return None
    
    # Define colors
    colors = {
        "active": (46, 204, 113),  # Green
        "idle": (241, 196, 15),    # Yellow
        "persistent": (231, 76, 60) # Red
    }
    color = colors.get(status, (128, 128, 128))
    
    # Ensure dir exists
    vaf_dir = Path(Config.load().get("vaf_dir", os.path.expanduser("~/.vaf")))
    icon_dir = vaf_dir / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    
    filename = icon_dir / f"tray_v2_{status}.png"
    
    # Base logo path
    base_dir = Path(__file__).parent.parent
    logo_path = base_dir / "vaf" / "media" / "logo_original.png"

    if not filename.exists() or True: # Force recreate for now to see changes
        try:
            if logo_path.exists():
                # Load logo
                img_src = Image.open(logo_path).convert("RGBA")
                
                # Autocrop: Remove transparent borders
                # We use a small threshold to catch nearly-transparent pixels
                bbox = img_src.getbbox()
                if bbox:
                    img_src = img_src.crop(bbox)
                
                # Create 64x64 canvas
                img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                
                # Resize logo to fill the ENTIRE canvas (64x64)
                img_src.thumbnail((64, 64), Image.Resampling.LANCZOS)
                
                # Center the logo on the canvas
                offset = ((64 - img_src.width) // 2, (64 - img_src.height) // 2)
                img.paste(img_src, offset, img_src)
            else:
                # Fallback to transparent canvas
                img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            
            draw = ImageDraw.Draw(img)
            
            # Draw a small indicator circle in the bottom right corner
            # Smaller indicator to not cover the now even larger logo
            draw.ellipse((46, 46, 64, 64), fill=(255, 255, 255, 255)) 
            draw.ellipse((48, 48, 62, 62), fill=color)
            
            img.save(filename)
        except Exception as e:
            print(f"Failed to create icon: {e}")
            return None
        
    return str(filename)

def check_activity_loop(update_icon_callback):
    """Monitor activity and manage model state."""
    while not tray_context.should_exit:
        is_active = tray_context.is_active()
        is_loaded = tray_context.model_loaded
        is_persistent = tray_context.is_persistent()
        
        # ACTIVE STATE
        if is_active:
            if not is_loaded:
                # Load Model
                print("Activity detected. Loading model...")
                update_icon_callback("active")
                # We start server using configured model
                model = Config.get("model")
                if server_mgr.start_server(model_path=server_mgr.get_model_path(model), port=8080):
                    tray_context.set_model_loaded(True)
                else:
                    print("Failed to start server")
            else:
                # Already loaded, ensure icon is green
                update_icon_callback("active")
                
        # IDLE STATE
        else:
            if is_loaded and not is_persistent:
                # Check timeout
                time_since_last = time.time() - tray_context.last_heartbeat
                if time_since_last > tray_context.idle_timeout:
                    print(f"Idle timeout ({tray_context.idle_timeout}s) reached. Unloading model...")
                    server_mgr.stop_server(force_external=True) # We own it effectively here
                    tray_context.set_model_loaded(False)
                    update_icon_callback("idle")
            else:
                # Already unloaded or persistent
                update_icon_callback("persistent" if is_persistent else "idle")
                
        time.sleep(1)

def open_webui(_):
    log("Tray", "open_webui called")
    from vaf.core.frontend_manager import FrontendManager
    fm = FrontendManager()
    
    # Check/Start frontend
    port = fm.start_frontend()
    if not port:
        log("Tray", "open_webui: Failed to start/find Web UI port")
        return

    url = f"http://localhost:{port}"
    log("Tray", f"open_webui: Target URL is {url}")
    
    if platform.system() == "Windows":
        try:
            import win32gui
            import win32con
            
            def window_enum_handler(hwnd, ctx):
                title = win32gui.GetWindowText(hwnd)
                # Check for "VAF" and browser indicators
                if "VAF" in title and ("Google Chrome" in title or "Firefox" in title or "Edge" in title or "Browser" in title):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                    ctx['found'] = True

            context = {'found': False}
            win32gui.EnumWindows(window_enum_handler, context)
            
            if context['found']:
                log("Tray", "open_webui: Existing window found and focused")
                return
        except Exception as e:
            log("Tray", f"open_webui: Windows focus check failed: {e}")

    if platform.system() == "Darwin":
        # ... (macOS logic stays same) ...
        pass

    # Fallback: Open new tab
    log("Tray", "open_webui: Opening new browser tab...")
    try:
        import webbrowser
        # Try standard way
        if not webbrowser.open(url):
            raise Exception("webbrowser.open returned False")
    except Exception as e:
        log("Tray", f"open_webui: Standard webbrowser.open failed: {e}. Trying shell fallback...")
        if platform.system() == "Windows":
            try:
                # Most reliable way on Windows to open a URL from background process
                subprocess.Popen(["cmd", "/c", "start", url], shell=True)
            except Exception as e2:
                log("Tray", f"open_webui: Shell fallback failed: {e2}")

def toggle_persistence(item):
    new_state = not tray_context.is_persistent()
    tray_context.set_persistent(new_state)
    item.state = new_state # Update menu checkmark (if supported)

def quit_app(icon_or_app):
    """Handle quit action with safety check."""
    # Check if CLI is running (heartbeat active)
    if tray_context.active_websockets > 0 or (time.time() - tray_context.last_heartbeat < 30):
        pass 

    print("Shutting down...")
    tray_context.should_exit = True
    
    # Stop Web UI (Next.js)
    try:
        from vaf.core.frontend_manager import FrontendManager
        FrontendManager().stop_frontend()
    except Exception as e:
        print(f"Error stopping frontend: {e}")

    server_mgr.stop_server(force_external=True)
    os._exit(0)

# ==========================================
# macOS Implementation (Rumps)
# ==========================================
# ==========================================
# macOS Implementation (Rumps)
if platform.system() == "Darwin":
    try:
        logger.info("[Tray] Attempting to import rumps...")
        import rumps
        logger.info("[Tray] Rumps imported successfully.")

        class VafTrayApp(rumps.App):
            def __init__(self):
                # Simple Icon Logic
                icon_path = get_icon_path("idle")
                logger.info(f"[Tray] Init with icon: {icon_path}")
                
                # Init Rumps App
                super(VafTrayApp, self).__init__("VAF", icon=icon_path, quit_button=None)

                self.menu = [
                    rumps.MenuItem("Status: Idle", callback=None),
                    rumps.separator,
                    rumps.MenuItem("Open WebUI", callback=self.on_open_webui),
                    rumps.MenuItem("Persistent Server", callback=self.on_toggle_persist, key="P"),
                    rumps.separator,
                    rumps.MenuItem("Quit VAF", callback=self.on_quit)
                ]
                
                # Start Timer immediately
                self.timer = rumps.Timer(self.update_loop, 1)
                self.timer.start()

                # Robust macOS Dock Reopen Handler
                self._dock_timer = rumps.Timer(self.setup_mac_handlers, 1)
                self._dock_timer.start()

                self._last_open = 0

            def setup_mac_handlers(self, _):
                """Setup notification observers for macOS."""
                self._dock_timer.stop()
                try:
                    from AppKit import NSNotificationCenter, NSApplicationDidBecomeActiveNotification
                    from Foundation import NSObject
                    import objc

                    # We MUST use a real NSObject subclass to handle selectors reliably
                    class ActivationObserver(NSObject):
                        def onActivate_(self, notification):
                            # Debounce check
                            current_time = time.time()
                            if current_time - self.app_instance._last_open < 1.0:
                                return
                            
                            self.app_instance._last_open = current_time
                            logger.info("[Tray] App activation detected (Dock click/Focus).")
                            threading.Thread(target=open_webui, args=(None,), daemon=True).start()

                    self._observer_obj = ActivationObserver.alloc().init()
                    self._observer_obj.app_instance = self
                    
                    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                        self._observer_obj, 
                        objc.selector(self._observer_obj.onActivate_, signature=b"v@:@"),
                        NSApplicationDidBecomeActiveNotification, 
                        None
                    )
                    
                    logger.info("[Tray] Registered robust macOS Activation observer.")
                except Exception as e:
                    logger.error(f"[Tray] Failed to setup macOS observers: {e}")

            def update_loop(self, _):
                """Main logic loop called by timer."""
                try:
                    is_active = tray_context.is_active()
                    is_persistent = tray_context.is_persistent()
                    
                    status_text = "Persistent" if is_persistent else ("Active" if is_active else "Idle")
                    self.menu["Status: Idle"].title = f"Status: {status_text}"
                    self.menu["Persistent Server"].state = 1 if is_persistent else 0

                    # Icon Update Logic
                    target_status = "idle"
                    if is_persistent:
                        target_status = "persistent"
                    elif is_active:
                        target_status = "active"
                    
                    new_icon = get_icon_path(target_status)
                    if self.icon != new_icon and os.path.exists(new_icon):
                        self.icon = new_icon

                except Exception as e:
                    logger.error(f"Error in update loop: {e}")

            def on_open_webui(self, _):
                open_webui(None)

            def on_toggle_persist(self, sender):
                toggle_persistence(sender)
                
            def on_quit(self, _):
                # Check for active session
                if tray_context.is_active():
                    resp = rumps.alert("Active Session", "A VAF session is currently active. Are you sure you want to quit?", ok="Quit", cancel="Cancel")
                    if resp != 1:
                        return
                quit_app(self)

        def run_app():
            logger.info("[Tray] run_app (Rumps) called")
            # Singleton Check
            lock_socket = check_singleton()
            if not lock_socket:
                logger.warning("[Tray] Singleton check failed")
                return

            # Start Web Server
            logger.info("[Tray] Starting Web Server thread...")
            t = threading.Thread(target=start_uvicorn, daemon=True)
            t.start()
            
            # Start Headless Agent Loop (for Web UI processing)
            from vaf.core.headless_runner import run_headless_agent
            t_agent = threading.Thread(target=run_headless_agent, daemon=True)
            t_agent.start()
            
            # Start Frontend (Next.js) automatically
            def start_frontend_bg():
                logger.info("[Tray] Starting Frontend manager...")
                from vaf.core.frontend_manager import FrontendManager
                port = FrontendManager().start_frontend()
                if port:
                    logger.info(f"[Tray] Frontend started on port {port}, opening browser...")
                    import webbrowser
                    webbrowser.open(f"http://localhost:{port}")
            t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
            t_fe.start()
            
            # Start App
            logger.info("Initializing VafTrayApp().run()")
            
            # Start Command Listener thread
            t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
            t_cmd.start()
            
            VafTrayApp().run()

    except ImportError:
        # Fallback to pystray if rumps is not available
        pass

# ==========================================
# Cross-Platform Implementation (Pystray)
# ==========================================
if platform.system() != "Darwin" or "rumps" not in sys.modules:
    import pystray
    from PIL import Image, ImageDraw

    def create_image(color_name):
        # Map color names to tuples/strings that PIL accepts if needed, or pass through
        # But we want to match get_icon_path logic or reuse it
        # Pystray wants an Image object, not a path
        path = get_icon_path(color_name)
        if path:
            return Image.open(path)
        return Image.new('RGB', (64, 64), 'red') # Fallback

    def run_app():
        clear_log()
        log("Tray", "run_app called (Pystray)")
        print("[Tray] run_app called (Pystray)")
        # Singleton Check
        lock_socket = check_singleton()
        if not lock_socket:
            print("[Tray] Singleton check failed (another instance running)")
            log("Tray", "Singleton check failed - aborting")
            return

        print("[Tray] Singleton check passed")
        log("Tray", "Singleton check passed")

        # Initialize SpeechManager on Main Thread to avoid COM issues
        try:
            log("Tray", "Initializing SpeechManager (Main Thread)...")
            from vaf.core.speech import SpeechManager
            SpeechManager.get_instance()
            log("Tray", "SpeechManager initialized")
        except Exception as e:
            log("Tray", f"SpeechManager init failed: {e}")

        # Start Web Server
        print("[Tray] Starting Web Server thread...")
        log("Tray", "Spawning Web Server thread...")
        t = threading.Thread(target=start_uvicorn, daemon=True)
        t.start()

        # Start Headless Agent Loop (for Web UI processing)
        print("[Tray] Starting Agent thread...")
        log("Tray", "Spawning Agent thread...")
        from vaf.core.headless_runner import run_headless_agent
        t_agent = threading.Thread(target=run_headless_agent, daemon=True)
        t_agent.start()

        # Start Frontend (Next.js) automatically
        def start_frontend_bg():
            print("[Tray] Starting Frontend manager...")
            log("Tray", "Starting Frontend Manager...")
            from vaf.core.frontend_manager import FrontendManager
            port = FrontendManager().start_frontend(log_callback=lambda msg, style: log("Frontend", msg))
            if port:
                print(f"[Tray] Frontend started on port {port}, opening browser...")
                log("Tray", f"Frontend started on port {port}")
                open_webui(None)
            else:
                print("[Tray] Frontend failed to start.")
                log("Tray", "Frontend failed to start")
        print("[Tray] Starting Frontend thread...")
        t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
        t_fe.start()

        # Menu
        print("[Tray] Initializing Pystray icon...")
        log("Tray", "Initializing System Tray Icon...")
        
        menu = pystray.Menu(
            pystray.MenuItem("Status: Idle", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Open WebUI", open_webui, default=True),
            pystray.MenuItem("Persistent Server", toggle_persistence, checked=lambda item: tray_context.is_persistent()),
            pystray.MenuItem("Quit", quit_app)
        )

        icon = pystray.Icon("VAF", create_image("idle"), "VAF Agent", menu)

        def update_icon(state):
            if state == "active":
                icon.icon = create_image("active")
            elif state == "idle":
                icon.icon = create_image("idle")
            elif state == "persistent":
                icon.icon = create_image("persistent")
        
        # Logic Thread
        log("Tray", "Starting Activity Logic thread...")
        t_logic = threading.Thread(target=check_activity_loop, args=(update_icon,), daemon=True)
        t_logic.start()

        # Command Listener thread (for singleton activation)
        log("Tray", "Starting Command Listener thread...")
        t_cmd = threading.Thread(target=command_listener, args=(lock_socket,), daemon=True)
        t_cmd.start()

        log("Tray", "Entering Pystray main loop (icon.run)")
        icon.run()

if __name__ == "__main__":
    run_app()
