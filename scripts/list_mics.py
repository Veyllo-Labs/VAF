# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import sys

try:
    import pyaudio
except ImportError:
    # pyaudio is the optional vaf[speech] extra, not a core dependency (no wheels for
    # brand-new Pythons; the source build needs the portaudio C headers).
    print("pyaudio is not installed - it is part of the optional speech extra.")
    print('Install it with:  pip install pyaudio   (or:  pip install "vaf[speech]")')
    sys.exit(1)

def list_microphones():
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    numdevices = info.get('deviceCount')
    
    unique_devices = {}

    for i in range(0, numdevices):
        if (p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels')) > 0:
            device_name = p.get_device_info_by_host_api_device_index(0, i).get('name')
            
            # Filter out obvious duplicates or unwanted devices here if needed
            # For now, just listing all input devices found
            if device_name not in unique_devices:
                unique_devices[device_name] = i
                print(f"Device ID {i}: {device_name}")

    p.terminate()

if __name__ == "__main__":
    list_microphones()
