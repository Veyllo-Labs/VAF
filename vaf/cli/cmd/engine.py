# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf engine`: which engine runs the local model (vaf/core/local_engine.py).

    vaf engine                       the engine in use, what this machine can run, the
                                     vLLM install, its model and parsers, the server
    vaf engine vllm [--model REPO]   checks the hardware, installs the pinned vLLM (asks
                [--yes]              first, or --yes), downloads the model, checks that it
                                     fits and switches the local engine to vLLM
    vaf engine llama                 back to llama.cpp (GGUF); a running vLLM is stopped
    vaf engine vllm --uninstall      removes the vLLM environment (refused while vLLM is
                                     the engine); the models stay in the Hugging Face cache

Exit codes like `vaf profile`: 0 done, 1 not complete, 2 refused.

The switch does not start vLLM itself: the next use of the local model starts it, as it
starts the GGUF engine. Not behind the terminal door, like `vaf profile`: what it changes
is the machine's, not an account's data, and an account that could run it from the
agent's shell has `host_bash`, which can install the same packages directly.
"""
from typing import Optional

import typer

from vaf.cli.ui import UI


def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB"


def _n_ctx() -> int:
    from vaf.core.config import Config
    from vaf.core.vllm_server import MIN_CONTEXT
    try:
        return max(int(Config.get("n_ctx", MIN_CONTEXT) or MIN_CONTEXT), MIN_CONTEXT)
    except (TypeError, ValueError):
        return MIN_CONTEXT


def _show() -> None:
    from vaf.core import local_engine as le
    from vaf.core import vllm_server as vs
    engine = le.current()
    UI.info("Engine: " + ("vllm (vLLM, Hugging Face safetensors)" if engine == le.VLLM
                          else "llama (llama.cpp, GGUF)"))
    verdict = le.support()
    UI.info(f"vLLM on this machine: {verdict.level}. {verdict.reason}")
    version = vs.installed_version()
    UI.info(f"vLLM installed: {version}" if version
            else f"vLLM installed: no (VAF installs {vs.PIN} with `vaf engine vllm`)")
    repo = le.resolve_vllm_model()
    tool, reasoning = le.parsers_for(repo)
    UI.info(f"vLLM model: {repo}; tool-call parser {tool or 'unknown'}, reasoning parser "
            f"{reasoning or 'none'}")
    pid = vs.running_pid()
    if engine == le.VLLM:
        UI.info(f"vLLM server: running (PID {pid})" if pid else "vLLM server: not running "
                "(it starts with the next use of the local model)")
    for sentence in le.conflicts():
        UI.warning(sentence)


def _stop_llama() -> bool:
    """Stop the llama server, which holds the GPU memory vLLM is measured against and needs,
    and wait until that memory is released (the same handover the vLLM start runs). True when
    one was running and is stopped now; exit 1 when it does not stop."""
    from vaf.core.backend import ServerManager
    mgr = ServerManager(skip_cleanup=True)
    if mgr._llama_pid() is None:
        return False
    UI.info("Stopping the llama server (vLLM needs its GPU memory)...")
    reason = mgr.hand_over_from_llama()
    if reason:
        UI.error(reason + " The local engine is unchanged.")
        raise typer.Exit(1)
    return True


def _uninstall() -> None:
    from vaf.core import local_engine as le
    from vaf.core import vllm_server as vs
    if le.is_vllm():
        UI.error("vLLM is the local engine: switch back first (`vaf engine llama`), then "
                 "uninstall.")
        raise typer.Exit(2)
    if vs.installed_version() is None and not vs.venv_dir().exists():
        UI.info("vLLM is not installed.")
        return
    reason = vs.uninstall()
    if reason:
        UI.error(reason)
        raise typer.Exit(1)
    UI.success(f"Removed {vs.venv_dir()}.")
    repo = le.resolve_vllm_model()
    snapshot = vs.local_snapshot(repo)
    if snapshot:
        UI.info(f"The model {repo} ({_gb(vs.model_size_bytes(snapshot))}) stays in the Hugging "
                f"Face cache ({snapshot}); delete it there when you need the space.")
    else:
        UI.info("Models downloaded for vLLM stay in the Hugging Face cache.")


def _to_vllm(model: Optional[str], yes: bool) -> None:
    from vaf.core import inference_profile as ip
    from vaf.core import local_engine as le
    from vaf.core import vllm_server as vs
    from vaf.core.config import Config

    if not ip.local_inference_allowed():
        UI.error(ip.refusal("vLLM"))
        raise typer.Exit(2)
    if model is not None and not le.valid_repo_id(model):
        UI.error(f"--model takes a Hugging Face repo id (owner/name), not {model!r}.")
        raise typer.Exit(2)
    verdict = le.support()
    if not verdict.supported:
        UI.error(verdict.reason)
        raise typer.Exit(2)
    repo = model.strip() if model else le.resolve_vllm_model()
    tool, reasoning = le.parsers_for(repo)
    refusal = le.parser_refusal(repo, tool, reasoning)
    if refusal:
        UI.error(refusal)
        raise typer.Exit(2)

    if not vs.is_installed() or vs.installed_version() != vs.PIN:
        UI.info(f"This installs vLLM {vs.PIN} (about {vs.DOWNLOAD_GB_AT_LEAST:.1f} GB or more to "
                f"download, more on disk) into {vs.venv_dir()}.")
        if not yes and not typer.confirm("Install it now?", default=False):
            UI.info("Nothing was installed; the local engine is unchanged.")
            raise typer.Exit(1)
        reason = vs.install(log=UI.info)
        if reason:
            UI.error(reason)
            UI.error("The local engine is unchanged.")
            raise typer.Exit(1)
        UI.success(f"vLLM {vs.installed_version() or vs.PIN} is installed.")

    model_dir = vs.ensure_model(repo)
    if not model_dir:
        UI.error(f"The model {repo} could not be downloaded; the local engine is unchanged.")
        raise typer.Exit(1)

    stopped_llama = _stop_llama()
    result = vs.plan_fit(model_dir, _n_ctx())
    if result.note:
        UI.warning(result.note)
    if not result.ok:
        UI.error(result.reason)
        UI.error(f"The local engine stays {le.current()}; a smaller model (--model) or a "
                 "smaller n_ctx may fit."
                 + (" The llama server was stopped for the measurement; it starts again with "
                    "the next use of the local model." if stopped_llama else ""))
        raise typer.Exit(1)
    UI.info(f"{repo}: {result.reason}")
    if model is not None:
        Config.set("vllm_model", repo)
    previous = le.current()
    Config.set("local_engine", le.VLLM)
    UI.success("The local engine is vLLM." + (" (unchanged)" if previous == le.VLLM else "")
               + " It starts with the next use of the local model; the first start takes "
               "minutes.")


def _to_llama() -> None:
    from vaf.core import local_engine as le
    from vaf.core import vllm_server as vs
    from vaf.core.config import Config
    previous = le.current()
    Config.set("local_engine", le.LLAMA)
    reason = vs.stop() if vs.running_pid() is not None else ""
    UI.success("The local engine is llama.cpp." + (" (unchanged)" if previous == le.LLAMA else ""))
    if reason:
        UI.error(reason)
        raise typer.Exit(1)


def engine_cmd(
    name: Optional[str] = typer.Argument(None, help="llama or vllm; empty shows the current one"),
    model: Optional[str] = typer.Option(None, "--model",
                                        help="With vllm: the Hugging Face repo to run (owner/name)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="With vllm: install without asking"),
    uninstall: bool = typer.Option(False, "--uninstall",
                                   help="With vllm: remove the vLLM environment"),
):
    """Which engine runs the local model: llama.cpp (GGUF) or vLLM."""
    if name is None:
        _show()
        return
    wanted = name.strip().lower()
    if wanted not in ("llama", "vllm"):
        UI.error(f"Unknown engine {name!r}: use llama or vllm.")
        raise typer.Exit(2)
    if wanted == "llama":
        if model is not None or uninstall or yes:
            UI.error("--model, --yes and --uninstall go with vllm.")
            raise typer.Exit(2)
        _to_llama()
        return
    if uninstall:
        if model is not None:
            UI.error("--uninstall takes no --model.")
            raise typer.Exit(2)
        _uninstall()
        return
    _to_vllm(model, yes)
