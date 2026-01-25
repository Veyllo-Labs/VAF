import logging
import sys
from vaf.tools.sandbox import DockerSandbox

# Configure logging to see what's happening
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def test_sandbox():
    print("=== Testing Docker Sandbox ===")
    
    # Use 'context manager' syntax for auto-cleanup
    with DockerSandbox() as box:
        # 1. Check OS inside container
        print("\n[Test 1] Check OS Release:")
        code, out, err = box.execute("cat /etc/os-release | grep PRETTY_NAME")
        if code == 0:
            print(f"✅ Success: {out.strip()}")
        else:
            print(f"❌ Failed: {err}")

        # 2. Python execution
        print("\n[Test 2] Python Math:")
        code, out, err = box.execute("python3 -c 'print(100 + 456)'")
        if code == 0 and "556" in out:
            print(f"✅ Success: {out.strip()}")
        else:
            print(f"❌ Failed: {out} {err}")

        # 3. File I/O
        print("\n[Test 3] File I/O:")
        test_file = "/tmp/hello.txt"
        test_content = "Hello from VAF Host!"
        
        try:
            box.write_file(test_file, test_content)
            read_back = box.read_file(test_file)
            if read_back.strip() == test_content:
                print(f"✅ Success: File content matches '{read_back.strip()}'")
            else:
                print(f"❌ Failed: Read '{read_back}' != '{test_content}'")
        except Exception as e:
             print(f"❌ Error: {e}")

        # 4. Timeout Test
        print("\n[Test 4] Timeout (expecting failure after 2s):")
        code, _, err = box.execute("sleep 5", timeout=2)
        if "timed out" in err:
            print(f"✅ Success: Caught timeout correctly.")
        else:
            print(f"❌ Failed: Did not timeout properly. Code: {code}")

    print("\n=== Test Finished (Container should be gone) ===")

if __name__ == "__main__":
    test_sandbox()
