
import subprocess
import platform

def check_port(port):
    print(f"Checking port {port}...")
    try:
        cmd = f"netstat -ano | findstr :{port}"
        print(f"Running: {cmd}")
        output = subprocess.check_output(cmd, shell=True).decode()
        print("Output:")
        print(output)
        
        for line in output.splitlines():
            if "LISTENING" in line:
                parts = line.strip().split()
                print(f"Line parts: {parts}")
                if len(parts) > 4:
                    pid = parts[-1]
                    print(f"Found PID: {pid}")
    except subprocess.CalledProcessError:
        print("No process found (netstat returned non-zero)")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_port(3000)
    check_port(3001)
