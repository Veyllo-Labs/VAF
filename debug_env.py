#!/usr/bin/env python3
"""Debug script to compare environment between terminal and app bundle"""
import sys
import os
import platform

print("=" * 60)
print("ENVIRONMENT DEBUG")
print("=" * 60)
print(f"Python Executable: {sys.executable}")
print(f"Python Version: {sys.version}")
print(f"sys.argv[0]: {sys.argv[0]}")
print(f"__file__: {__file__}")
print(f"CWD: {os.getcwd()}")
print(f"Platform: {platform.platform()}")
print(f"")
print("CRITICAL ENV VARS:")
print(f"  PATH: {os.environ.get('PATH', 'NOT SET')[:200]}")
print(f"  PYTHONPATH: {os.environ.get('PYTHONPATH', 'NOT SET')}")
print(f"  VIRTUAL_ENV: {os.environ.get('VIRTUAL_ENV', 'NOT SET')}")
print(f"  LANG: {os.environ.get('LANG', 'NOT SET')}")
print(f"  HOME: {os.environ.get('HOME', 'NOT SET')}")
print(f"")

# Check if we can import rumps
try:
    import rumps
    print(f"✅ rumps imported successfully from: {rumps.__file__}")
    
    # Try to create a minimal rumps app
    class TestApp(rumps.App):
        def __init__(self):
            super(TestApp, self).__init__("TEST", icon=None, quit_button=None)
    
    print("✅ rumps.App class instantiated successfully")
    
    # Check NSApplication
    try:
        from AppKit import NSApplication, NSApp
        app = NSApplication.sharedApplication()
        print(f"✅ NSApplication: {app}")
        print(f"   isActive: {app.isActive()}")
        print(f"   activationPolicy: {app.activationPolicy()}")
    except Exception as e:
        print(f"❌ NSApplication check failed: {e}")
        
except Exception as e:
    print(f"❌ rumps import/test failed: {e}")
    import traceback
    traceback.print_exc()

print("=" * 60)
