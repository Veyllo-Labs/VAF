# Process Management in VAF

## Problem: Orphaned llama-server Processes

When the VAF terminal is closed (especially on Windows), the llama-server process could continue running in the background, consuming system resources even though the main application had exited.

## Solution: Multi-Layer Process Management

VAF now implements a comprehensive process management system with multiple layers of protection:

### 1. **Windows Job Objects** (Windows-specific)

On Windows, VAF creates a **Job Object** when the `ServerManager` is initialized. A Job Object is a Windows kernel object that allows you to manage a group of processes as a single unit.

Key features:
- When the Job Object handle is closed, **all processes in the job are automatically terminated**
- This happens even if the parent process crashes or is forcefully terminated
- The Job Object is configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` flag

Implementation:
```python
# In ServerManager.__init__()
if self.system == "Windows":
    self._create_job_object()

# When starting llama-server process
if self.system == "Windows" and self._job_handle:
    ctypes.windll.kernel32.AssignProcessToJobObject(
        self._job_handle,
        process_handle
    )
```

### 2. **Signal Handlers** (All Platforms)

VAF registers multiple signal handlers to catch different termination scenarios:

- **SIGINT** (Ctrl+C) - User interruption
- **SIGTERM** - Graceful termination request
- **SIGHUP** (Unix) - Terminal hangup/disconnection
- **Console Control Handler** (Windows) - Catches window close events

Windows Console Handler catches:
- `CTRL_CLOSE_EVENT` (2) - User closes console window
- `CTRL_LOGOFF_EVENT` (5) - User logs off
- `CTRL_SHUTDOWN_EVENT` (6) - System shutdown

Implementation:
```python
# In Agent.__init__()
signal.signal(signal.SIGINT, self.shutdown)
signal.signal(signal.SIGTERM, self.shutdown)

# Windows-specific
if platform.system() == "Windows":
    kernel32.SetConsoleCtrlHandler(self._win_handler_ref, True)
```

### 3. **atexit Handler** (Final Backup)

As a final safety net, VAF registers an `atexit` handler that:
- Executes when Python interpreter shuts down normally
- Checks if other VAF instances are still running
- Stops the server if no other instances are active

Implementation:
```python
# In Agent.__init__()
atexit.register(self._atexit_cleanup)
```

### 4. **PID File Tracking**

VAF maintains a PID file (`~/.vaf/server.pid`) that:
- Stores the server process ID
- Allows recovery of orphaned processes on next startup
- Enables server reuse across multiple VAF instances

### 5. **Session Management**

VAF tracks active sessions to determine when to stop the server:
- Each VAF instance registers a session
- On exit, checks if other sessions are still active
- Only stops server when the last session exits

## Process Lifecycle

```
1. VAF Starts
   ↓
2. ServerManager creates Job Object (Windows)
   ↓
3. llama-server process starts
   ↓
4. Process is assigned to Job Object (Windows)
   ↓
5. PID is saved to file
   ↓
6. Signal handlers and atexit registered
   ↓
7. User closes terminal/presses Ctrl+C
   ↓
8. Signal handler catches event
   ↓
9. Checks for other active VAF sessions
   ↓
10. If last session:
    - Calls stop_server()
    - Process.terminate() → Process.kill()
    - Job Object ensures process is killed (Windows)
    - PID file removed
```

## Configuration Options

### Persist Server Mode

Users can configure the server to persist across sessions:

```json
{
  "persist_server": true
}
```

When enabled, the server continues running even after all VAF instances exit. This is useful for:
- Faster subsequent launches
- Running server as a daemon
- Development/testing scenarios

To manually stop a persisted server:
```bash
# Linux/macOS
killall llama-server

# Windows
taskkill /F /IM llama-server.exe
```

## Testing

To verify process management works correctly:

1. Start VAF
2. Check that llama-server.exe is running:
   ```powershell
   Get-Process llama-server
   ```
3. Close the terminal window (don't use Ctrl+C)
4. Check again - process should be gone:
   ```powershell
   Get-Process llama-server
   # Should return error: "Cannot find a process"
   ```

## Platform-Specific Behavior

### Windows
- Uses Job Objects for guaranteed cleanup
- Console Control Handler catches window close
- `CREATE_NEW_PROCESS_GROUP` allows clean signal propagation

### Linux/macOS
- Relies on SIGHUP signal when terminal closes
- Process groups ensure child cleanup
- Standard Unix process management

## Troubleshooting

### Server still running after exit?

1. Check if persist mode is enabled:
   ```bash
   cat ~/.vaf/config.json | grep persist_server
   ```

2. Check for other VAF instances:
   ```bash
   # Linux/macOS
   ps aux | grep vaf
   
   # Windows
   tasklist | findstr python
   ```

3. Manually kill orphaned processes:
   ```bash
   # Linux/macOS
   killall llama-server
   
   # Windows
   taskkill /F /IM llama-server.exe
   ```

### Process terminates too early?

Check logs:
- `logs/server.log` - Server output
- `logs/server_cmd.log` - Server startup command

## Related Files

- `vaf/core/backend.py` - ServerManager with Job Object implementation
- `vaf/core/agent.py` - Signal handlers and cleanup logic
- `vaf/core/session.py` - Session tracking

## References

- [Windows Job Objects Documentation](https://docs.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Python subprocess Documentation](https://docs.python.org/3/library/subprocess.html)
- [Signal handling in Python](https://docs.python.org/3/library/signal.html)
