import rumps
import time
import os
import sys

# Point to the icon we verified exists
icon_path = os.path.expanduser("~/.vaf/icons/tray_v2_idle.png")
print(f"Icon path: {icon_path}")
print(f"Exists: {os.path.exists(icon_path)}")

class TestApp(rumps.App):
    def __init__(self):
        super(TestApp, self).__init__("TestApp", icon=icon_path)
        self.menu = ["Test Item", "Quit"]

    @rumps.clicked("Test Item")
    def test_item(self, _):
        print("Item clicked")

if __name__ == "__main__":
    print("Starting Rumps Test App...")
    TestApp().run()
