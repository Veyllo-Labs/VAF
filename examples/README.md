# VAF Examples

Small, runnable scripts for the embedding surface documented in
[docs/EMBEDDING.md](../docs/EMBEDDING.md). Each file is self-contained and
commented; run them from the repo root with the repo venv (or any environment
where `pip install -e .` was done):

```bash
venv/bin/python examples/01_hello_agent.py
```

| Example | Shows |
|---|---|
| [01_hello_agent.py](01_hello_agent.py) | The five-line quickstart: construct an `Agent`, run one prompt, multi-turn state, plus a one-shot `complete()` that never touches the conversation |
| [02_streaming_and_events.py](02_streaming_and_events.py) | Live token streaming (`on_token`) plus the structured event sink (`tool_start`/`tool_end`/gate events) |
| [03_stream_json_subprocess.py](03_stream_json_subprocess.py) | Driving VAF as a subprocess via `vaf prompt --output-format stream-json` and parsing the NDJSON - the pattern for non-Python integrations |
| [04_inline_tool.py](04_inline_tool.py) | Per-instance tool registration with `agent.add_tool()` - no package, no file drop-in |
| [05_chatbot_with_memory.py](05_chatbot_with_memory.py) | A chatbot that survives restarts: `save_session()` + `Agent(session=...)` |
| [06_custom_persona.py](06_custom_persona.py) | Give the agent its own voice and instructions with `Agent(system_prompt=...)` |
| [07_tool_caller_and_authorizer.py](07_tool_caller_and_authorizer.py) | Running a tool with VAF's own rules but no conversation (`ToolCaller`), vetoing a call per user and per argument (`set_tool_authorizer`), and restricting which tools an account may use at all (`set_account_allowlist_resolver`). **Needs no provider, no API key and no network** - it never talks to a model |
| [08_session_storage_and_encryption.py](08_session_storage_and_encryption.py) | How conversations are stored: plaintext, plaintext with several tenants (`list()` vs `list_owned()`), encrypted at rest with `file_encryption_enabled`, and recovery after the machine key is gone. Greps the raw bytes to prove what is and is not readable on disk. **Needs no provider, no API key and no network**, and runs against a throwaway home directory so your own installation is untouched. It pins the master key to a file so the walk-through reads the same on every platform; a real install places that key per platform |
| [09_voice_turn.py](09_voice_turn.py) | Driving one live-call turn yourself with `VoiceTurnEngine`: you own the microphone, the transport and the text-to-speech, the engine owns the decision (noise gate, speech-to-text, speaker verification, reflex policy, reply, delegation) and hands back one `TurnOutcome`. The recognizer is injected through the `transcribe` seam, so it **needs no microphone and no speech extra** - only a model backend for the reply layer |
| [10_a2a_reference_peer.py](10_a2a_reference_peer.py) | A SECOND implementation of the agent-to-agent room protocol, written from [A2A_PROTOCOL.md](../docs/agents/A2A_PROTOCOL.md) and importing **nothing** from `vaf`: the frame rules, the five forward-compatibility rules, deduplication, canonical ordering and the role table. `tests/test_a2a_conformance.py` runs the conformance list against this module AND against VAF's own, so a rule only one of them keeps is a failure. That import ban is what makes it a proof the protocol is implementable rather than a self-test. **Needs nothing at all** - standard library only |
| [11_a2a_room.py](11_a2a_room.py) | Several agents in one conversation from the public facade: opening a room, joining it with the derived handle, reading the transcript in canonical order, finding the rooms a participant is in, and minting an invitation with the briefing that travels with it. Ends on the two refusals that matter - nobody commands in a round, and a closed room takes nothing more. **Needs no provider, no API key and no network**, and runs against a throwaway home directory |
| [vaf_example_tool/](vaf_example_tool/) | A complete installable pip package that adds a custom tool through the `vaf.tools` entry-point group |

## Prerequisites

- VAF installed (`pip install -e .` from the repo root, or a full product
  install).
- A working model backend: either an API provider configured in
  `~/.vaf/config.json` (fastest for a first test) or local mode (the first
  run downloads a multi-GB model). The one exception is
  `07_tool_caller_and_authorizer.py` and
  `08_session_storage_and_encryption.py`, which drive the tool and storage
  layers directly and need no backend at all - a good first pair to run if you
  just installed. The examples default to whatever your
  config says; `01_hello_agent.py` shows how to override the provider inline
  (the same `config={...}` works in every example).

## The custom-tool package

```bash
pip install -e examples/vaf_example_tool
venv/bin/python examples/01_hello_agent.py   # then ask: "roll a d20"
```

After the install, the `dice_roll` tool is discovered automatically at agent
startup (see the entry-point section of
[docs/EMBEDDING.md](../docs/EMBEDDING.md)); no VAF source file is touched.

## Notes

Example 05 writes its session id into `chat_session_id.txt` in the current
directory (that file belongs to the example app, not to VAF); the session
itself lives in VAF's standard store under `~/.vaf/sessions/`.

Example 08 is the opposite: it points `HOME` at a temporary directory **before**
importing VAF, so the keys it mints, the recovery note it writes and the machine
key it deletes in the last step all live inside that sandbox. Nothing it does
touches `~/.vaf`. For the real installation, `vaf secure status` reports the same
state - see [ENCRYPTION_AT_REST.md](../docs/security/ENCRYPTION_AT_REST.md).
