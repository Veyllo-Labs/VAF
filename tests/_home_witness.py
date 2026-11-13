# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A witness for the suite's own isolation, run only as a child pytest.

tests/test_suite_writes_nowhere_real.py and tests/test_service_port_guard.py start a
pytest on this file with variables of their own; without those variables it does nothing.
The leading underscore keeps it out of every collection that does not name it.

- VAF_HOME_WITNESS_OUT: at import (that is, during collection) write `collect.txt` into
  `Path.home()` and into `Platform.data_dir()`, in the test write `Documents/run.txt` under
  the home, and at exit report the home and whether the service-port guard is still
  installed, as JSON, into that file.
- VAF_HOME_WITNESS_DIAL: in the test, dial 127.0.0.1 on that port and expect a refusal,
  whose message contains VAF_HOME_WITNESS_REFUSER when that is set.
"""
import atexit
import json
import os
import socket
from pathlib import Path

import pytest

_OUT = os.environ.get("VAF_HOME_WITNESS_OUT")

if _OUT:
    from vaf.core.platform import Platform

    (Path.home() / "collect.txt").write_text("written at collection", encoding="utf-8")
    _data = Platform.data_dir()
    _data.mkdir(parents=True, exist_ok=True)
    (_data / "collect.txt").write_text("written at collection", encoding="utf-8")

    def _report_at_exit():
        # Registered after tests/conftest.py was imported, so it runs before that file's
        # own exit handlers: the session root and its home still exist.
        report = {
            "home": str(Path.home()),
            "guard_at_exit": bool(getattr(socket.socket.connect,
                                          "_vaf_service_port_guard", False)),
        }
        with open(_OUT, "w", encoding="utf-8") as out:
            json.dump(report, out)

    atexit.register(_report_at_exit)


def test_witness():
    if _OUT:
        documents = Path.home() / "Documents"
        documents.mkdir(parents=True, exist_ok=True)
        (documents / "run.txt").write_text("written by a test", encoding="utf-8")
    port = os.environ.get("VAF_HOME_WITNESS_DIAL")
    if port:
        # Never dial a VAF port unguarded: unless the installed wrapper is the conftest guard
        # or hostile's counter refusing this very port, fail here instead of reaching
        # whatever listens on the machine.
        connect = socket.socket.connect
        assert (getattr(connect, "_vaf_service_port_guard", False)
                or int(port) in getattr(connect, "_vaf_hostile_refuses", ())), \
            "no wrapper that refuses this port is installed"
        with pytest.raises(ConnectionRefusedError) as refused:
            socket.create_connection(("127.0.0.1", int(port)), timeout=2)
        assert os.environ.get("VAF_HOME_WITNESS_REFUSER", "") in str(refused.value)
