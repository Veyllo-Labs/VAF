# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Outbound channel sanitization (incident 2026-07-13 09:17, Telegram).

The sub-agent result drain hand-copied a SHORTER sanitizer chain than the
normal headless reply path and sent 1034 chars of untagged English
chain-of-thought to the user. One shared chain (_prepare_channel_outbound)
now serves both paths, including a conservative structural guard against
untagged CoT prefixes, and the drain falls back to a deterministic result
excerpt instead of "[No summary generated]" (invariant 4.3: deliver
something useful, never noise, never nothing).
"""
from pathlib import Path

import vaf.core.headless_runner as hr

# Condensed from the real leaked message (session telegram_1283674662 msg[102]).
LEAKED = """The librarian agent result says "Documents folder contains: 3 PDFs, 3 TXTs, 0 DOCXs (Total: 16 files)" - that doesn't sound like it actually deleted the wetter_berlin.html file.

Let me check - the update_automation succeeded though. And the wetter_berlin.html is still there? Let me check what's in the workspace now.

Actually, looking at this again - the librarian agent was asked to delete the file, but its result is just listing the Documents folder. That's odd. Let me verify the state.

Actually, let me check the workspace to confirm.

Der Librarian hat die Datei nicht gelöscht -- sie ist noch da. Ich versuch's nochmal mit klareren Anweisungen."""


def test_incident_leak_is_stripped_to_the_user_facing_tail():
    out = hr._prepare_channel_outbound(LEAKED)
    assert "Let me check" not in out
    assert "result says" not in out
    assert "Actually," not in out
    assert "Der Librarian hat die Datei nicht gelöscht" in out


def test_legit_replies_survive_untouched():
    # English users get legit English replies - the guard is structural, not
    # language-based, and must not trim real content.
    legit = ("Here is your summary:\n\nThe report covers three topics.\n\n"
             "Actually, one more thing worth noting: the data is from May.\n\n"
             "Let me know if you need details.")
    assert hr._prepare_channel_outbound(legit) == legit
    short = "Alles erledigt. Die Datei liegt unter ~/Documents/report.html."
    assert hr._prepare_channel_outbound(short) == short
    # A single deliberative opener without a second one stays untouched.
    single = "Hmm, das ist knifflig.\n\nAber machbar.\n\nIch lege los."
    assert hr._prepare_channel_outbound(single) == single


def test_chain_applies_all_stages():
    txt = ("<think>secret</think>[WORKFLOW_ASYNC:abc] internal\n"
           "Hallo Mert, dein Bericht ist fertig.")
    out = hr._prepare_channel_outbound(txt)
    assert "secret" not in out and "WORKFLOW_ASYNC" not in out
    assert "Hallo Mert" in out
    # internal-phrase net still blocks whole contaminated messages
    assert hr._prepare_channel_outbound("[TOOL BLOCKED] do not send this") == ""


def test_all_messenger_send_paths_use_the_shared_chain():
    # Copy-drift guard (Rule 2): every messenger send in the runner goes through
    # _prepare_channel_outbound - the incident WAS a drifted hand-copy.
    src = Path(hr.__file__).read_text(encoding="utf-8")
    assert src.count("_prepare_channel_outbound(") >= 6  # def + 3 normal paths + drain uses
    body = src.split("def _prepare_channel_outbound", 1)[1]
    assert "_sanitize_outgoing_message(out)" in body.split("def ", 1)[0], (
        "shared chain lost the internal-phrase net"
    )
    # the old short hand-copied chain must not exist anymore
    assert '"[No summary generated]"' not in src, (
        "drain regained the noise placeholder instead of the deterministic fallback"
    )


def test_drain_summary_based_on_chat_step_return():
    src = Path(hr.__file__).read_text(encoding="utf-8")
    assert "drain_summary_text = agent.chat_step(" in src, (
        "drain messenger summary must be based on chat_step's reasoning-stripped return"
    )
    assert "_prepare_channel_outbound(drain_summary_text" in src
