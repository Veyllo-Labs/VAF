import pyaudio

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
