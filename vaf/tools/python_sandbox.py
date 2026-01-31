"""
VAF Python Sandbox - Secure Docker-based Code Execution

Executes Python code in an isolated Docker container for security.
Uses a PERSISTENT container (vaf-sandbox) for fast execution.

Security features:
- Process isolation via Docker
- Memory limit: 512MB
- CPU limit: 0.5 cores
- Workspace isolation between executions
- Auto-cleanup of workspace after each run
"""
import base64
import subprocess
import logging
import uuid
from typing import Tuple
from vaf.tools.base import BaseTool

logger = logging.getLogger("vaf.python_sandbox")

# Persistent container name (from docker-compose.memory.yml)
SANDBOX_CONTAINER = "vaf-sandbox"


class PythonSandboxTool(BaseTool):
    """
    Secure Python Sandbox using a persistent Docker container.
    
    Uses the pre-started 'vaf-sandbox' container for instant execution.
    Falls back to creating an ephemeral container if not available.
    
    Use for:
    - Mathematical calculations
    - Data processing
    - Algorithm implementations
    - Scientific computations
    - Running untrusted code safely
    """
    
    name = "python_sandbox"
    description = """Execute Python code safely in a Docker-isolated sandbox.
Runs code in a secure container with limited resources (512MB RAM, 0.5 CPU).
Use for calculations, data processing, algorithms, and running untrusted code.

REQUIRES Docker to be installed and running.
Returns stdout/stderr from the execution."""

    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute in the sandbox"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30)",
                "default": 30
            },
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: pip packages to install before running (e.g., ['numpy', 'pandas'])"
            }
        },
        "required": ["code"]
    }
    
    def __init__(self):
        super().__init__()
        self._ephemeral_sandbox = None
    
    def _is_persistent_sandbox_running(self) -> bool:
        """Check if the persistent sandbox container is running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", SANDBOX_CONTAINER],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0 and "true" in result.stdout.lower()
        except Exception:
            return False
    
    def _ensure_docker_available(self) -> Tuple[bool, str]:
        """Check if Docker is available. Returns (success, error_message)."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                return False, "Docker daemon is not running. Please start Docker Desktop."
            return True, ""
        except FileNotFoundError:
            return False, "Docker is not installed. Please install Docker Desktop from https://docker.com"
        except subprocess.TimeoutExpired:
            return False, "Docker daemon is not responding. Please restart Docker Desktop."
        except Exception as e:
            return False, f"Docker check failed: {e}"
    
    def _execute_in_persistent(self, command: str, timeout: int, workdir: str = "/workspace") -> Tuple[int, str, str]:
        """Execute command in the persistent sandbox container."""
        exec_cmd = [
            "docker", "exec",
            "-w", workdir,
            SANDBOX_CONTAINER,
            "sh", "-c", command
        ]
        
        try:
            result = subprocess.run(
                exec_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Execution timed out after {timeout}s"
        except Exception as e:
            return -1, "", str(e)
    
    def _execute_in_ephemeral(self, command: str, timeout: int) -> Tuple[int, str, str]:
        """Execute in an ephemeral container (fallback if persistent not available)."""
        if self._ephemeral_sandbox is None:
            from vaf.tools.sandbox import DockerSandbox
            self._ephemeral_sandbox = DockerSandbox(image="python:3.11-slim")
            self._ephemeral_sandbox.start()
        
        return self._ephemeral_sandbox.execute(command, timeout=timeout)
    
    def run(self, **kwargs) -> str:
        """Execute Python code in Docker sandbox."""
        code = str(kwargs.get("code", "")).strip()
        timeout = int(kwargs.get("timeout", 30))
        packages = kwargs.get("packages", [])
        
        if not code:
            return "[ERROR] python_sandbox: No code provided."
        
        # Step 1: Verify Docker is available (NO FALLBACK TO HOST)
        docker_ok, docker_error = self._ensure_docker_available()
        if not docker_ok:
            logger.error(f"Docker not available: {docker_error}")
            return f"[SECURITY] Sandbox requires Docker: {docker_error}\n\nCode execution blocked for security reasons."
        
        # Step 2: Choose execution method (persistent vs ephemeral)
        use_persistent = self._is_persistent_sandbox_running()
        
        if use_persistent:
            logger.debug("Using persistent sandbox (fast)")
            execute_fn = self._execute_in_persistent
        else:
            logger.info("Persistent sandbox not running, using ephemeral container")
            execute_fn = self._execute_in_ephemeral
        
        try:
            # Step 3: Create unique workspace for this execution
            exec_id = uuid.uuid4().hex[:8]
            workdir = f"/tmp/vaf_{exec_id}"
            
            # Create workspace directory
            exit_code, _, err = execute_fn(f"mkdir -p {workdir}", timeout=5)
            if exit_code != 0:
                return f"[ERROR] Failed to create workspace: {err}"
            
            # Step 4: Install packages if requested
            if packages:
                pkg_list = " ".join(packages)
                logger.info(f"Installing packages: {pkg_list}")
                exit_code, out, err = execute_fn(
                    f"pip install --quiet --disable-pip-version-check {pkg_list}",
                    timeout=120
                )
                if exit_code != 0:
                    return f"[ERROR] Failed to install packages: {err or out}"
            
            # Step 5: Execute code
            # Base64 encode to avoid shell escaping issues
            b64_code = base64.b64encode(code.encode('utf-8')).decode('utf-8')
            safe_cmd = f"cd {workdir} && echo {b64_code} | base64 -d | python3"
            
            logger.debug(f"Executing: {code[:100]}...")
            exit_code, stdout, stderr = execute_fn(safe_cmd, timeout=timeout)
            
            # Step 6: Cleanup workspace
            execute_fn(f"rm -rf {workdir}", timeout=5)
            
            # Step 7: Format result
            if exit_code != 0:
                error_output = stderr or stdout or f"Exit code: {exit_code}"
                return f"[ERROR] Sandbox execution failed (exit={exit_code}):\n{error_output}"
            
            result = ""
            if stdout:
                result += stdout
            if stderr:
                if result:
                    result += f"\n[stderr]\n{stderr}"
                else:
                    result = f"[stderr]\n{stderr}"
            
            return result.strip() or "[OK] Code executed successfully (no output)."
            
        except Exception as e:
            logger.error(f"Sandbox execution error: {e}")
            return f"[ERROR] Sandbox execution failed: {e}"
    
    def cleanup(self):
        """Stop ephemeral sandbox if used."""
        if self._ephemeral_sandbox:
            try:
                self._ephemeral_sandbox.stop()
            except Exception as e:
                logger.warning(f"Sandbox cleanup failed: {e}")
            self._ephemeral_sandbox = None
    
    def __del__(self):
        """Cleanup on destruction."""
        self.cleanup()
