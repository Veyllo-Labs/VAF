# CLAUDE.md — Working rules for AI sessions in this repo

These rules are binding for every AI-assisted session (Claude Code or otherwise).
They exist because violations of each one already caused real damage. Do not skip them.

## Rule 1 — READ THE DESIGN DOC BEFORE TOUCHING A SUBSYSTEM (always)

Before changing ANY file in a subsystem, read its design doc first. No exceptions —
"I know what this does" is how the Veyllo provider gap happened (a provider was added
centrally while the coder's private endpoint map was missed; the coder then silently
generated with the local model). Understand first, then build.

| You are about to change… | Read FIRST |
|---|---|
| `vaf/tools/coder.py`, coder templates | [docs/agents/CODER_ARCHITECTURE.md](docs/agents/CODER_ARCHITECTURE.md) |
| Sub-agent spawn/IPC/results (`subagent_ipc.py`, `cli/cmd/subagent.py`, spawn paths) | [docs/agents/SUBAGENT_IPC.md](docs/agents/SUBAGENT_IPC.md) |
| Main agent loop (`vaf/core/agent.py` chat_step/guardrails/tools) | [docs/agents/AGENT_LOOP.md](docs/agents/AGENT_LOOP.md) |
| Tool routing / registration | [docs/agents/TOOL_ROUTER_ARCHITECTURE.md](docs/agents/TOOL_ROUTER_ARCHITECTURE.md), [docs/agents/TOOLS_CATALOG.md](docs/agents/TOOLS_CATALOG.md) |
| Providers / models / `api_backend.py` / anything provider-gated | [docs/llm/PROVIDER_MODES.md](docs/llm/PROVIDER_MODES.md), [docs/llm/LLM_BACKEND_FACTS.md](docs/llm/LLM_BACKEND_FACTS.md), [docs/llm/API_INTEGRATION.md](docs/llm/API_INTEGRATION.md) |
| Config keys / defaults | [docs/setup/CONFIG_SCHEMA.md](docs/setup/CONFIG_SCHEMA.md) |
| WebSocket events / SubAgent window / live streams | [docs/web-ui/WEBUI_WEBSOCKET_FLOW.md](docs/web-ui/WEBUI_WEBSOCKET_FLOW.md) |
| Web UI (any component) | [docs/web-ui/WEB_UI.md](docs/web-ui/WEB_UI.md); mobile: [docs/web-ui/MOBILE_UI.md](docs/web-ui/MOBILE_UI.md) (desktop must stay byte-identical) |
| Context/compression/memory | [docs/memory/CONTEXT_MANAGEMENT.md](docs/memory/CONTEXT_MANAGEMENT.md), [docs/memory/CONTEXT_COMPRESSION_FLOW.md](docs/memory/CONTEXT_COMPRESSION_FLOW.md) |
| Workflows / engine | [docs/agents/WORKFLOW_SELECTION.md](docs/agents/WORKFLOW_SELECTION.md), [docs/agents/TOOL_SUPERVISION.md](docs/agents/TOOL_SUPERVISION.md) |
| Sandboxing / security / user isolation | [docs/security/SANDBOXING.md](docs/security/SANDBOXING.md), [docs/security/USER_ISOLATION.md](docs/security/USER_ISOLATION.md) |
| Installers (`install.ps1`, `install.sh`, packaging) | [docs/setup/INSTALLATION_GUIDE.md](docs/setup/INSTALLATION_GUIDE.md) + the platform setup doc |
| Thinking mode / proactive runs | [docs/agents/Thinking-Mode.md](docs/agents/Thinking-Mode.md) |

If the doc contradicts the code, say so and fix the doc in the same change — never
leave a design doc describing behavior that no longer exists.

## Rule 2 — Central registries have COPIES. Find them before adding anything.

Adding a provider/tool/config/event in one place is never enough. Known cross-cutting
registries (grep for ALL of them when you touch one):

- **Providers**: `config.py PROVIDER_MODELS` + `api_backend.py` factory +
  `vaf/tools/coder.py coder_api_providers()` (CI-guarded: `tests/test_coder_provider_map.py`)
  + Settings selectors (web) + `docs/llm/PROVIDER_MODES.md` catalog row (mandatory).
- **Config keys**: `config.py DEFAULTS` + `docs/setup/CONFIG_SCHEMA.md` row AND its
  key-count line. The `subagent_`/global prefixes decide write permissions — the key
  NAME is a security decision.
- **WebSocket coder/sub-agent state**: backend payload fields must be explicitly
  forwarded in `web/app/page.tsx` (the handler rebuilds objects field-by-field — an
  unforwarded field is silently dropped; this bug happened twice: `diffs`, `activity`).
- **Sub-agent behavior**: the four SUBAGENT_TOOLS are dispatched in `agent.py`,
  spawned in `cli/cmd/subagent.py`, engine-run in `workflows/engine.py` — a change in
  one usually needs the other two.

Prefer a CI guard over a prose rule: if two places must stay in sync, write a test
that fails when they drift (pattern: `tests/test_coder_provider_map.py`).

## Rule 3 — Never guess. Verify against the runtime.

- Debug from artifacts: session JSON, `logs/queue_*.log`, `logs/debug/coding_agent/*/events.jsonl`,
  IPC state in `~/.vaf/subagent_queue/`. State your root cause with file:line evidence
  before writing a fix.
- Unit tests passing is NOT proof: the live app needs a restart for `vaf/core` changes
  (sub-agent subprocesses reload `coder.py` per run without a restart). Say explicitly
  which changes need a restart.
- A tool result claiming success can lie (e.g. `python_sandbox` "Saved:" writes into an
  ephemeral container). Verify effects on the host filesystem when persistence matters.

## Rule 4 — Hard invariants (violating any of these caused a real incident)

1. **Tool-call adjacency**: never append system/user messages into `history` inside a
   tool-execution loop; every early `continue`/`break` must answer the pending
   `tool_call` first. `_normalize_tool_adjacency` enforces it pre-send — do not bypass.
2. **Never erase a streamed reply.** Guardrails may append notes; they must not clear
   or regenerate what the user already saw.
3. **Sub-agent results are delivered exactly once, by the headless runner drain**, with
   a deterministic fallback — never silently dropped, never consumed mid-chat-turn.
4. **User isolation**: anything session-scoped must key on the session (e.g.
   `ipc.get_active_tasks_for_current_session()`); never build prompt content from
   process-global state like `agent._async_subagent_tasks` (cross-user leak).
   Automations run with `agent._background_run = True` — respect it in prompt gates.
5. **Env-var hygiene**: `VAF_IN_SUBAGENT_TERMINAL` (and siblings) set in the main
   process must be restored on EVERY exit path (finally/except) — a leak makes every
   coder run execute in-process and serializes all chat behind it.
6. **Local vs API**: local mode means ONE llama server — never encourage or hardcode a
   second concurrent inference; API-only features gate on
   `provider != "local" AND api_backend is not None` (provider alone lies after an
   API-init failure).
7. **Data-model boundaries coerce types** (e.g. task titles to `str` in
   `persistence.Task.__post_init__`): model-shaped input AND previously persisted files
   both pass through constructors — fix at the boundary, not the call site.
8. **Windows installer stays pure ASCII** and never parses localized command output;
   status checks must not require admin (CIM reads instead of feature queries).

## Rule 5 — House conventions (non-negotiable)

- **Commits**: English messages; end with the `Co-authored-by: VAF Agent <noreply@veyllo.app>`
  trailer as the very last line (two blank lines before it, `--cleanup=verbatim`).
  Commit only when asked; NEVER push without explicit permission; no feature branches
  (solo repo, direct to main).
- **Every new `.py`/`.ts`/`.tsx` under `vaf/`, `tests/`, `scripts/`, `web/`** needs the
  3-line SPDX header (CI enforces it).
- **Docs**: English only, no emojis, no references to internal plan documents.
  `CHANGELOG.md [Unreleased]` gets an entry for every user-facing change.
- **Before declaring work done**, run the local CI equivalents: full pytest, Ruff gate
  (`--select=E9,F63,F7,F82`), `scripts/check_doc_links.py`,
  `scripts/check_license_headers.py`, and `npm run build` for web changes. Use the repo
  venv (`venv/bin/python`) — bare `python3` lacks the dependencies.
- **The user decides scope**: plan first for non-trivial work, surface cascade risks,
  and ask before destructive or outward-facing actions.
