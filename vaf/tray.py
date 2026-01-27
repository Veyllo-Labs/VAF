
import os
import sys
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
    """Ensure only one instance of the tray app runs."""
    import socket
    try:
        # Try to bind to a specific port to ensure singleton
        # We use 8002 for the lock (8001 is web server, 8080 is LLM)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 8002))
        return s
    except socket.error:
        print("Tray app is already running.")
        return None

def start_uvicorn():
    """Start uvicorn server in a separate thread."""
    try:
        uvicorn.run(app, host="127.0.0.1", port=8001, log_level="error")
    except Exception as e:
        print(f"Web server failed: {e}")

    except Exception as e:
        print(f"Web server failed: {e}")

def get_icon_path(status):
    """Generate and return path to an icon for the given status."""
    if not Image: return None
    
    # Define colors
    colors = {
        "active": (46, 204, 113),  # Green
        "idle": (241, 196, 15),    # Yellow
        "persistent": (52, 152, 219) # Blue
    }
    color = colors.get(status, (128, 128, 128))
    
    # Ensure dir exists
    icon_dir = Path(Config.load().get("vaf_dir", os.path.expanduser("~/.vaf"))) / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    
    filename = icon_dir / f"tray_{status}.png"
    if not filename.exists():
        # Create 64x64 icon
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Draw circle
        draw.ellipse((8, 8, 56, 56), fill=color)
        img.save(filename)
        
    return str(filename)

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
    from vaf.core.frontend_manager import FrontendManager
    fm = FrontendManager()
    
    # Check/Start frontend
    port = fm.start_frontend()
    if port:
        webbrowser.open(f"http://localhost:{port}")
    else:
        print("Failed to start Web UI")

def toggle_persistence(item):
    new_state = not tray_context.is_persistent()
    tray_context.set_persistent(new_state)
    item.state = new_state # Update menu checkmark (if supported)

def quit_app(icon_or_app):
    """Handle quit action with safety check."""
    # Check if CLI is running (heartbeat active)
    if tray_context.active_websockets > 0 or (time.time() - tray_context.last_heartbeat < 30):
        # We can't show a native dialog easily cross-platform without blocking
        # But rumps has alert, pystray doesn't.
        # For now, we print to console and just exit, or we could use tkinter/osascript for dialogs.
        # Since requirements asked for confirmation:
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
        print("[Tray] run_app called (Pystray)")
        # Singleton Check
        lock_socket = check_singleton()
        if not lock_socket:
            print("[Tray] Singleton check failed (another instance running)")
            return

        print("[Tray] Singleton check passed")

        # Start Web Server
        print("[Tray] Starting Web Server thread...")
        t = threading.Thread(target=start_uvicorn, daemon=True)
        t.start()

        # Start Headless Agent Loop (for Web UI processing)
        print("[Tray] Starting Agent thread...")
        from vaf.core.headless_runner import run_headless_agent
        t_agent = threading.Thread(target=run_headless_agent, daemon=True)
        t_agent.start()

        # Start Frontend (Next.js) automatically
        def start_frontend_bg():
            print("[Tray] Starting Frontend manager...")
            from vaf.core.frontend_manager import FrontendManager
            port = FrontendManager().start_frontend()
            if port:
                print(f"[Tray] Frontend started on port {port}, opening browser...")
                import webbrowser
                webbrowser.open(f"http://localhost:{port}")
            else:
                print("[Tray] Frontend failed to start.")
        print("[Tray] Starting Frontend thread...")
        t_fe = threading.Thread(target=start_frontend_bg, daemon=True)
        t_fe.start()

        # Menu
        print("[Tray] Initializing Pystray icon...")
        menu = pystray.Menu(
            pystray.MenuItem("Status: Idle", lambda icon, item: None, enabled=False),
            pystray.MenuItem("Open WebUI", open_webui, default=True),
            pystray.MenuItem("Persistent Server", toggle_persistence, checked=lambda item: tray_context.is_persistent()),
            pystray.MenuItem("Quit", quit_app)
        )

        icon = pystray.Icon("VAF", create_image("yellow"), "VAF Agent", menu)

        def update_icon(state):
            if state == "active":
                icon.icon = create_image("green")
                # Update title? Pystray items are immutable-ish, need specific update method if supported
            elif state == "idle":
                icon.icon = create_image("yellow")
            elif state == "persistent":
                icon.icon = create_image("blue")

        # Logic Thread
        t_logic = threading.Thread(target=check_activity_loop, args=(update_icon,), daemon=True)
        t_logic.start()

        icon.run()

if __name__ == "__main__":
    run_app()
