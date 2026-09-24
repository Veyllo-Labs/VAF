# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Credential redaction in the approval preview, form by form.

The dialog is the control on the one lane that runs unsandboxed, so a secret it shows is a
secret handed to whoever looks at the screen, the event stream or events.jsonl. Measured
before this table existed, against the JSON form the preview is built from:

- `curl -u alice:secret`, `--user`, `lftp -u alice,secret`, `sshpass -p`, `mysql -psecret`,
  `Authorization: Basic`, `X-Api-Key`, `export FTP_PASS=...` were shown in full;
- `--password="s3cr3t"` showed the password AND reported it redacted - the value pattern
  stopped at the JSON escape and replaced only the backslash;
- `https://alice:p@ss@host` leaked the part of the password after its own `@`;
- `--password=secret` counted two secrets for one.

Every row runs through `build_preview`, the function the gate calls, so the table measures
what the dialog receives rather than what a regex does in isolation.
"""
import pytest

from vaf.core.arg_preview import SECRET_ASSIGNMENT, build_preview

SECRET = "s3cr3tVALUE9"


def _preview(command):
    return build_preview("host_bash", {"command": command})


@pytest.mark.parametrize("command", [
    f"curl -u alice:{SECRET} ftp://h/",
    f"curl --user alice:{SECRET} ftp://h/",
    f"curl --user=alice:{SECRET} ftp://h/",
    f"curl -u 'alice:{SECRET}' ftp://h/",
    f'curl -u "alice:{SECRET}" ftp://h/',
    f"lftp -u alice,{SECRET} h",
    f"sshpass -p {SECRET} ssh h",
    f"mysql -u root -p{SECRET} db",
    f"wget --password {SECRET} http://h",
    f"wget --password={SECRET} http://h",
    f'mysql --password="{SECRET}" db',
    f"mysql --password='{SECRET}' db",
    f"curl -H 'Authorization: Basic YWxpY2U6{SECRET}' http://h",
    f"curl -H 'Authorization: Bearer abc{SECRET}' http://h",
    f"curl -H 'X-Api-Key: {SECRET}' http://h",
    f"export FTP_PASS={SECRET}",
    f"DB_PASSWORD={SECRET} ./run",
    f"curl 'http://h/x?api_key={SECRET}&q=1'",
    f"curl ftp://alice:{SECRET}@h/",
    f"curl ftp://alice:p@{SECRET}@h/",
])
def test_the_secret_never_reaches_the_dialog(command):
    pv = _preview(command)
    assert SECRET not in pv["text"], pv["text"]
    assert pv["redacted"] >= 1
    assert "[redacted]" in pv["text"]


@pytest.mark.parametrize("command", [
    f"wget --password={SECRET} http://h",
    f'mysql --password="{SECRET}" db',
    f"curl -u alice:{SECRET} ftp://h/",
])
def test_one_secret_counts_once(command):
    assert _preview(command)["redacted"] == 1


def test_a_json_argument_named_like_a_secret_is_hidden():
    pv = build_preview("setup_mail", {"account": "alice@example.org", "password": SECRET})
    assert SECRET not in pv["text"]
    assert "alice@example.org" in pv["text"], "the reader must still see what was passed"


def test_the_reader_still_sees_what_was_passed():
    text = _preview(f"curl -u alice:{SECRET} ftp://h/")["text"]
    assert "curl -u alice:" in text and "ftp://h/" in text
    text = _preview(f"curl ftp://alice:{SECRET}@h/")["text"]
    assert "ftp://alice:" in text and "@h/" in text, "the user and host must stay readable"


@pytest.mark.parametrize("command", [
    "systemctl restart nginx",
    "ls -u /tmp",
    "git log --oneline -5",
    "curl -u $FTP_USER:$FTP_PASS ftp://h/",
    "echo the password is in the vault",
    "curl -H 'Authorization: Bearer $TOKEN' http://h",
])
def test_ordinary_commands_and_variable_references_stay_readable(command):
    pv = _preview(command)
    assert pv["redacted"] == 0, pv["text"]


@pytest.mark.parametrize("line,expected", [
    ("/ws?token=abc.def", "/ws?token=***"),
    ("/x?api_key=SECRET&q=hi", "/x?api_key=***&q=hi"),
    ("/x?q=hello&page=2", "/x?q=hello&page=2"),
])
def test_the_access_log_uses_the_same_assignment_rule(line, expected):
    from vaf.core.log_helper import _SECRET_QS_RE

    assert _SECRET_QS_RE is SECRET_ASSIGNMENT
    assert SECRET_ASSIGNMENT.sub(r"\1***", line) == expected
