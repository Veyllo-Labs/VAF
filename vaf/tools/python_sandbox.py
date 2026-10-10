# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Python Sandbox - Secure Docker-based Code Execution

Executes Python code in the caller's own scratch environment (vaf/core/environments.py):
a container of their own, never shared with another person, kept running between calls
so a run starts in well under a second.

Security features:
- One container per person, non-root, every capability dropped
- Memory limit: 512MB, CPU limit: 0.5 cores
- A working directory per run, removed after it
- Timeouts and Stop end the run's own processes inside the container

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
import os
import posixpath
import shutil
import logging
import uuid
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from vaf.tools.base import BaseTool
from vaf.core.channels import CHAT_CHANNELS

logger = logging.getLogger("vaf.python_sandbox")


class PythonSandboxTool(BaseTool):
    """
    Secure Python Sandbox in the caller's own scratch environment.
    
    Use for:
    - Mathematical calculations
    - Data processing
    - Algorithm implementations
    - Scientific computations
    - Running untrusted code safely
    """
    
    
    identity_kwargs = ("user_scope_id",)
    name = "python_sandbox"
    # A stop-aware poll loop with its own deadline that kills the docker exec the moment Stop
    # is requested. Abandoned by a bounded run instead, the thread could lose the stop flag
    # to clear_stop before it gets to kill the exec.
    self_supervised = True
    category    = "code"
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
        "The sandbox filesystem is EPHEMERAL and isolated from the host: files you write here "
        "do NOT reach the user by themselves. To DELIVER files (images, PDFs, any artifact "
        "your code produces), write them to relative paths and list them in export_files - "
        "they are copied into the chat workspace after the run. For plain text content "
        "write_file(path=..., content=...) also works. "
        "Set with_vaf_tools=True to call other VAF tools from inside the code via "
        "`import vaf_tools; result = vaf_tools.call('tool_name', {...})` — "
        "only the final print output returns to context (Programmatic Tool Calling). "
        "REQUIRES Docker to be installed and running."
    )
    input_examples = [
        {"code": "print(2 ** 32)"},
        {"code": "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.savefig('chart.png')\nprint('done')", "packages": ["matplotlib"], "export_files": ["chart.png"]},
        {"code": "import vaf_tools\ndata = vaf_tools.call('web_search', {'query': 'EUR/USD rate'})\nprint(data)", "with_vaf_tools": True},
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
                "description": "Timeout in seconds (default: 30, max 600)",
                "default": 30
            },
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional: pip packages to install before running (e.g., ['numpy', 'pandas']). "
                    "Installs are TEMPORARY: they go into this run's private directory and are "
                    "deleted with it after the run - nothing accumulates in your sandbox."
                )
            },
            "export_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Files your code wrote that must be DELIVERED to the user: copied from the "
                    "sandbox into the chat workspace after a successful run (e.g. ['chart.png']). "
                    "THE way to persist binary artifacts - never print base64 into context. "
                    "Relative paths resolve against the run's working directory, and only files "
                    "inside it can be exported. Max 5 files."
                )
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
    
    # The sandbox supervises itself (self_supervised), so no dispatcher bound stands behind
    # the model's timeout: it is clamped here, like host_bash's, for both execution paths.
    MAX_TIMEOUT_SECONDS = 600

    @classmethod
    def _run_timeout(cls, kwargs) -> int:
        try:
            requested = int(kwargs.get("timeout") or 30)
        except (TypeError, ValueError):
            requested = 30
        return min(max(1, requested), cls.MAX_TIMEOUT_SECONDS)

    @staticmethod
    def _ensure_docker_available() -> Tuple[bool, str]:
        """Is there a docker daemon to run in? Returns (ok, the reason when not)."""
        try:
            from vaf.core.service_stack import is_docker_daemon_running, resolve_docker_exe
            if not shutil.which(resolve_docker_exe()) and not os.path.isfile(resolve_docker_exe()):
                return False, "Docker is not installed. Please install Docker Desktop from https://docker.com"
            if not is_docker_daemon_running():
                return False, "Docker daemon is not running. Please start Docker Desktop."
            return True, ""
        except Exception as e:
            return False, f"Docker check failed: {e}"

    def _session_stop_check(self) -> Callable[[], bool]:
        """Return a predicate that is True when the current session has requested Stop.
        Lets a long sandbox exec be cancelled promptly instead of running to its timeout while
        the worker thread is abandoned. Falls back to 'never' if the queue/session is unavailable."""
        from vaf.core.tool_dispatch import current_session_stop_check
        return current_session_stop_check()

    def _executor(self, env, run_id: str):
        """`execute_fn(command, timeout, env=None) -> (rc, stdout, stderr)` for one run in
        the scratch environment. Every command of the run carries the same marker, so a
        timeout or a Stop ends exactly this run's processes (exec_bounded)."""
        from vaf.core.environments import get_environment_manager
        mgr = get_environment_manager()
        stopped = self._session_stop_check()

        def execute_fn(command: str, timeout: int, env: Optional[Dict[str, str]] = None):
            r = mgr.exec_in(run_env, ["sh", "-c", command], timeout=timeout, env_values=env,
                            check_stop=stopped, run_id=run_id)
            if r.cancelled:
                return -1, r.stdout, f"{(r.stderr or '').strip()}\nExecution cancelled by stop request.".strip()
            if r.timed_out:
                return -1, r.stdout, f"{(r.stderr or '').strip()}\nExecution timed out after {int(timeout)}s.".strip()
            return r.returncode, r.stdout, r.stderr

        run_env = env
        return execute_fn

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
            names = agent.visible_tools() if hasattr(agent, "visible_tools") else agent.tools
            return _call_via_agent, list(names.keys())

        # No agent, no bridge - deliberately. There used to be a fallback here that looked up
        # `self.available_tools` and called `tool.run(**args)` directly, which skipped the whole
        # pipeline: no policy evaluation, no confirmation gate, no identity assignment. It could
        # never actually fire (this class does not declare `available_tools`, and every injection
        # site in agent.py/framework.py guards on `hasattr`), but a bridge that quietly downgrades
        # to an unchecked dispatcher is the wrong shape to leave lying around. Without an agent the
        # caller gets the honest refusal in run() instead.
        return None, []

    # ------------------------------------------------------------------ #
    #  Temporary per-run package installs                                   #
    # ------------------------------------------------------------------ #
    # Packages land in {workdir}/_pkgs via pip --target, and PYTHONPATH /
    # PIP_TARGET point there for the run. The existing end-of-run
    # `rm -rf {workdir}` then removes them together with the workspace, so
    # installs never accumulate in the person's scratch container, which lives
    # on between runs. PIP_TARGET also catches code that shells out to pip itself.

    _PKG_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\[\],~=<>!-]*$")

    @staticmethod
    def _pkgs_dir(workdir: str) -> str:
        return f"{workdir}/_pkgs"

    @classmethod
    def _validate_packages(cls, packages: List[str]) -> Optional[str]:
        """Return an error message if any spec is not a plain pip requirement
        (defense against shell metacharacters riding in via the model)."""
        for p in packages:
            if not isinstance(p, str) or len(p) > 120 or not cls._PKG_SPEC_RE.match(p):
                return f"Invalid package spec: {p!r}"
        return None

    @classmethod
    def _pip_install_cmd(cls, packages: List[str], workdir: str) -> str:
        pkg_list = " ".join(packages)
        return (
            f"pip install --quiet --disable-pip-version-check --no-cache-dir "
            f"--target {cls._pkgs_dir(workdir)} {pkg_list}"
        )

    @classmethod
    def _run_env_prefix(cls, workdir: str, extra_pythonpath: str = "") -> str:
        pp = f"{extra_pythonpath}:{cls._pkgs_dir(workdir)}" if extra_pythonpath else cls._pkgs_dir(workdir)
        return f"PIP_TARGET={cls._pkgs_dir(workdir)} PYTHONPATH={pp}"

    def _run_with_bridge(
        self,
        code: str,
        execute_fn,
        workdir: str,
        timeout: int,
        bridge_env: Dict[str, str],
        stub_src: str,
    ) -> Tuple[int, str, str]:
        """Write stub + code into workdir, pass bridge env, execute. The bridge URL and
        token reach the run as its environment (containers.exec_bounded), never as text in
        the command."""
        # Write vaf_tools.py stub (base64 to avoid escaping issues)
        b64_stub = base64.b64encode(stub_src.encode()).decode()
        exit_code, _, err = execute_fn(
            f"echo {b64_stub} | base64 -d > {workdir}/vaf_tools.py", timeout=10
        )
        if exit_code != 0:
            return -1, "", f"Failed to write vaf_tools stub: {err}"

        b64_code = base64.b64encode(code.encode()).decode()
        cmd = (
            f"cd {workdir} && "
            f"{self._run_env_prefix(workdir, extra_pythonpath=workdir)} "
            f"sh -c 'echo {b64_code} | base64 -d | python3'"
        )
        return execute_fn(cmd, timeout=timeout, env=dict(bridge_env))

    # ------------------------------------------------------------------ #
    #  Main run()                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _blocked_persistence_write(code: str) -> Optional[str]:
        """Return a redirect message if `code` tries to write a file to a host/workspace
        path, else None.

        python_sandbox runs in a Docker container isolated from the host filesystem (only a
        scratch `/workspace` volume, no bind-mount to the user's Documents/VAF_Projects). A
        write to a host/workspace path therefore lands in the container's ephemeral layer and
        is silently discarded — yet the code's own `print("Saved: ...")` makes it look like it
        worked, so the file the user asked for just vanishes. Detect that intent and redirect
        to write_file (which runs in the host process and actually persists to the chat
        workspace). Pure scratch writes (`/tmp`, `/workspace`, relative paths) are allowed.
        """
        if not code:
            return None
        # A file WRITE (not a read, not stdout/StringIO)?
        writes = (
            (bool(re.search(r"\bopen\s*\(", code)) and bool(re.search(r"""['"](?:x|w|a)b?\+?['"]""", code)))
            or bool(re.search(r"\.(write_text|write_bytes|to_csv|to_json|to_markdown|to_excel|to_html|savefig)\s*\(", code))
        )
        if not writes:
            return None
        # ...targeting a host/workspace persistence path (where the user expects it to land)?
        markers = ("VAF_Projects", "VAF_Documents", "/home/", "/Users/", "\\Users\\", "Documents")
        if not any(m in code for m in markers):
            return None
        return (
            "BLOCKED: python_sandbox runs in an isolated Docker sandbox, so a file written to a "
            "host/workspace path (e.g. under VAF_Projects or Documents) does NOT persist — it "
            "vanishes when the run ends, even though a print(\"Saved: ...\") looks successful. "
            "That is why such 'saved' files never appear in the workspace.\n\n"
            "To DELIVER files produced by your code (images, PDFs, any artifact): write them to "
            "RELATIVE paths and pass export_files=[\"<name>\"] in the SAME python_sandbox call — "
            "they are copied into the chat workspace after the run. Do NOT print base64 into "
            "context (large files get truncated and arrive corrupt).\n"
            "For plain text content you already have, write_file(path=\"<name>\", content=\"...\") "
            "also works. Use python_sandbox scratch paths (/tmp, /workspace) for intermediates."
        )

    @staticmethod
    def _export_source(raw: str, workdir: str) -> Optional[str]:
        """The container path one export_files entry names, or None when it lies outside
        this run's own working directory.

        Only what this run produced is delivered. The sandbox used to be one container for
        everyone, with every run's workdir next to the others under /tmp, and accepting any
        /tmp path let one user's run copy another user's files out. Each person has a
        container of their own now, and the rule still holds inside it: an earlier run's
        leftovers are not this call's artifacts. Normalised first, so `../x` cannot climb
        out."""
        p = str(raw or "").strip()
        if not p:
            return None
        root = posixpath.normpath(workdir)
        cpath = posixpath.normpath(p if p.startswith("/") else posixpath.join(root, p))
        if not cpath.startswith(root + "/"):
            return None
        return cpath

    @staticmethod
    def _refuse_copied_non_file(dest: str) -> Optional[str]:
        """Remove what docker cp produced when it is not a regular file, and say why.

        docker cp copies a symbolic link AS a link: one the code planted (pointing at a
        host path such as ~/.ssh/id_ed25519) would land in the chat workspace and resolve
        on the host when anything opened it. A directory would land whole. Neither is an
        artifact this lane delivers."""
        if os.path.islink(dest):
            try:
                os.unlink(dest)
            except OSError:
                pass
            return "it is a symbolic link"
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
            return "it is a directory"
        return None

    def _export_artifacts(self, export_files, workdir: str, container: str,
                          session_id) -> list:
        """Copy files the code produced OUT of the container into the chat workspace.

        This is the sanctioned exit for binary artifacts (tool-friction-audit wish item
        "sandbox_persist"): the base64-through-context lane truncates anything
        beyond the model's output budget (live incident: a 400KB chart arrived
        as 2.5KB of corrupt PNG). docker cp runs BEFORE the per-exec workdir is
        removed. Only files inside THIS run's working directory may be named
        (see _export_source); the DESTINATION is always the chat workspace - the
        model never chooses a host path. Returns human/model-readable note
        lines; never raises.
        """
        notes = []
        try:
            if not container:
                return ["[export failed: no sandbox container available]"]
            from vaf.core import containers
            from vaf.core.platform import Platform
            from vaf.core.session import resolve_agent_output_dir
            dest_dir = resolve_agent_output_dir(
                Platform.documents_dir() / "VAF_Projects", session_id=session_id
            )
            for raw in list(export_files)[:5]:
                p = str(raw or "").strip()
                if not p:
                    continue
                cpath = self._export_source(p, workdir)
                if cpath is None:
                    notes.append(f"[export skipped: {p} - only files inside this run's "
                                 f"working directory can be exported]")
                    continue
                base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(cpath.rstrip("/"))) or "artifact"
                dest = str(Path(dest_dir) / base)
                if os.path.islink(dest):
                    # docker cp would write THROUGH a link left at the destination.
                    os.unlink(dest)
                try:
                    r = containers.docker(["cp", f"{container}:{cpath}", dest], timeout=60)
                except Exception as e:
                    notes.append(f"[export failed: {p}: {e}]")
                    continue
                refused = self._refuse_copied_non_file(dest)
                if refused:
                    notes.append(f"[export refused: {p} - {refused}]")
                    continue
                if r.returncode != 0 or not os.path.isfile(dest):
                    reason = (r.stderr or "").strip() or "file not found in sandbox"
                    notes.append(f"[export failed: {p}: {reason[:150]}]")
                    continue
                size = os.path.getsize(dest)
                notes.append(f"Exported to chat workspace: {dest} ({size:,} bytes)")
                try:
                    if session_id:
                        from vaf.core.web_interface import notify_file_created
                        notify_file_created(session_id, dest)
                except Exception:
                    pass
        except Exception as e:
            notes.append(f"[export failed: {e}]")
        return notes

    def run(self, **kwargs) -> str:
        """Execute Python code in Docker sandbox (per-user isolated workspace)."""
        code = str(kwargs.get("code", "")).strip()
        timeout = self._run_timeout(kwargs)
        packages = kwargs.get("packages", [])
        with_vaf_tools: bool = bool(kwargs.get("with_vaf_tools", False))
        agent = kwargs.get("_agent") or getattr(self, "_agent", None)
        current_source = str(getattr(agent, "_current_chat_source", "") or "").strip().lower()
        if with_vaf_tools and current_source in CHAT_CHANNELS:
            logger.warning("python_sandbox: disabling with_vaf_tools for channel source=%s", current_source)
            with_vaf_tools = False
        # Whose sandbox: each person gets a container of their own (the scratch environment).
        user_scope_id = kwargs.get("user_scope_id")

        if not code:
            return "[ERROR] python_sandbox: No code provided."

        # Guard: the sandbox is isolated from the host FS, so a write to a workspace/host path
        # silently vanishes. Redirect persistence-intent writes to write_file before running.
        _persist_block = self._blocked_persistence_write(code)
        if _persist_block:
            logger.info("python_sandbox: blocked host-path write, redirecting to write_file")
            return _persist_block

        # Step 1: Verify Docker is available (NO FALLBACK TO HOST)
        docker_ok, docker_error = self._ensure_docker_available()
        if not docker_ok:
            logger.error(f"Docker not available: {docker_error}")
            return f"[SECURITY] Sandbox requires Docker: {docker_error}\n\nCode execution blocked for security reasons."

        # Step 2: This person's scratch environment, created or started on demand.
        from vaf.core.environments import EnvironmentRefused, get_environment_manager
        try:
            scratch = get_environment_manager().scratch_for(user_scope_id)
        except EnvironmentRefused as e:
            return f"[ERROR] python_sandbox: {e}"
        except Exception as e:
            logger.error("python_sandbox: scratch environment unavailable: %s", e)
            return f"[ERROR] python_sandbox: the sandbox could not be started: {e}"
        exec_id = uuid.uuid4().hex[:12]
        execute_fn = self._executor(scratch, exec_id)

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
            # Step 3: A working directory for this run, inside this person's own container.
            workdir = f"/tmp/vaf_run_{exec_id}"

            # Create workspace directory. The mkdir is trivial; this budget is really for the
            # docker-exec round-trip, which can be slow on a COLD or busy container (first run after a
            # restart, or while the local model is saturating CPU/GPU). 5s was too tight and surfaced
            # as a misleading "Failed to create workspace" (the model then misread it as "numpy
            # missing" and gave up). Give the cold exec room, and log the failure so it is diagnosable.
            exit_code, _, err = execute_fn(f"mkdir -p {workdir}", timeout=30)
            if exit_code != 0:
                logger.warning("python_sandbox: workspace creation failed (workdir=%s): %s", workdir, err)
                return f"[ERROR] Failed to create workspace: {err}"

            # Step 4: Install packages if requested - into the run's private
            # _pkgs dir, removed with the workdir in Step 6 (temporary by design).
            if packages:
                bad = self._validate_packages(packages)
                if bad:
                    return f"[ERROR] {bad}"
                logger.info(f"Installing packages (temporary, per-run): {' '.join(packages)}")
                exit_code, out, err = execute_fn(
                    self._pip_install_cmd(packages, workdir),
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
                safe_cmd = (
                    f"cd {workdir} && {self._run_env_prefix(workdir)} "
                    f"sh -c 'echo {b64_code} | base64 -d | python3'"
                )
                logger.debug(f"Executing: {code[:100]}...")
                exit_code, stdout, stderr = execute_fn(safe_cmd, timeout=timeout)

            # Step 5b: Export declared artifacts BEFORE the workdir is removed -
            # this is how binary files reach the user (docker cp to the chat
            # workspace; no base64 through the model's context).
            export_notes = []
            _export_files = kwargs.get("export_files") or []
            if _export_files and exit_code == 0:
                # One resolver: context first, process boundary second.
                try:
                    from vaf.core.subagent_ipc import get_current_session_id
                    _sid = kwargs.get("_session_id") or get_current_session_id()
                except Exception:
                    _sid = kwargs.get("_session_id")
                export_notes = self._export_artifacts(
                    _export_files, workdir, scratch.container, _sid
                )

            # Step 6: Cleanup workspace
            execute_fn(f"rm -rf {workdir}", timeout=15)

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
            if export_notes:
                result = (result.strip() + "\n\n" if result.strip() else "") + "\n".join(export_notes)

            return result.strip() or "[OK] Code executed successfully (no output)."

        except Exception as e:
            logger.error(f"Sandbox execution error: {e}")
            return f"[ERROR] Sandbox execution failed: {e}"
        finally:
            if bridge:
                bridge.stop()
