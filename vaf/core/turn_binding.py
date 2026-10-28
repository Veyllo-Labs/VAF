# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One turn bound onto a shared engine: whose turn it is, which conversation, which markers.

VAF's runner builds ONE agent per worker and serves every chat and every account of the
machine with it, through three lanes in one loop: a queued task, an agent room's wake and the
drain that summarizes finished sub-agent work. Each lane used to set what it knew by hand and
leave the rest standing, so a turn ran on what the previous one left behind. Measured, with
the real agent and only the provider faked:

- an account's new chat started with the previous account's context summary, decision and
  goal in its system message, and the tools the previous chat had used in its tool array;
- an agent room's turn ran inside whichever chat had been loaded last, with that chat's
  history, working memory and project file, and that chat's next request carried the room's
  exchange; its tools worked in that chat's folder and its sub-agents were stamped with that
  chat's session;
- the drain's summary turn ran with the role the last queued turn left (a user's chat drained
  as an admin) and the source of whatever task ran last.

This module is the one place a lane binds a turn: `bind_turn(agent, TurnBinding(...))` before
the turn, `end_turn(agent)` after it. A lane never writes a binding attribute itself
(guarded by tests/test_runner_turn_binding.py, the single-writer test).

THE FIVE CLASSES. Every attribute an agent carries is in `AGENT_STATE`, in one of five
lifetimes (guarded by tests/test_turn_binding.py, which reads the attributes from the code):

- ``engine``: the agent's life, correct to share between chats and accounts: the config,
  the backend, the tool registry and its caches, the hooks, the locks, the lane flags set once
  by the lane that built the agent (`_background_run` and its siblings). `_resident_tools` is
  one of them on purpose: on a provider that restricts callability separately it keeps the
  tool array byte-identical for the prompt cache, while the callable set is the turn's own.
  NAMED BOUNDARY: `_ww_training` is no such flag. A tool-training run sets it around its own
  calls, on the runner's shared agent too, and while it holds those calls skip the
  confirmation and turn gates; a turn the runner runs meanwhile on the same agent skips them
  as well. It is not scoped per context yet.
- ``step``: reset by `chat_step` itself at its start, per round or inside one call.
- ``chat``: one conversation (the history, the context state, the working memory, the recent
  tools). Swapped by `load_session_context`, or started fresh by `init_chat`
  (`Agent._reset_chat_state`).
- ``account``: one account's state, which the binding neither swaps nor resets: the engine
  keeps it per account and reads it through the bound identity. `_pending_user_question` (the
  ask-first latch) is a property keyed by the bound scope (`Agent._pending_user_question`).
  NAMED BOUNDARY: `_room_reply_streak` is keyed by room only, and any account's person clears
  it for every room (`Agent.note_human_turn`).
- ``binding``: one turn, set by the lane. Only `bind_turn` and `end_turn` write them.

Plus four per-context variables in `vaf.core.subagent_ipc`: the session, the turn, the room
and the conversation of its own. `bind_turn` declares all four.

THE ORDER inside `bind_turn` puts everything the prompt reads in place before the
conversation's prompt is built: the account check (nothing has changed when it refuses), the
identity and the markers, the context variables, then the conversation (a session load with
the identity re-asserted on its success path, `identity_binding`'s rule, or a fresh
conversation of its own), and last the chat's project folder, which the load resets.

FORWARD ONLY. Nothing is rolled back after a turn: the next `bind_turn` replaces every binding
attribute, so a restore would put a finished turn's values back for nothing.
`end_turn` clears what gates and routes (the allowed set, the Front Office and room markers,
the room id) for the code that runs between two turns; the identity, the source and the loaded
conversation stay until the next bind. `identity_binding` ships no context manager for the
same reason, and neither does this module.

NAMED BOUNDARY: not on the public facade. The facade fixes its scope and session at
construction (`vaf.Agent(user_scope=..., session=...)`) and never moves its engine between
conversations, and an embedder who serves several conversations builds one `Agent` per
conversation; N embedder lanes that move one engine between conversations: 0. A binding also
requires a chat source, which makes `Agent._project_context_block` a runner lane, while the
facade is a terminal lane by design. The export is one lazy `__getattr__` branch the day an
embedder serves several conversations from one engine.

NAMED BOUNDARY: the CLI's own web mode. `vaf run --web` hosts the web server in the CLI
process without the in-process runner, and the CLI loop takes the browser's queued chats
itself with only `load_session_context`: no task identity, role or account check. It answers
nobody else: a messenger or mail task, and any task a contact sent, is declined there
(`_declined_queue_task` in vaf/cli/cmd/run.py), so no Front Office set is owed; and while a
VAF service runs, that mode starts no server, and the service's runner serves the browser.
Binding the browser's chats here would be a second copy of the queue lane's value
computation; whether that mode hands its queue to the runner or binds through
`_task_turn_binding` is a decision for that mode, measured first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from vaf.core.identity_binding import Identity, bind_identity, reassert_identity

__all__ = [
    "AGENT_STATE",
    "TurnBinding",
    "TurnRefused",
    "bind_turn",
    "end_turn",
    "room_turn_marker",
]

_ENGINE, _STEP, _CHAT, _ACCOUNT, _BINDING = "engine", "step", "chat", "account", "binding"

#: Every attribute an Agent carries, by lifetime. The single source the guards read.
AGENT_STATE: dict = {
    # engine: the agent's life
    "api_backend": _ENGINE, "base_dir": _ENGINE, "config": _ENGINE, "devnull": _ENGINE,
    "filename": _ENGINE, "is_gemma_local": _ENGINE, "llm": _ENGINE,
    "model_display_name": _ENGINE, "model_mode": _ENGINE, "model_path": _ENGINE,
    "models_dir": _ENGINE, "old_stderr": _ENGINE, "provider": _ENGINE, "repo_id": _ENGINE,
    "server": _ENGINE, "state_registry": _ENGINE, "tool_activity": _ENGINE, "tools": _ENGINE,
    "use_server": _ENGINE, "verbose": _ENGINE, "workspace": _ENGINE,
    "_api_init_error": _ENGINE, "_async_subagent_tasks": _ENGINE, "_atexit_called": _ENGINE,
    "_background_run": _ENGINE, "_backend_swap_lock": _ENGINE, "_compaction_hook": _ENGINE,
    "_config_overrides": _ENGINE, "_event_sink": _ENGINE, "_host_audio_allowed": _ENGINE,
    "_identity_is_owner_bound": _ENGINE, "_max_false_promise_retries": _ENGINE,
    "_mcp_server_status": _ENGINE, "_mcp_tool_names": _ENGINE, "_noninteractive": _ENGINE,
    "_recent_tool_keep_turns": _ENGINE, "_resident_tools": _ENGINE, "_run_kind": _ENGINE,
    "_runner_owns_subagent_delivery": _ENGINE, "_session_id": _ENGINE,
    "_shutdown_called": _ENGINE, "_system_prompt_override": _ENGINE,
    "_tokenizer_instance": _ENGINE, "_tool_authorizer": _ENGINE,
    "_tools_fs_last_check": _ENGINE, "_tools_fs_sig": _ENGINE, "_tools_schema_cache": _ENGINE,
    "_tools_schema_cache_key": _ENGINE, "_win_handler_ref": _ENGINE,
    "_ww_stale_checked": _ENGINE, "_ww_training": _ENGINE,
    # step: reset by chat_step itself
    "_active_tools": _STEP, "_anti_spin_streak": _STEP, "_autocontinue_step_sig": _STEP,
    "_autocontinue_stuck": _STEP, "_compaction_in_progress": _STEP, "_cross_chat_block": _STEP,
    "_cross_chat_hints": _STEP, "_current_turn_thinking_mode": _STEP,
    "_false_promise_retries": _STEP, "_force_tool_choice": _STEP,
    "_force_tool_choice_used": _STEP, "_forced_round": _STEP, "_held_this_round": _STEP,
    "_injected_context_chars": _STEP, "_last_language_mismatch": _STEP,
    "_nonprogress_streak": _STEP, "_note_tools_this_turn": _STEP,
    "_orchestrator_heavy_calls_this_turn": _STEP, "_pending_skill_hint": _STEP,
    "_pending_skill_match": _STEP, "_pending_workflow_hint": _STEP, "_plan_gate_blocks": _STEP,
    "_result_grounding_retries": _STEP, "_round_spawns": _STEP, "_subagent_round": _STEP,
    "_thinking_allow_search": _STEP, "_thinking_node": _STEP, "_thinking_read_counts": _STEP,
    "_thinking_reply_context": _STEP, "_thinking_reply_gate_armed": _STEP,
    "_thinking_reply_pending": _STEP, "_thinking_reply_user_text": _STEP,
    "_tool_exec_times": _STEP, "_turn_closed_by_tool": _STEP, "_turn_closing": _STEP,
    "_turn_failed": _STEP, "_turn_failure": _STEP, "_turn_ran_progress_tool": _STEP,
    "_workflow_selection_tier": _STEP,
    # chat: one conversation
    "history": _CHAT, "current_session_id": _CHAT, "main_persistence": _CHAT,
    "prompt_manager": _CHAT, "context_manager": _CHAT, "_is_new_session": _CHAT,
    "_recent_tools": _CHAT,
    # account: keyed by the account, read through the bound identity
    "_pending_user_question": _ACCOUNT, "_room_reply_streak": _ACCOUNT,
    # binding: one turn, written only here
    "_current_user_scope_id": _BINDING, "_current_username": _BINDING,
    "_current_user_role": _BINDING, "_current_chat_source": _BINDING,
    "_allowed_tools": _BINDING, "_front_office_mode": _BINDING, "_front_office_chat": _BINDING,
    "_front_office_contact": _BINDING, "_unattended_turn": _BINDING,
    "_turn_is_human": _BINDING, "_turn_wake": _BINDING, "_room_turn": _BINDING,
    "_synthetic_drain_turn": _BINDING, "_excluded_tools": _BINDING,
    "_chat_project_dir": _BINDING, "_turn_decision_note": _BINDING,
}


#: The keys of an agent room's turn marker (`TurnBinding.room_turn`, read off `_room_turn`), with
#: the value a lane that does not know one means. The closed value wherever it matters: not the
#: user's words, no authority, no shared folder.
ROOM_TURN_KEYS: dict = {
    "room_id": "", "mode": "", "query": "", "from_user": False, "from_user_only": False,
    "from_authority": False, "shared_folder": "",
}


def room_turn_marker(wake: Optional[dict]) -> dict:
    """The ``room_turn`` marker for a turn a room started, from what the lane learned of it.

    One list of keys for every lane that binds such a turn (the runner's room wake and an
    automation a room message starts), because a key computed by a lane and not handed over
    fails CLOSED in the gate that reads it, which is the quiet way for two lanes to drift.
    Unknown keys in ``wake`` are left out; missing ones get the closed value."""
    wake = wake or {}
    return {key: wake.get(key, default) for key, default in ROOM_TURN_KEYS.items()}


class TurnRefused(RuntimeError):
    """The bound account may not start work: its access was taken away
    (`vaf.core.revocation.account_stands`). Nothing on the agent has changed."""


@dataclass(frozen=True)
class TurnBinding:
    """Everything one turn is bound to, decided by the lane's producer before the turn.

    Exactly one of ``session_id`` (a chat, loaded through `load_session_context`) and
    ``conversation`` (a conversation of its own, started fresh, with its own working memory
    under that key) is given. Every other field has the value a lane that says nothing
    means: no allowed set, no Front Office, attended, a person typing (``is_human=None``,
    because the engine reads an absent marker as a person, `Agent._turn_is_from_a_person`),
    no wake, no room, not synthetic, nothing hidden, no project folder.

    Validated at construction, so a binding that cannot be applied raises before anything
    on the agent changes: ``ValueError`` for an empty source, for both or neither of the
    session and the conversation, for an empty one of them; ``TypeError`` for an allowed set
    given as one string (`tool_dispatch.normalize_allowed_tools`).
    """

    identity: Identity
    source: str
    session_id: Optional[str] = None
    # Whose access decides whether the turn may start (`revocation.account_stands`), when
    # the lane knows it apart from the identity: a queued web turn names the account that
    # enqueued it. None asks about the identity's scope.
    account: Optional[str] = None
    conversation: Optional[str] = None
    force_reload: bool = False
    turn_id: Optional[str] = None
    room_id: Optional[str] = None
    allowed_tools: Optional[frozenset] = None
    front_office: bool = False
    front_office_chat: Any = None
    front_office_contact: Optional[dict] = None
    unattended: bool = False
    is_human: Optional[bool] = None
    wake: Optional[tuple] = None
    room_turn: Optional[dict] = None
    synthetic: bool = False
    excluded_tools: Optional[frozenset] = None
    project_dir: str = ""

    def __post_init__(self) -> None:
        if not str(self.source or "").strip():
            raise ValueError("a turn needs a chat source")
        if self.session_id is not None and not str(self.session_id).strip():
            raise ValueError("an empty session id names no chat")
        if self.conversation is not None and not str(self.conversation).strip():
            raise ValueError("an empty conversation key would share one working memory")
        if (self.session_id is None) == (self.conversation is None):
            raise ValueError("a turn runs either on a chat or in a conversation of its own")
        from vaf.core.tool_dispatch import normalize_allowed_tools
        object.__setattr__(self, "allowed_tools", normalize_allowed_tools(self.allowed_tools))
        if self.excluded_tools is not None:
            object.__setattr__(self, "excluded_tools",
                               frozenset(str(n) for n in self.excluded_tools))


def _write_markers(agent: Any, binding: Optional[TurnBinding]) -> None:
    """Every binding attribute except the identity triple and the source, from ``binding``,
    or to the neutral value when ``binding`` is None. The one writer of these twelve: a
    marker one place sets and another forgets to clear is how a timer's "nobody is watching"
    or a contact's tool set used to outlive its turn."""
    b = binding
    agent._front_office_mode = bool(b.front_office) if b else False
    agent._front_office_chat = b.front_office_chat if b else None
    contact = b.front_office_contact if b else None
    agent._front_office_contact = dict(contact) if isinstance(contact, dict) else None
    agent._unattended_turn = bool(b.unattended) if b else False
    if b is None or b.is_human is None:
        try:
            del agent._turn_is_human
        except AttributeError:
            pass
    else:
        agent._turn_is_human = bool(b.is_human)
    agent._turn_wake = b.wake if b else None
    agent._room_turn = dict(b.room_turn) if (b and b.room_turn is not None) else None
    agent._synthetic_drain_turn = bool(b.synthetic) if b else False
    if b is None or b.excluded_tools is None:
        try:
            del agent._excluded_tools
        except AttributeError:
            pass
    else:
        agent._excluded_tools = b.excluded_tools
    # The project folder is the chat's, and the load below resets it; bind_turn writes it
    # once the conversation is in place.
    agent._chat_project_dir = ""
    # Per turn: chat_step writes this turn's note. A turn that ends before it (an early
    # workflow exit) must not leave the previous chat's note for this chat's session file.
    agent._turn_decision_note = None
    agent.set_allowed_tools(b.allowed_tools if b else None)


def bind_turn(agent: Any, binding: TurnBinding) -> None:
    """Bind this turn onto the agent: account, identity, markers, context, conversation.

    Raises `TurnRefused` when the account may not start work, before anything changes. Any
    other exception (a session that cannot be loaded, which fails closed in
    `load_session_context`; a conversation of its own whose store cannot be opened, which
    raises RuntimeError) propagates: the lane decides, and a turn that could not be bound
    does not run. No ``with`` and no rollback (module docstring).
    """
    from vaf.core.revocation import account_stands
    from vaf.core.subagent_ipc import (set_current_conversation_id, set_current_room_id,
                                       set_current_session_id, set_current_turn_id)

    account = binding.account if binding.account is not None else binding.identity.scope
    if not account_stands(account):
        raise TurnRefused("this account may not start work: its access was taken away")

    bind_identity(agent, binding.identity)
    agent._current_chat_source = str(binding.source)
    _write_markers(agent, binding)

    set_current_session_id(binding.session_id)
    set_current_turn_id(binding.turn_id)
    set_current_room_id(binding.room_id)
    set_current_conversation_id(binding.conversation)

    if binding.session_id is not None:
        agent.load_session_context(binding.session_id, force=bool(binding.force_reload))
        # The load names the SESSION's owner; the turn's identity is the caller's. On the
        # success path only: a failed load fails closed with the caller's identity standing.
        reassert_identity(agent, binding.identity)
    else:
        # A conversation of its own: no chat is loaded and none is left standing, so this
        # agent's next load of any chat rebuilds it from the store.
        agent.current_session_id = None
        agent._chat_project_dir = ""
        agent.init_chat()
        # After init_chat, which rebuilds the prompt manager with a store of its own.
        agent._bind_session_persistence(None, conversation=str(binding.conversation))
        # A conversation of its own whose store did not open does not run: it would run with
        # no working memory of its own. The binder already left no store bound, so nothing of
        # the chat this agent served before is reachable either.
        store = getattr(agent, "main_persistence", None)
        if getattr(store, "conversation", None) != str(binding.conversation).strip():
            raise RuntimeError("the working memory of this conversation could not be opened")

    agent._chat_project_dir = str(binding.project_dir or "")


def end_turn(agent: Any) -> None:
    """Clear what gates and routes once a turn is over: the allowed set, the Front Office
    and room markers, the person and the wake, the synthetic flag, the hidden tools, the
    project folder, the decision note, and this context's room and conversation of its own
    (what runs between two turns writes into no room's store). The identity, the source and
    the loaded conversation stay until the next `bind_turn` replaces them."""
    from vaf.core.subagent_ipc import set_current_conversation_id, set_current_room_id

    _write_markers(agent, None)
    set_current_room_id(None)
    set_current_conversation_id(None)
