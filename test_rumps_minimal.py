#!/usr/bin/env python3
"""Minimal rumps test - just show a tray icon, nothing else"""
import sys
import os

# Set PYTHONPATH to find rumps
sys.path.insert(0, '/opt/homebrew/lib/python3.11/site-packages')

print("Starting minimal rumps test...")
print(f"Python: {sys.executable}")

try:
    import rumps
    print(f"✅ rumps imported from: {rumps.__file__}")
    
    class MinimalApp(rumps.App):
        def __init__(self):
            super(MinimalApp, self).__init__("TEST", icon=None)
            self.menu = ["Item 1", "Item 2"]
    
    print("Creating app...")
    app = MinimalApp()
    print("Calling app.run()...")
    app.run()
    print("app.run() returned (this should never print if RunLoop is running)")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
