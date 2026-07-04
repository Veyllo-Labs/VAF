# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Trust-state persistence must stay JSON-serializable.

`_norm_dir` normalizes via Platform.normalize_path, which returns a Path - if that Path
reaches `trusted_dirs`, `save_trust_state`'s json.dumps raises "Object of type PosixPath is
not JSON serializable". That silently broke "allow always" for every dangerous tool
(python_exec, host_bash): the trust was never saved AND the tool never ran (the gate raised
before dispatch). These tests pin that trusted_dirs are stored as strings and that saving is
robust even if a Path sneaks in.
"""
from pathlib import Path

import vaf.core.trust as trust
from vaf.core.trust import TrustState, _norm_dir


def test_norm_dir_returns_a_string():
    result = _norm_dir(Path("/tmp"))
    assert isinstance(result, str)


def test_mark_trusted_dir_roundtrips_as_strings(tmp_path, monkeypatch):
    store = tmp_path / "trust.json"
    monkeypatch.setattr(trust, "_trust_file", lambda: store)

    trust.mark_trusted_dir(Path("/tmp"))            # must not raise
    trust.set_tool_policy("host_bash", "allow")     # must not raise
    assert store.exists()
    state = trust.load_trust_state()
    assert all(isinstance(d, str) for d in state.trusted_dirs)
    assert state.tool_policies["host_bash"] == "allow"


def test_save_is_robust_if_a_path_sneaks_in(tmp_path, monkeypatch):
    # Belt-and-suspenders: even a Path object in trusted_dirs must not crash the save.
    store = tmp_path / "trust.json"
    monkeypatch.setattr(trust, "_trust_file", lambda: store)
    trust.save_trust_state(TrustState(trusted_dirs={Path("/tmp/x")}, tool_policies={}))
    reloaded = trust.load_trust_state()
    # The save str()s the Path; str(WindowsPath("/tmp/x")) == "\\tmp\\x", so compare against the
    # platform-native str() rather than a POSIX literal (real dirs go through resolve()/normalize).
    assert reloaded.trusted_dirs == {str(Path("/tmp/x"))}
    assert all(isinstance(d, str) for d in reloaded.trusted_dirs)
