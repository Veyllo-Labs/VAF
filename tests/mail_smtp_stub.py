# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A one-connection SMTP server on 127.0.0.1 for the tests of vaf/mail/sender.py.

It records every line it reads, raw, so a test sees the exact bytes on the wire. Replies are
configurable per verb: `mail`, `rcpt` (one reply per RCPT, in order; the last one repeats),
`data` (the answer to the DATA command itself) and `end` (the final answer after the dot).
`strict=True` answers the dot with `550 5.5.2` when any line of the message ended in a bare LF
or held a bare CR, which is what a server hardened against SMTP smuggling does.
`drop_after_dot=True` closes the connection instead of answering the dot (a hand-off whose
outcome the client cannot know).
"""
from __future__ import annotations

import re
import socket
import threading
from typing import List, Optional, Sequence

_BARE_LF = re.compile(rb"(?<!\r)\n")
_BARE_CR = re.compile(rb"\r(?!\n)")


class SmtpStub:
    def __init__(self, smtputf8: bool = False, *, strict: bool = False,
                 mail: bytes = b"250 ok", rcpt: Optional[Sequence[bytes]] = None,
                 data: bytes = b"354 go", end: bytes = b"250 queued",
                 drop_after_dot: bool = False):
        self.smtputf8, self.strict = smtputf8, strict
        self.mail_reply, self.rcpt_replies = mail, list(rcpt or [b"250 ok"])
        self.data_reply, self.end_reply = data, end
        self.drop_after_dot = drop_after_dot
        self.lines: List[bytes] = []
        self.message = b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _rcpt_reply(self, n: int) -> bytes:
        return self.rcpt_replies[min(n, len(self.rcpt_replies) - 1)]

    def _run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(10)
        f = conn.makefile("rb")
        n_rcpt = 0
        try:
            conn.sendall(b"220 stub\r\n")
            while True:
                line = f.readline()
                if not line:
                    break
                self.lines.append(line)
                up = line.upper()
                if up.startswith(b"EHLO"):
                    conn.sendall(b"250-stub\r\n250-8BITMIME\r\n"
                                 + (b"250-SMTPUTF8\r\n" if self.smtputf8 else b"")
                                 + b"250 AUTH PLAIN\r\n")
                elif up.startswith(b"AUTH"):
                    conn.sendall(b"235 ok\r\n")
                elif up.startswith(b"MAIL"):
                    conn.sendall(self.mail_reply + b"\r\n")
                elif up.startswith(b"RCPT"):
                    conn.sendall(self._rcpt_reply(n_rcpt) + b"\r\n")
                    n_rcpt += 1
                elif up.startswith(b"DATA"):
                    conn.sendall(self.data_reply + b"\r\n")
                    if not self.data_reply.startswith(b"354"):
                        continue
                    body = []
                    while True:
                        chunk = f.readline()
                        self.lines.append(chunk)
                        if chunk in (b".\r\n", b""):
                            break
                        body.append(chunk)
                    self.message = b"".join(body)
                    if self.drop_after_dot:
                        break
                    if self.strict and (_BARE_LF.search(self.message)
                                        or _BARE_CR.search(self.message)):
                        conn.sendall(b"550 5.5.2 bare LF or CR in message data\r\n")
                    else:
                        conn.sendall(self.end_reply + b"\r\n")
                elif up.startswith(b"QUIT"):
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"250 ok\r\n")
        except OSError:
            pass
        finally:
            f.close()
            conn.close()

    def close(self):
        self.sock.close()
        self.thread.join(timeout=5)

    def command(self, verb: bytes):
        return [line for line in self.lines if line.upper().startswith(verb)]


def verb_lines(stub: SmtpStub, verb: bytes):
    """The ``verb`` commands (MAIL, RCPT) with the part up to the first colon lowercased:
    smtplib writes "mail FROM:" and "rcpt TO:" on older Pythons (3.12 on CI) and "mail from:"
    and "rcpt to:" on 3.13; the arguments after the colon stay exactly as sent."""
    out = []
    for line in stub.command(verb):
        head, sep, rest = line.partition(b":")
        out.append(head.lower() + sep + rest)
    return out
