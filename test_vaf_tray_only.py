#!/usr/bin/env python3
"""Test VAF tray icon WITHOUT backend/frontend threads"""
import sys
import os

# Set environment
sys.path.insert(0, '/Users/m.c.elsner/VAF')
sys.path.insert(0, '/Users/m.c.elsner/VAF/venv/lib/python3.11/site-packages')
os.chdir('/Users/m.c.elsner/VAF')

print("Starting VAF Tray (NO backend/frontend)...")

# Import and patch
from vaf.tray import VafTrayApp, get_icon_path
import rumps

print(f"Icon path: {get_icon_path('idle')}")

class TestVafTray(rumps.App):
    def __init__(self):
        icon_path = get_icon_path("idle")
        print(f"Initializing with icon: {icon_path}")
        super(TestVafTray, self).__init__("VAF-TEST", icon=icon_path)
        self.menu = ["Test Item 1", "Test Item 2"]

print("Creating app...")
app = TestVafTray()
print("Running app...")
app.run()
