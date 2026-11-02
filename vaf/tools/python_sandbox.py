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
- Timeouts and Stop end the run's own processes inside the container; output is
  unbuffered (PYTHONUNBUFFERED), so a run stopped at its limit returns what it printed

Background runs (background=true) go through EnvironmentManager.start_process, the lane
sandbox_exec already uses: the call returns a handle at once, and the chat gets the
output and exit code as a wake turn when the run ends. The run's folder is removed then.
Refused where nobody could be told (processes.background_refusal), with with_vaf_tools
(the bridge lives only as long as the call), with export_files (files are delivered only
inside the call) and while a tool is being trained (a probe leaves nothing behind).

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


# The note containers.exec_bounded's backstop appends to stderr when it ends a run: the
# executor's own words, not the code's, so the answer leaves it out.
_CONTAINER_NOTE_RE = re.compile(r"\n?(?:cancelled by stop request|timed out after \d+s)\s*$")


class _Outcome(tuple):
    """`(rc, stdout, stderr)` of one command, with the ExecResult's own flags beside it.

    The setup commands unpack the three; the main run reads `timed_out`, `cancelled` and
    `raw_stderr` (the command's stderr without the executor's note) from here, so a timeout
    is known from the flag and never parsed out of text the code itself could print."""
    timed_out = False
    cancelled = False
    raw_stderr = ""

    @classmethod
    def of(cls, rc: int, stdout: str, stderr: str, *, timed_out: bool = False,
           cancelled: bool = False, raw_stderr: str = "") -> "_Outcome":
        o = cls((rc, stdout, stderr))
        o.timed_out, o.cancelled, o.raw_stderr = timed_out, cancelled, raw_stderr
        return o


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
    
    
    # The chat (session_id) is declared, not injected by name: export_files delivers into it,
    # and a background run reports back to it with the person's name and role.
    identity_kwargs = ("user_scope_id", "username", "user_role", "session_id")
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
    # bridge `with_vaf_tools` is opt-in and defaults to False; background=true is refused while
    # a tool is being trained, because a run that outlives the call is not nothing), and full
    # probing is the only way to learn a tool whose whole job is to ACCEPT and run code.
    whare_wananga_full_probe = True
    description = (
        "Run Python code in your own isolated sandbox (a Docker container: 512 MB RAM, 0.5 CPU, "
        "internet access, no access to this computer's files) and get what it prints. Default "
        "wait 120 s, max 600; on a timeout you get the output so far. For long work set "
        "background=true: the call returns at once and the result comes into this chat when the "
        "run ends. Files the code writes are temporary: list the ones the user should get in "
        "export_files. with_vaf_tools=True lets the code call VAF tools "
        "(`import vaf_tools; vaf_tools.call('tool_name', {...})`). REQUIRES Docker."
    )
    input_examples = [
        {"code": "print(2 ** 32)"},
        {"code": "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.savefig('chart.png')\nprint('done')", "packages": ["matplotlib"], "export_files": ["chart.png"]},
        {"code": "import vaf_tools\ndata = vaf_tools.call('web_search', {'query': 'EUR/USD rate'})\nprint(data)", "with_vaf_tools": True},
        {"code": "import subprocess\nfor i in range(10):\n    subprocess.run(['pip', 'install', 'requests'])\n    print('done', i)", "background": True},
    ]
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python code to run. print() what you want to see."
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds to wait for the result. Default 120, max 600. When time runs out, "
                    "the run is stopped and you get what it printed so far. Not used with "
                    "background=true."
                ),
                "default": 120
            },
            "background": {
                "type": "boolean",
                "description": (
                    "true: start the run and return at once. Its output and exit code come into "
                    "this chat as a new message when it ends (at most 8 hours). Use it for long "
                    "work: many installs, big downloads, long loops."
                ),
                "default": False
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
                    "inside it can be exported. Max 5 files (not with background=true)."
                )
            },
            "with_vaf_tools": {
                "type": "boolean",
                "description": (
                    "If true, inject a `vaf_tools` module so code can call VAF tools: "
                    "`import vaf_tools; result = vaf_tools.call('web_search', {'query': '...'})`. "
                    "Only the final print output is returned to the model context (no intermediate tool results). "
                    "Default: false (not with background=true)."
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
    # The default is sandbox_exec's: one `pip install` measured about 40 s, which a 30 s
    # default cut off before it had printed anything.
    DEFAULT_TIMEOUT_SECONDS = 120
    MAX_TIMEOUT_SECONDS = 600

    @classmethod
    def _run_timeout(cls, kwargs) -> int:
        try:
            requested = int(kwargs.get("timeout") or cls.DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            requested = cls.DEFAULT_TIMEOUT_SECONDS
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
            raw = _CONTAINER_NOTE_RE.sub("", r.stderr or "") if (r.cancelled or r.timed_out) else (r.stderr or "")
            if r.cancelled:
                return _Outcome.of(-1, r.stdout, f"{raw.strip()}\nExecution cancelled by stop request.".strip(),
                                   cancelled=True, raw_stderr=raw)
            if r.timed_out:
                return _Outcome.of(-1, r.stdout, f"{raw.strip()}\nExecution timed out after {int(timeout)}s.".strip(),
                                   timed_out=True, raw_stderr=raw)
            return _Outcome.of(r.returncode, r.stdout, r.stderr, raw_stderr=raw)

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
        """The run's environment. PYTHONUNBUFFERED: stdout is a pipe, so Python would
        block-buffer it, and the kill at a timeout drops that buffer - measured, a run that
        had printed three lines returned none. It reaches every Python the code starts too."""
        pp = f"{extra_pythonpath}:{cls._pkgs_dir(workdir)}" if extra_pythonpath else cls._pkgs_dir(workdir)
        return f"PIP_TARGET={cls._pkgs_dir(workdir)} PYTHONPATH={pp} PYTHONUNBUFFERED=1"

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
                elif os.path.exists(dest) and not os.path.isfile(dest):
                    # docker cp would copy INTO an existing folder, and the non-file check
                    # below would then remove that folder - one the person already had.
                    notes.append(f"[export skipped: {p} - {base} already exists in the chat "
                                 f"workspace and is not a file]")
                    continue
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

    # ------------------------------------------------------------------ #
    #  The answers                                                          #
    # ------------------------------------------------------------------ #
    # A timeout or a Stop is not worded as a failure ("error", "failed ... execution"): the
    # chat loop's inline retry blocker (vaf/core/agent.py, "Blocked retry of failed tool")
    # would then refuse the model's NEXT call of this tool, the one with a larger timeout or
    # background=true. Its identical-argument check still stops a verbatim repeat.

    @classmethod
    def _more_time_hint(cls, timeout: int, background_ok: bool) -> str:
        """The last line of a timeout answer: what gives the work the time it needs."""
        larger = (f"call python_sandbox again with a larger timeout (at most "
                  f"{cls.MAX_TIMEOUT_SECONDS})")
        later = "it returns at once and the result comes into this chat when the run ends."
        if timeout < cls.MAX_TIMEOUT_SECONDS:
            if background_ok and timeout >= cls.DEFAULT_TIMEOUT_SECONDS:
                # Already the default or more: work this long is background work (a loop of
                # installs ran about 7 minutes, past what any timeout here covers), and a
                # weak model takes the first remedy it reads.
                return (f"For long work, call python_sandbox again with background=true: {later} "
                        f"For a little more time, {larger}.")
            if background_ok:
                return f"To give it more time, {larger}, or with background=true for long work: {later}"
            return f"To give it more time, {larger}, or split the work into shorter runs."
        if background_ok:
            return (f"For work longer than {cls.MAX_TIMEOUT_SECONDS} s, call python_sandbox again "
                    f"with background=true: {later}")
        return "Split the work into shorter runs."

    @staticmethod
    def _stopped_answer(lead: str, stdout: str, stderr: str, export_given: bool,
                        hint: str = "") -> str:
        """A run that did not finish: what it printed until then, never thrown away. The
        status line first and the hint last, so both survive the chat's cut from the middle."""
        lines = [f"{lead} The code was stopped before it finished.", "Output so far:",
                 (stdout or "").rstrip() or "(nothing was printed)"]
        if (stderr or "").strip():
            lines += ["[stderr]", stderr.rstrip()]
        if export_given:
            lines.append("export_files were not copied: the run did not finish.")
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    @staticmethod
    def _failed_answer(exit_code: int, stdout: str, stderr: str) -> str:
        """A run that exited with a code other than 0: its output AND its error, because a
        traceback alone hides how far the code got."""
        lines = [f"[ERROR] Sandbox execution failed (exit={exit_code}):"]
        if (stdout or "").strip():
            lines.append(stdout.rstrip())
        if (stderr or "").strip():
            lines += ["[stderr]", stderr.rstrip()]
        if len(lines) == 1:
            lines.append("(no output)")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Background runs                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _background_refusal(kwargs: dict, agent: Any, source: str) -> Optional[str]:
        """Why this call may not run in the background, as the whole answer, or None. Asked
        before anything is created or run."""
        if kwargs.get("with_vaf_tools"):
            return ("[BLOCKED] with_vaf_tools does not work with background=true: the tool bridge "
                    "lives only as long as this call. Nothing ran. Call it again without one of "
                    "the two.")
        if kwargs.get("export_files"):
            return ("[BLOCKED] export_files does not work with background=true: files are "
                    "delivered only when the run ends inside the call. Nothing ran. Run it "
                    "without background to get the files.")
        from vaf.core.processes import background_refusal
        reason = background_refusal(kwargs.get("session_id"), source)
        if reason:
            return f"[BLOCKED] {reason}. Nothing ran. Run it without background."
        if getattr(agent, "_ww_training", False):
            return ("[BLOCKED] background runs are not started while a tool is being trained. "
                    "Nothing ran.")
        return None

    @staticmethod
    def _label(code: str) -> str:
        first = next((ln.strip() for ln in code.splitlines() if ln.strip()), "")
        return "python_sandbox: " + first[:80]

    def _start_background(self, code: str, packages, kwargs: dict, user_scope_id: Any) -> str:
        """Start the code as a background process in the caller's scratch environment
        (EnvironmentManager.start_process) and return its handle at once. The folder is the
        process's now: start_process removes it when the run is reported or stopped."""
        from vaf.core.environments import EnvironmentRefused, get_environment_manager, setting
        if packages:
            bad = self._validate_packages(packages)
            if bad:
                return f"[ERROR] {bad}"
        mgr = get_environment_manager()
        try:
            scratch = mgr.scratch_for(user_scope_id)
        except EnvironmentRefused as e:
            return f"[ERROR] python_sandbox: {e}"
        except Exception as e:
            logger.error("python_sandbox: scratch environment unavailable: %s", e)
            return f"[ERROR] python_sandbox: the sandbox could not be started: {e}"
        workdir = f"/tmp/vaf_run_{uuid.uuid4().hex[:12]}"
        handed_over = False
        try:
            r = mgr.exec_in(scratch, ["mkdir", "-p", workdir], timeout=30)
            if r.returncode != 0:
                return f"[ERROR] Failed to create workspace: {r.stderr}"
            # On stdin, never in the command: the command travels as one environment
            # string (VAF_CMD), which the kernel limits to 128 KB.
            r = mgr.exec_in(scratch, ["sh", "-c", f"cat > {workdir}/main.py"], timeout=30,
                            input_text=code)
            if r.returncode != 0:
                return f"[ERROR] python_sandbox: the code could not be written: {r.stderr}"
            command = f"{self._run_env_prefix(workdir)} python3 -u main.py"
            if packages:
                command = f"{self._pip_install_cmd(packages, workdir)} && {command}"
            try:
                handle = mgr.start_process(
                    user_scope_id, scratch.id, command,
                    session_id=str(kwargs.get("session_id") or ""),
                    username=kwargs.get("username"), user_role=kwargs.get("user_role"),
                    cwd=workdir, cleanup_dir=workdir, label=self._label(code))
            except EnvironmentRefused as e:
                return f"[BLOCKED] {e} Nothing ran."
            handed_over = True
        except Exception as e:
            logger.error("python_sandbox: background start failed: %s", e)
            return f"[ERROR] python_sandbox: the background run could not be started: {e}"
        finally:
            if not handed_over:
                try:
                    mgr.exec_in(scratch, ["rm", "-rf", workdir], timeout=15)
                except Exception:
                    pass
        from vaf.core.tool_dispatch import advise_tools
        accepted = advise_tools("host_process")
        lines = [f"[started in the background] {handle}",
                 f"The code runs on its own now (at most {setting('process_max_hours')} h); this "
                 f"chat gets its output and exit code as a new message when it ends, so there is "
                 f"nothing to wait for."]
        if accepted is None or "host_process" in accepted:
            lines.append(f"To look before then: host_process(action=\"log\", id=\"{handle}\"); "
                         f"to stop it: host_process(action=\"stop\", id=\"{handle}\").")
        return "\n".join(lines)

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
        # Strict: a model that writes the flag as the string "false" means no.
        background = str(kwargs.get("background", "")).strip().lower() in ("true", "1", "yes")
        export_given = bool(kwargs.get("export_files"))

        if not code:
            return "[ERROR] python_sandbox: No code provided."

        if background:
            refused = self._background_refusal(kwargs, agent, current_source)
            if refused:
                return refused

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

        if background:
            return self._start_background(code, packages, kwargs, user_scope_id)

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

        # Step 3: A working directory for this run, inside this person's own container.
        workdir = f"/tmp/vaf_run_{exec_id}"
        try:

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
                outcome = self._run_with_bridge(
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
                outcome = execute_fn(safe_cmd, timeout=timeout)
            exit_code, stdout, stderr = outcome

            # A run that did not finish: what it printed so far, and how to go on.
            if getattr(outcome, "timed_out", False) or getattr(outcome, "cancelled", False):
                raw_err = getattr(outcome, "raw_stderr", stderr)
                if getattr(outcome, "cancelled", False):
                    return self._stopped_answer("[cancelled by stop request]", stdout, raw_err,
                                                export_given)
                from vaf.core.processes import background_refusal
                background_ok = (not with_vaf_tools and not export_given
                                 and background_refusal(kwargs.get("session_id"), current_source) is None
                                 and not getattr(agent, "_ww_training", False))
                return self._stopped_answer(f"[timed out after {timeout} s]", stdout, raw_err,
                                            export_given, self._more_time_hint(timeout, background_ok))

            # Step 5b: Export declared artifacts BEFORE the workdir is removed -
            # this is how binary files reach the user (docker cp to the chat
            # workspace; no base64 through the model's context).
            export_notes = []
            _export_files = kwargs.get("export_files") or []
            if _export_files and exit_code == 0:
                # The declared chat first (identity_kwargs), the process boundary second.
                try:
                    from vaf.core.subagent_ipc import get_current_session_id
                    _sid = kwargs.get("session_id") or get_current_session_id()
                except Exception:
                    _sid = kwargs.get("session_id")
                export_notes = self._export_artifacts(
                    _export_files, workdir, scratch.container, _sid
                )

            # Step 6: the workspace goes in the finally below, on every exit.

            # Step 7: Format result
            if exit_code != 0:
                return self._failed_answer(exit_code, stdout, stderr)

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
            # On every exit - a refused package, a failed install, an exception - and not
            # through execute_fn: after a Stop its stop check would cancel the cleanup too.
            try:
                get_environment_manager().exec_in(scratch, ["rm", "-rf", workdir], timeout=15)
            except Exception:
                pass
            if bridge:
                bridge.stop()
