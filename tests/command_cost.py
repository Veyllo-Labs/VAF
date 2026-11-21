# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What one command check costs, split into VAF's own CPU time and shlex's work.

A timing gate on a command check whose text holds one long word times VAF's own CPU time and
counts shlex's work; it never times shlex. A text of short words may be timed whole: its tokens
stay far below glibc's 32 KiB mmap threshold, where the effect below cannot act.
`shlex.shlex.read_token` builds a token one character at a time (`self.token += nextchar`), so
each character allocates a new string while the old one is still alive, and the cost of one
long word depends on how much contiguous free room the heap has at that moment, not on VAF.
With glibc's mmap threshold pinned at 32 KiB (the LARGE ALLOCS axis of scripts/hostile_env.py)
one shlex pass over the same 60 K word measured 0.054, 0.158 or 0.307 s CPU depending on the
heap state, and repeated within 2 % in that state, so neither a best-of-N nor a re-measure
escapes it. The whole check of a 128 K one-word text at the length cap measured 7.4 to 8.6 s
CPU under that axis (about 1.05 s without it) and 11.8 s wall on a shared 4-vCPU runner, while
the CPU time outside shlex was 0.133 s in every regime.

So `measure_check` reports, for one run of the check:
- `total`: the CPU time (`time.process_time`) of the whole check;
- `own`: `total` minus the CPU time spent inside `shlex.split`, never below zero;
- `calls`: how often the check called `shlex.split`;
- `chars`: how many characters it handed to `shlex.split`, summed over those calls.

`own` is what a gate times; `calls` and `chars` are deterministic and judge shlex's work, so a
change that reads a text through shlex once more is still caught. `total` is for the message.

Named boundary: shlex's time is never judged, only its call count and its character count. A
change that made shlex's tokens longer without handing it more characters (joining words into
one token) would raise its cost and pass. shlex's cost on one long word is the length-capped
exception of vaf.core.command_policy, and the token is the user's own word.
"""
import shlex
import time
from typing import NamedTuple


class CheckCost(NamedTuple):
    total: float
    own: float
    calls: int
    chars: int


def measure_check(text, classify, profile="host_unattended"):
    """One run of `classify(text, profile=profile)` as a CheckCost. For the length of the run
    `shlex.split` on the shlex module is a wrapper that adds up its CPU time, its calls and the
    length of the text it was handed; the command check looks it up on the module at every
    call, and the original is back on every exit, an exception from `classify` included.
    `classify` is passed in, so a caller hands over its module's global at call time and a
    test that monkeypatches that global measures the fake."""
    original = shlex.split
    inside = [0.0, 0, 0]

    def counted(s, *args, **kwargs):
        inside[1] += 1
        inside[2] += len(s) if isinstance(s, str) else 0
        start = time.process_time()
        try:
            return original(s, *args, **kwargs)
        finally:
            inside[0] += time.process_time() - start
    shlex.split = counted
    try:
        start = time.process_time()
        classify(text, profile=profile)
        total = time.process_time() - start
    finally:
        shlex.split = original
    return CheckCost(total, max(total - inside[0], 0.0), inside[1], inside[2])
