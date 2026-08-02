# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The full-screen terminal app behind `vaf run`.

A Textual application that drives the SAME in-process engine as the classic
interactive lane, through the same seams (`chat_step`, `set_event_sink`, the
gate's `register_gate`/`resolve_gate` contract, `load_session_context`) - no
second implementation of a turn exists here. Textual is imported lazily by the
command body only, so `import vaf` and `vaf --version` stay slim.

Module map: `theme_bridge` (vaf themes -> Textual themes; the agent's white
dot), `widgets` (transcript + chrome), `screens` (all overlays; every one is
keyboard-complete), `agent_bridge` (the single agent lane: boot, turns, drain,
event dispatch, the gate responder), `app` (assembly + key/word routing).
"""
