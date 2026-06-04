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

Programmatic Tool Calling (Tool Calling 2.0 — provider-agnostic)
-----------------------------------------------------------------
Pass with_vaf_tools=True to give sandbox code access to a `vaf_tools` module
that lets it call any VAF tool directly.  Only the final stdout of the code
returns to the model context; intermediate tool results are consumed inside
the running script and never become chat messages.

  python_sandbox(
      code=\"\"\"
import vaf_tools
weather = vaf_tools.call("web_search", {"query": "Berlin weather today"})
orders  = vaf_tools.call("get_orders", {"limit": 5})
print(f"Weather: {weather}\\nOrders: {orders}")
\"\"\",
      with_vaf_tools=True,
  )

Works with every backend (OpenAI, Anthropic, Google, local) — no special
API features required.
"""
import base64
import subprocess
import logging
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
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
    permission_level = "write"
    side_effect_class = "reversible"
    # Whare Wananga: probe this in full rather than via the error path. Executing self-contained
    # probe code here is harmless and leaves nothing permanent (Docker-isolated; the host-tool
    # bridge `with_vaf_tools` is opt-in and defaults to False), and full probing is the only way
    # to learn a tool whose whole job is to ACCEPT and run code.
    whare_wananga_full_probe = True
    description = (
        "Execute Python code safely in a Docker-isolated sandbox. "
        "Runs code in a secure container with limited resources (512MB RAM, 0.5 CPU). "
        "Use for calculations, data processing, algorithms, and running untrusted code. "
        "Set with_vaf_tools=True to call other VAF tools from inside the code via "
        "`import vaf_tools; result = vaf_tools.call('tool_name', {...})` — "
        "only the final print output returns to context (Programmatic Tool Calling). "
        "REQUIRES Docker to be installed and running."
    )
    input_examples = [
        {"code": "print(2 ** 32)"},
        {"code": "import vaf_tools\ndata = vaf_tools.call('web_search', {'query': 'EUR/USD rate'})\nprint(data)", "with_vaf_tools": True},
        {"code": "import pandas as pd\ndf = pd.DataFrame({'a': [1,2,3]})\nprint(df.describe())", "packages": ["pandas"]},
    ]
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
            },
            "with_vaf_tools": {
                "type": "boolean",
                "description": (
                    "If true, inject a `vaf_tools` module so code can call VAF tools: "
                    "`import vaf_tools; result = vaf_tools.call('web_search', {'query': '...'})`. "
                    "Only the final print output is returned to the model context (no intermediate tool results). "
                    "Default: false."
                ),
                "default": False
            }
        },
        "required": ["code"]
    }

    # Injected by agent after tool loading — provides access to the tool registry
    # so with_vaf_tools=True can call real tools.
    _agent: Optional[Any] = None
    
    def __init__(self):
        super().__init__()
        self._ephemeral_sandbox = None
    
    def _get_subprocess_kwargs(self) -> dict:
        """Get platform-specific subprocess kwargs."""
        import platform
        kwargs = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return kwargs

    def _is_persistent_sandbox_running(self) -> bool:
        """Check if the persistent sandbox container is running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", SANDBOX_CONTAINER],
                capture_output=True,
                text=True,
                timeout=5,
                **self._get_subprocess_kwargs()
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
                timeout=10,
                **self._get_subprocess_kwargs()
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

    def _session_stop_check(self) -> Callable[[], bool]:
        """Return a predicate that is True when the current session has requested Stop.
        Lets a long sandbox exec be cancelled promptly instead of running to its timeout while
        the worker thread is abandoned. Falls back to 'never' if the queue/session is unavailable."""
        try:
            from vaf.core.task_queue import TaskQueue
            from vaf.core.subagent_ipc import get_current_session_id
            sid = get_current_session_id()
            tq = TaskQueue()
            return lambda: bool(sid) and tq.should_stop(sid)
        except Exception:
            return lambda: False

    def _kill_sandbox_exec(self, proc) -> None:
        """Halt a sandbox exec: kill the host docker-exec client and best-effort the in-container
        process (docker exec does not propagate the kill into the container, so the code would keep
        running otherwise)."""
        try:
            proc.kill()
        except Exception:
            pass
        try:
            subprocess.run(
                ["docker", "exec", SANDBOX_CONTAINER, "pkill", "-9", "-f", "python"],
                capture_output=True, timeout=5, **self._get_subprocess_kwargs()
            )
        except Exception:
            pass

    def _execute_in_persistent(self, command: str, timeout: int, workdir: str = "/workspace") -> Tuple[int, str, str]:
        """Execute command in the persistent sandbox container, stop-aware: a Stop request kills the
        exec promptly instead of letting it run to the timeout."""
        exec_cmd = [
            "docker", "exec",
            "-w", workdir,
            SANDBOX_CONTAINER,
            "sh", "-c", command
        ]

        try:
            proc = subprocess.Popen(
                exec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                **self._get_subprocess_kwargs()
            )
        except Exception as e:
            return -1, "", str(e)

        stopped = self._session_stop_check()
        deadline = time.monotonic() + max(1.0, float(timeout))
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                pass
            reason = "cancelled by stop request" if stopped() else (
                f"timed out after {int(timeout)}s" if time.monotonic() >= deadline else None
            )
            if reason:
                self._kill_sandbox_exec(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = "", ""
                return -1, out or "", f"{(err or '').strip()}\nExecution {reason}.".strip()
    
    def _execute_in_ephemeral(self, command: str, timeout: int) -> Tuple[int, str, str]:
        """Execute in an ephemeral container (fallback if persistent not available)."""
        if self._ephemeral_sandbox is None:
            from vaf.tools.sandbox import DockerSandbox
            self._ephemeral_sandbox = DockerSandbox(image="python:3.11-slim")
            self._ephemeral_sandbox.start()
        
        return self._ephemeral_sandbox.execute(command, timeout=timeout)
    
    # ------------------------------------------------------------------ #
    #  Programmatic Tool Calling helpers                                   #
    # ------------------------------------------------------------------ #

    def _build_call_tool_fn(self, kwargs: dict):
        """Return a call_tool function bound to the current agent/tool registry."""
        agent = kwargs.get("_agent") or getattr(self, "_agent", None)
        if agent is not None:
            # Use the agent's full execute_tool pipeline (trust gates, logging, etc.)
            def _call_via_agent(tool_name: str, args: Dict[str, Any]) -> str:
                try:
                    return str(agent.execute_tool(tool_name, args))
                except Exception as exc:
                    return f"[ERROR] {exc}"
            return _call_via_agent, list(agent.tools.keys())

        # Fallback: use the tool registry from available_tools if set
        registry: Dict[str, Any] = getattr(self, "available_tools", {}) or {}
        if registry:
            def _call_via_registry(tool_name: str, args: Dict[str, Any]) -> str:
                tool = registry.get(tool_name)
                if tool is None:
                    return f"[ERROR] Unknown tool '{tool_name}'"
                try:
                    return str(tool.run(**args))
                except Exception as exc:
                    return f"[ERROR] {exc}"
            return _call_via_registry, list(registry.keys())

        return None, []

    def _run_with_bridge(
        self,
        code: str,
        execute_fn,
        workdir: str,
        timeout: int,
        bridge_env: Dict[str, str],
        stub_src: str,
    ) -> Tuple[int, str, str]:
        """Write stub + code into workdir, pass bridge env, execute."""
        # Write vaf_tools.py stub (base64 to avoid escaping issues)
        b64_stub = base64.b64encode(stub_src.encode()).decode()
        exit_code, _, err = execute_fn(
            f"echo {b64_stub} | base64 -d > {workdir}/vaf_tools.py", timeout=10
        )
        if exit_code != 0:
            return -1, "", f"Failed to write vaf_tools stub: {err}"

        # Build env export prefix for the sandbox command
        env_prefix = " ".join(f'{k}="{v}"' for k, v in bridge_env.items())

        b64_code = base64.b64encode(code.encode()).decode()
        cmd = (
            f"cd {workdir} && "
            f"PYTHONPATH={workdir} {env_prefix} "
            f"sh -c 'echo {b64_code} | base64 -d | python3'"
        )
        return execute_fn(cmd, timeout=timeout)

    # ------------------------------------------------------------------ #
    #  Main run()                                                           #
    # ------------------------------------------------------------------ #

    def run(self, **kwargs) -> str:
        """Execute Python code in Docker sandbox (per-user isolated workspace)."""
        code = str(kwargs.get("code", "")).strip()
        timeout = int(kwargs.get("timeout", 30))
        packages = kwargs.get("packages", [])
        with_vaf_tools: bool = bool(kwargs.get("with_vaf_tools", False))
        agent = kwargs.get("_agent") or getattr(self, "_agent", None)
        current_source = str(getattr(agent, "_current_chat_source", "") or "").strip().lower()
        if with_vaf_tools and current_source in {"telegram", "whatsapp", "discord"}:
            logger.warning("python_sandbox: disabling with_vaf_tools for channel source=%s", current_source)
            with_vaf_tools = False
        # User scope for workspace isolation — each user gets their own temp directory
        user_scope_id = kwargs.get("user_scope_id")

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

        # Step 2b: If Programmatic Tool Calling requested, set up the bridge
        bridge = None
        bridge_env: Dict[str, str] = {}
        stub_src: str = ""
        if with_vaf_tools:
            call_tool_fn, available_tools = self._build_call_tool_fn(kwargs)
            if call_tool_fn is None:
                return (
                    "[ERROR] python_sandbox: with_vaf_tools=True but no tool registry is accessible. "
                    "The sandbox must be called from within an agent context."
                )
            try:
                from vaf.core.tool_bridge import ToolBridgeServer
                import secrets
                token = secrets.token_hex(16)

                def _safe_call(name: str, args: Dict[str, Any]) -> str:
                    logger.info("ToolBridge: sandbox called tool=%s", name)
                    return call_tool_fn(name, args)

                bridge = ToolBridgeServer(
                    call_tool=_safe_call,
                    list_tools=lambda: available_tools,
                    token=token,
                )
                bridge.start()
                bridge_env = bridge.sandbox_env()
                stub_src = bridge.stub_source()
                logger.info("ToolBridge: sandbox env=%s", bridge_env)
            except Exception as exc:
                logger.warning("ToolBridge setup failed: %s", exc)
                return f"[ERROR] python_sandbox: Could not start tool bridge: {exc}"

        try:
            # Step 3: Create unique workspace for this execution (per-user isolated)
            exec_id = uuid.uuid4().hex[:8]
            scope_prefix = str(user_scope_id).replace("-", "")[:12] if user_scope_id else "shared"
            workdir = f"/tmp/vaf_{scope_prefix}_{exec_id}"

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
            if with_vaf_tools and bridge_env and stub_src:
                logger.debug("Executing with vaf_tools bridge: %s...", code[:100])
                exit_code, stdout, stderr = self._run_with_bridge(
                    code, execute_fn, workdir, timeout, bridge_env, stub_src
                )
            else:
                # Standard execution: Base64 encode to avoid shell escaping issues
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
        finally:
            if bridge:
                bridge.stop()
    
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
