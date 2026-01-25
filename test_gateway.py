import time
import multiprocessing
import uvicorn
import asyncio
import websockets
import json
import sys
from vaf.core.gateway import app

def start_server():
    """Runs the Uvicorn server."""
    print("[Server] Starting on port 8000...")
    sys.stdout.flush()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

async def run_client():
    """Connects to the server and sends a message."""
    uri = "ws://127.0.0.1:8000/ws/test_client_1?client_type=test_script"
    print(f"[Client] Connecting to {uri}...")
    
    try:
        async with websockets.connect(uri) as websocket:
            print("[Client] Connected!")
            
            # Send a prompt
            msg = {
                "id": "123",
                "source": "test_script",
                "type": "agent.prompt",
                "payload": {"text": "Hello VAF Gateway!"}
            }
            print(f"[Client] Sending: {msg}")
            await websocket.send(json.dumps(msg))

            # Listen for responses
            for _ in range(3): # Listen for a few messages (Connect ACK, Status, Response)
                response = await websocket.recv()
                print(f"[Client] Received: {response}")
                
    except Exception as e:
        print(f"[Client] Error: {e}")

def start_client_process():
    # Wait a bit for server to start
    time.sleep(2) 
    asyncio.run(run_client())

if __name__ == "__main__":
    # 1. Start Server Process
    server_process = multiprocessing.Process(target=start_server)
    server_process.start()

    # 2. Run Client Logic
    start_client_process()

    # 3. Cleanup
    print("[Main] Test finished. Terminating server...")
    server_process.terminate()
    server_process.join()
