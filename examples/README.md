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
| [01_hello_agent.py](01_hello_agent.py) | The five-line quickstart: construct an `Agent`, run one prompt, multi-turn state |
| [02_streaming_and_events.py](02_streaming_and_events.py) | Live token streaming (`on_token`) plus the structured event sink (`tool_start`/`tool_end`/gate events) |
| [03_stream_json_subprocess.py](03_stream_json_subprocess.py) | Driving VAF as a subprocess via `vaf prompt --output-format stream-json` and parsing the NDJSON - the pattern for non-Python integrations |
| [04_inline_tool.py](04_inline_tool.py) | Per-instance tool registration with `agent.add_tool()` - no package, no file drop-in |
| [05_chatbot_with_memory.py](05_chatbot_with_memory.py) | A chatbot that survives restarts: `save_session()` + `Agent(session=...)` |
| [06_custom_persona.py](06_custom_persona.py) | Give the agent its own voice and instructions with `Agent(system_prompt=...)` |
| [vaf_example_tool/](vaf_example_tool/) | A complete installable pip package that adds a custom tool through the `vaf.tools` entry-point group |

## Prerequisites

- VAF installed (`pip install -e .` from the repo root, or a full product
  install).
- A working model backend: either an API provider configured in
  `~/.vaf/config.json` (fastest for a first test) or local mode (the first
  run downloads a multi-GB model). The examples default to whatever your
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
