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

Every step is ``local_engine.apply`` (and ``state``, ``uninstall_vllm``), the implementation
Settings > AI & Model and the full-screen TUI call too; this module only prints.

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


def _say(text: str, kind: str = "info") -> None:
    {"success": UI.success, "warning": UI.warning}.get(kind, UI.info)(text)


def _show() -> None:
    from vaf.core import local_engine as le
    state = le.state()
    vllm = state["vllm"]
    UI.info("Engine: " + ("vllm (vLLM, Hugging Face safetensors)" if state["engine"] == le.VLLM
                          else "llama (llama.cpp, GGUF)"))
    UI.info(f"vLLM on this machine: {state['support']['level']}. {state['support']['reason']}")
    version = vllm["installed_version"]
    if version and vllm["needs_install"]:
        UI.info(f"vLLM installed: {version}, set up by an earlier VAF; it cannot start until "
                "`vaf engine vllm` rebuilds it")
    else:
        UI.info(f"vLLM installed: {version}" if version
                else f"vLLM installed: no (VAF installs {vllm['pin']} with `vaf engine vllm`)")
    parsers = vllm["parsers"]
    UI.info(f"vLLM model: {vllm['model']}; tool-call parser {parsers['tool'] or 'unknown'}, "
            f"reasoning parser {parsers['reasoning'] or 'none'}")
    if state["engine"] == le.VLLM:
        from vaf.core import vllm_server as vs
        pid = vs.running_pid()
        UI.info(f"vLLM server: running (PID {pid})" if pid else "vLLM server: not running "
                "(it starts with the next use of the local model)")
    for conflict in state["conflicts"]:
        UI.warning(conflict["text"])


def _finish(result) -> None:
    """Print what ``local_engine.apply`` did and exit like `vaf profile`: 0 done, 1 not
    complete, 2 refused."""
    if result.status == "done":
        UI.success(result.text)
        return
    if result.note:
        UI.success(result.note)
    UI.error(result.text)
    if result.detail:
        UI.error(result.detail)
    raise typer.Exit(2 if result.status == "refused" else 1)


def _uninstall() -> None:
    from vaf.core import local_engine as le
    out = le.uninstall_vllm()
    if out["code"] == "not_installed":
        UI.info(out["text"])
        return
    if not out["ok"]:
        UI.error(out["text"])
        raise typer.Exit(2 if out["code"] == "engine_active" else 1)
    UI.success(out["text"])
    if out["kept_model"]:
        UI.info(f"The model {out['kept_model']} ({_gb(out['kept_bytes'])}) stays in the Hugging "
                f"Face cache ({out['kept_path']}); delete it there when you need the space.")
    else:
        UI.info("Models downloaded for vLLM stay in the Hugging Face cache.")


def _to_vllm(model: Optional[str], yes: bool) -> None:
    from vaf.core import local_engine as le
    result = le.apply(le.VLLM, model=model, log=_say)
    if result.status == "needs_install":
        UI.info(result.text)
        if not yes and not typer.confirm("Install it now?", default=False):
            UI.info("Nothing was installed; the local engine is unchanged.")
            raise typer.Exit(1)
        result = le.apply(le.VLLM, model=model, install=True, log=_say)
    _finish(result)


def _to_llama() -> None:
    from vaf.core import local_engine as le
    _finish(le.apply(le.LLAMA))


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
