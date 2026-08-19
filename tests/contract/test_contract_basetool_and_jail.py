# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: BaseTool declarations, the file_access hard error, self.log(), user_jail.

docs/EMBEDDING.md ("Writing a tool" plus the multi-tenant identity_kwargs /
file_access block) promises a stranger three things pinned here: the declarative
class attributes a tool sets and their defaults, the class-definition-time
TypeError that stops a file_access declaration from running UNCONFINED, and the
two supported runtime helpers - self.log(message) and vaf.user_jail. Error
messages are pinned by substring only; the exact prose may be reworded.
"""
import inspect
import os
from pathlib import Path

import pytest

import vaf
from vaf import BaseTool

# Synthetic scope (repo convention): obviously fake, can never collide with a
# host machine's configured local_admin_scope_id, so the jail resolves non-admin.
SYNTHETIC_SCOPE = "deadbeef-0000-0000-0000-000000000000"


class ZzContractProbeTool(BaseTool):
    """Minimal concrete tool for the log/schema pins.

    Synthetic zz_ name: module-scope BaseTool subclasses are safe (no global
    registry at class-definition time), but the name must stay clear of the
    repo's roster-scanning tests when this suite runs in-repo.
    """

    name = "zz_contract_probe"
    description = "Probe tool for the embedder contract suite"

    def run(self, **kwargs) -> str:
        return "ok"


@pytest.fixture()
def _debug_logging_on(monkeypatch):
    """Force the debug-log switch on for the self.log() pins.

    The switch reads the host's config file (default True); a host that turned
    debug_logs_enabled off would fail every log-write assertion without this.
    The pin here is the WRITE path, not the switch.
    """
    import vaf.core.log_helper as log_helper  # test-harness seam: the switch self.log() consults

    monkeypatch.setattr(log_helper, "is_debug_logging_enabled", lambda: True)


def _tool_log_lines(log_dir):
    files = sorted(Path(log_dir).glob("tools_*.log"))
    assert files, f"self.log() wrote no tools_<date>.log in {log_dir}"
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8").splitlines()


# -- 1. Declarative class attributes ----------------------------------------


def test_basetool_ships_the_documented_declaration_defaults():
    """EMBEDDING.md tells a tool author which knobs exist and that omitting one
    means the safe default; renaming or re-defaulting any of these breaks every
    third-party tool that relied on the documented default."""
    assert BaseTool.permission_level == "read"
    assert BaseTool.side_effect_class == "none"
    assert BaseTool.admin_only is False
    assert BaseTool.coder_only is False
    assert BaseTool.channel_restrictions == ()
    assert BaseTool.identity_kwargs == ()
    assert BaseTool.file_access is None
    assert BaseTool.input_examples == []
    assert BaseTool.result_is_deliverable is False
    assert isinstance(BaseTool.parameters, dict)


def test_run_is_abstract_and_a_runless_subclass_cannot_be_instantiated():
    """run() is the one method a tool MUST implement; a BaseTool that silently
    accepted a runless subclass would defer the failure to dispatch time."""
    assert inspect.isabstract(BaseTool)

    class ZzNoRunTool(BaseTool):
        name = "zz_no_run"

    with pytest.raises(TypeError):
        ZzNoRunTool()


# -- 2. file_access declaration: hard error at class definition time ---------


def test_an_invalid_file_access_mode_raises_typeerror_at_class_definition():
    """The mode vocabulary is closed; a typo must fail when the class is
    defined, not silently run without a boundary."""
    with pytest.raises(TypeError, match=r"'read', 'write' or None"):

        class ZzBadModeTool(BaseTool):
            name = "zz_bad_mode"
            file_access = "lesen"

            def run(self, **kwargs) -> str:
                return ""


def test_file_access_without_identity_kwargs_raises_the_unconfined_typeerror():
    """Documented hard error: without identity_kwargs the dispatcher passes no
    scope, user_jail installs nothing, and the tool would run UNCONFINED while
    looking confined. Class-definition time is the only moment before
    production where that is noticed."""
    with pytest.raises(TypeError) as excinfo:

        class ZzUnconfinedTool(BaseTool):
            name = "zz_unconfined"
            file_access = "write"

            def run(self, **kwargs) -> str:
                return ""

    message = str(excinfo.value)
    assert "identity_kwargs" in message
    assert "UNCONFINED" in message


def test_a_partial_identity_declaration_is_rejected_the_same_way():
    """Both 'user_scope_id' AND 'user_role' are required: the jail resolves
    admin-ness from the role, so a scope-only declaration is still a gap."""
    with pytest.raises(TypeError) as excinfo:

        class ZzHalfDeclaredTool(BaseTool):
            name = "zz_half_declared"
            file_access = "write"
            identity_kwargs = ("user_scope_id",)

            def run(self, **kwargs) -> str:
                return ""

    message = str(excinfo.value)
    assert "identity_kwargs" in message
    assert "UNCONFINED" in message


def test_the_full_identity_declaration_defines_cleanly_for_both_modes():
    """The accepted control case: the pairing EMBEDDING.md tells an author to
    write must keep defining without error, for 'read' and 'write' alike."""
    for mode in ("read", "write"):

        class ZzDeclaredTool(BaseTool):
            name = f"zz_declared_{mode}"
            file_access = mode
            identity_kwargs = ("user_scope_id", "user_role")

            def run(self, **kwargs) -> str:
                return ""

        assert issubclass(ZzDeclaredTool, BaseTool)


# -- 3. self.log() ------------------------------------------------------------


def test_self_log_writes_one_line_with_the_tool_name_to_the_dated_tools_log(
    contract_log_dir, _debug_logging_on
):
    """The supported way for a tool to log: tools_<date>.log in the VAF log
    directory (VAF_LOG_DIR redirect honored), tool name filled in."""
    ZzContractProbeTool().log("probe message")
    lines = _tool_log_lines(contract_log_dir)
    assert len(lines) == 1
    assert "[zz_contract_probe]" in lines[0]
    assert "probe message" in lines[0]


def test_self_log_never_raises_and_keeps_every_entry_on_one_line(
    contract_log_dir, _debug_logging_on
):
    """Documented: log() never raises - a broken log line must not be able to
    fail a tool call - and one call is one line, so a multiline message cannot
    corrupt the line-oriented log file."""
    tool = ZzContractProbeTool()
    tool.log(None)
    tool.log([1, "two", {"three": 3}])
    tool.log("first half\nsecond half\rthird piece")
    lines = _tool_log_lines(contract_log_dir)
    assert len(lines) == 3, "each log() call must append exactly one line"
    multiline_entry = lines[2]
    assert "first half" in multiline_entry
    assert "second half" in multiline_entry
    assert "third piece" in multiline_entry


def test_self_log_truncates_a_very_long_message(contract_log_dir, _debug_logging_on):
    """Domain logs have no rotation, so an unbounded message would be an
    unbounded file; the exact cap value is incidental, the truncation is not."""
    ZzContractProbeTool().log("x" * 50_000)
    lines = _tool_log_lines(contract_log_dir)
    assert len(lines) == 1
    assert len(lines[0]) < 10_000, "a 50k message must be truncated on disk"


# -- 4. get_schema() ----------------------------------------------------------


def test_get_schema_returns_the_openai_function_shape():
    """Tool schemas cross into every provider backend in this exact envelope;
    a reshaped dict breaks each embedder's own schema plumbing."""
    schema = ZzContractProbeTool().get_schema()
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == "zz_contract_probe"
    assert isinstance(function["description"], str) and function["description"]
    assert isinstance(function["parameters"], dict)


# -- 5. user_jail --------------------------------------------------------------


def test_user_jail_is_callable_from_the_facade_and_a_falsy_scope_is_a_no_op():
    """Documented direct-consumer case (coder, workflow engine, automations):
    no scope means no jail, and the context manager must still enter and exit
    cleanly rather than force callers to special-case scopeless runs."""
    assert callable(vaf.user_jail)
    with vaf.user_jail("", None):
        pass


def test_user_jail_write_mode_enters_and_exits_cleanly_without_touching_the_filesystem(
    monkeypatch,
):
    """mode='write' is pure path composition: entering the jail must need no
    real per-user directories, so an embedder can wrap any tool body without
    provisioning anything first. Pinned by recording every directory-creation
    attempt during a steady-state entry."""
    jail = vaf.user_jail
    with jail(SYNTHETIC_SCOPE, "user", mode="write"):
        pass  # warm-up: the jail's function-local imports resolve before we patch
    created = []
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: created.append(str(self)))
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: created.append(str(a[0]) if a else "?"))
    monkeypatch.setattr(os, "mkdir", lambda *a, **k: created.append(str(a[0]) if a else "?"))
    with jail(SYNTHETIC_SCOPE, "user", mode="write"):
        pass
    assert created == [], f"user_jail(mode='write') created directories: {created}"


def test_user_jail_mode_is_keyword_only():
    """The signature is user_jail(user_scope_id, user_role=None, *, mode=...);
    a positional third argument must fail loudly instead of being read as a
    mode, because a silent reinterpretation would change which roots a tool
    may touch."""
    with pytest.raises(TypeError):
        with vaf.user_jail(SYNTHETIC_SCOPE, "user", "write"):
            pass
