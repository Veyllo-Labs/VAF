# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf env`: your sandbox environments, from the terminal.

The same object the agent's tools and the web UI use (vaf/core/environments.py): list,
create, run a command, open a shell, read a background process's output, take a preview,
stop, delete, and clear what expired. The command runs as the machine owner, like
`vaf secrets` and `vaf ssh`, behind the same terminal door: an environment holds the
owner's code and can reach what its network allows.

`list --all` and `delete --all-owners` are the machine owner's view of everybody's
environments (the terminal user is the admin of this machine). Nothing here runs code in
another person's environment.
"""
from typing import Optional

import typer

from vaf.cli.ui import UI

app = typer.Typer(help="Your sandbox environments: containers to run, install and test code in.")


@app.callback()
def _group():
    """Your sandbox environments: containers to run, install and test code in."""


def _scope():
    from vaf.core.identity_binding import resolve_owner_identity
    return resolve_owner_identity().scope


def _manager():
    from vaf.core.environments import get_environment_manager
    return get_environment_manager()


def _fail(exc) -> None:
    UI.error(str(exc))
    raise typer.Exit(1)


@app.command("list")
def list_envs(all_owners: bool = typer.Option(False, "--all", help="Everybody's environments (admin view)")):
    """Your environments, or with --all everybody's."""
    from vaf.core.environments import EnvironmentRefused
    try:
        envs = _manager().list(_scope(), everyone=all_owners)
    except EnvironmentRefused as e:
        _fail(e)
    if not envs:
        UI.info("No sandbox environments.")
        return
    for env in envs:
        owner = f"  owner {env.owner}" if all_owners else ""
        print(env.describe() + owner)


@app.command("create")
def create(
    temp: bool = typer.Option(False, "--temp", help="A temporary environment (removed a day after its last use)"),
    project: Optional[str] = typer.Option(None, "--project", help="A project environment with this name"),
    path: Optional[str] = typer.Option(None, "--path", help="Project folder to mount at /workspace"),
    network: Optional[str] = typer.Option(None, "--network", help="none, registries or open"),
    memory: Optional[int] = typer.Option(None, "--memory", help="Memory limit in MB"),
):
    """Create an environment. Waits for the environment image the first time it is built."""
    from vaf.core.environments import EnvironmentRefused
    if temp == bool(project):
        UI.error("Say --temp or --project NAME (one of them).")
        raise typer.Exit(2)
    if path and not project:
        UI.error("--path needs --project: a temporary environment has no project folder.")
        raise typer.Exit(2)
    try:
        env = _manager().create(_scope(), kind="project" if project else "temporary",
                                name=project or "", project_path=path, network=network,
                                memory_mb=memory, wait_for_image=True)
    except EnvironmentRefused as e:
        _fail(e)
    UI.success(f"Created {env.describe()}")
    if env.degraded:
        UI.warning(env.degraded)


def _command_line(args) -> str:
    """The shell line the environment runs. Joining the words with spaces lost their
    boundaries: `python3 -c "print('a b')"` arrived as `python3 -c print('a b')`. Each word
    is quoted for the container's sh (POSIX, whatever the host is); one word alone is
    taken as the line the person wrote, so pipes and `&&` keep working."""
    import shlex
    words = [str(a) for a in args]
    if len(words) == 1:
        return words[0].strip()
    return shlex.join(words).strip()


@app.command("exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_cmd(
    ctx: typer.Context,
    env_id: str = typer.Argument(..., help="The environment's id"),
    timeout: int = typer.Option(600, "--timeout", help="Seconds the command may run"),
    background: bool = typer.Option(False, "--background", help="Keep it running and return its id"),
):
    """Run a command in an environment: vaf env exec ID -- COMMAND ...

    Several words are one command with its arguments, each kept whole
    (`-- python3 -c "print('a b')"`); a single quoted word is a shell line
    (`-- "pip install -r requirements.txt && pytest"`)."""
    from vaf.core.environments import EnvironmentRefused
    command = _command_line(ctx.args)
    if not command:
        UI.error("No command. Example: vaf env exec ID -- python3 --version")
        raise typer.Exit(2)
    try:
        if background:
            print(_manager().start_process(_scope(), env_id, command))
            return
        r = _manager().exec(_scope(), env_id, command, timeout=timeout)
    except EnvironmentRefused as e:
        _fail(e)
    if r.stdout:
        print(r.stdout, end="" if r.stdout.endswith("\n") else "\n")
    if r.stderr:
        UI.print(r.stderr.rstrip())
    if r.timed_out:
        UI.error(f"Timed out after {timeout}s.")
    raise typer.Exit(r.returncode if r.returncode >= 0 else 124)


@app.command("shell")
def shell(env_id: str = typer.Argument(..., help="The environment's id")):
    """An interactive shell in an environment (/workspace)."""
    import subprocess
    from vaf.core.environments import EnvironmentRefused
    from vaf.core.service_stack import resolve_docker_exe
    try:
        env = _manager().get(_scope(), env_id)
        _manager()._ensure_running(env)
    except EnvironmentRefused as e:
        _fail(e)
    raise typer.Exit(subprocess.call([resolve_docker_exe(), "exec", "-it", "-w", "/workspace",
                                      env.container, "bash"]))


@app.command("ps")
def ps():
    """Background processes in your environments."""
    from vaf.core.environments import EnvironmentRefused
    try:
        rows = _manager().processes(_scope())
    except EnvironmentRefused as e:
        _fail(e)
    if not rows:
        UI.info("No background processes.")
        return
    for row in rows:
        print(f"{row['handle']}: {row['state']} - {row['command'][:120]}")


@app.command("logs")
def logs(handle: str = typer.Argument(..., help="A process id from `vaf env ps`"),
         chars: int = typer.Option(4000, "--chars", help="How much of the end to show")):
    """The end of a background process's output."""
    from vaf.core.environments import EnvironmentRefused
    try:
        print(_manager().process_log(_scope(), handle, max_chars=min(max(200, chars), 200000)))
    except EnvironmentRefused as e:
        _fail(e)


@app.command("kill")
def kill(handle: str = typer.Argument(..., help="A process id from `vaf env ps`")):
    """Stop a background process."""
    from vaf.core.environments import EnvironmentRefused
    try:
        print(_manager().stop_process(_scope(), handle))
    except EnvironmentRefused as e:
        _fail(e)


@app.command("preview")
def preview(env_id: str = typer.Argument(..., help="The environment's id"),
            target: str = typer.Argument(..., help="A URL (localhost is the environment) or a path under /workspace"),
            out: str = typer.Option("preview.png", "--out", help="Where to save the screenshot")):
    """A screenshot of a page the environment serves, plus its console and text."""
    import base64
    from pathlib import Path
    from vaf.core.environments import EnvironmentRefused
    try:
        r = _manager().render(_scope(), env_id, target)
    except EnvironmentRefused as e:
        _fail(e)
    if not r.get("ok"):
        _fail(r.get("error") or "no screenshot")
    Path(out).write_bytes(base64.b64decode(r["screenshot_b64"]))
    UI.success(f"Saved {out}")
    if r.get("title"):
        print(f"Title: {r['title']}")
    for line in (r.get("page_errors") or []) + (r.get("console") or []):
        print(f"  {line}")


@app.command("stop")
def stop(env_id: str = typer.Argument(..., help="The environment's id"),
         all_owners: bool = typer.Option(False, "--all-owners", help="Also another person's (admin)")):
    """Stop an environment (its files stay)."""
    from vaf.core.environments import EnvironmentRefused
    try:
        UI.success(f"Stopped {_manager().stop(_scope(), env_id, admin=all_owners).id}")
    except EnvironmentRefused as e:
        _fail(e)


@app.command("delete")
def delete(env_id: str = typer.Argument(..., help="The environment's id"),
           yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask"),
           all_owners: bool = typer.Option(False, "--all-owners", help="Also another person's (admin)")):
    """Delete an environment: container, files and network."""
    from vaf.core.environments import EnvironmentRefused
    if not yes and not typer.confirm(f"Delete environment {env_id} and everything in it?"):
        raise typer.Exit(1)
    try:
        UI.success(f"Deleted {_manager().delete(_scope(), env_id, admin=all_owners).id}")
    except EnvironmentRefused as e:
        _fail(e)


@app.command("prune")
def prune():
    """Remove expired temporary environments, stop idle ones, clear crash leftovers."""
    summary = _manager().prune()
    UI.success(f"Removed {summary['removed']}, stopped {summary['stopped']}, "
               f"cleared {summary['orphans']} leftovers.")
