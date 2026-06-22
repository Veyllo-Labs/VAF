# Embedding VAF as a Library

VAF can be used as a headless agent **framework** — a foundation you build your
own application on, instead of writing the agent loop, tool dispatch, context
management and multi-provider LLM plumbing yourself. This page is the developer
contract for that use.

For the desktop/server product, see the main [README](../README.md).

---

## Install

The base install is intentionally slim — only what a headless agent needs:

```bash
pip install vaf
```

This pulls the core runtime and the LLM provider SDKs (OpenAI, Anthropic,
Google) — but **not** the web server, desktop UI, embeddings stack, or chat
bridges. Add those only if you need them, via extras:

| Extra | Adds | For |
|---|---|---|
| `vaf[server]` | fastapi, uvicorn, websockets | the HTTP/WebSocket API |
| `vaf[desktop]` | pywebview, pystray, PyQt6 | the desktop window / tray |
| `vaf[memory]` | sqlalchemy, pgvector, sentence-transformers, redis | long-term RAG memory |
| `vaf[speech]` | SpeechRecognition, pyaudio | offline speech-to-text |
| `vaf[browser]` | browser-use, playwright | browser automation tools |
| `vaf[pdf]` | pdfplumber, pytesseract, pdf2image | PDF extraction / OCR |
| `vaf[docs]` | python-docx, openpyxl, python-pptx | Office document tools |
| `vaf[discord]` / `vaf[telegram]` | chat bridges | messaging integrations |
| `vaf[all]` | everything above | parity with the full product |

```bash
pip install "vaf[memory,server]"      # mix and match
pip install "vaf[all]"                # the whole product
```

Tools whose extra is not installed are not loaded at startup (they are
unavailable until you install the extra); the agent still runs.

---

## Quickstart

```python
from vaf import Agent

agent = Agent(config={"provider": "deepseek"})
answer = agent.run("In one short sentence, what is Python?")
print(answer)
```

`Agent` here is the stable façade. The full internal engine remains available as
`vaf.CoreAgent` (a.k.a. `vaf.core.agent.Agent`) for advanced use.

The same code works with a local GGUF model — it is provider-agnostic:

```python
agent = Agent(config={"provider": "local"})   # downloads/starts a local model
print(agent.run("Hello!"))
```

### Streaming

```python
agent = Agent(config={"provider": "anthropic"})
agent.run("Explain async/await.", on_token=lambda s: print(s, end="", flush=True))
```

`on_token` receives text deltas as they arrive. For reasoning models the deltas
may include the model's `<think>...</think>` block; the value returned by `run()`
is always the cleaned final answer.

### Stateful conversations

One `Agent` instance keeps one conversation — repeated `run()` calls continue
the same history. Create a new `Agent` for an independent conversation.

---

## Configuration

`config=` is a dict merged on top of `~/.vaf/config.json` for this instance only
— nothing is written to disk, so each `Agent` can carry its own settings. Common
keys (full schema in [vaf/core/config.py](../vaf/core/config.py)):

| Key | Default | Meaning |
|---|---|---|
| `provider` | `local` | `local`, `openai`, `anthropic`, `google`, `deepseek`, `openrouter` |
| `model` | `auto` | local GGUF filename / repo, or an API model name |
| `api_key_<provider>` | — | API key, e.g. `api_key_deepseek` |
| `api_model_<provider>` | — | model per provider, e.g. `api_model_openai` |
| `n_ctx` | `32768` | context window (min 32768 for tool use) |
| `temperature` | `0.7` | sampling temperature |

```python
Agent(config={
    "provider": "openai",
    "api_key_openai": "sk-...",
    "api_model_openai": "gpt-4o",
})
```

---

## Headless safety: tool confirmation

VAF gates dangerous tools (shell, file writes, unsandboxed Python) behind a
confirmation prompt. An embedded library must never block waiting for a human,
so the façade sets `VAF_NONINTERACTIVE=1` by default: gated tools return an
error instead of hanging. To opt out, set `VAF_NONINTERACTIVE=0` before
constructing the agent.

To let specific dangerous tools run unattended, use the trust mechanisms instead
of disabling the gate:

- mark a working directory trusted (`mark_trusted_dir`),
- set a per-tool policy to allow (`set_tool_policy`),
- both persist in `~/.vaf/trust.json`.

---

## Writing a tool

A tool is a `BaseTool` subclass. Drop it in `vaf/tools/` (in-tree) or ship it as
a pip package (below). Full contract and examples in
[vaf/tools/base.py](../vaf/tools/base.py) and [vaf/tools/README.md](../vaf/tools/README.md).

```python
from vaf.tools.base import BaseTool

class WeatherTool(BaseTool):
    name = "get_weather"
    description = "Return the current weather for a city."
    permission_level = "read"          # read | write | dangerous | system
    side_effect_class = "none"         # none | reversible | irreversible
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }

    def run(self, **kwargs) -> str:
        city = kwargs["city"]
        return f"It is sunny in {city}."
```

Key declarative rules the runtime enforces:

- `permission_level` — `dangerous`/`system` trigger the confirmation gate.
- `side_effect_class` — surfaced to the model so it knows what is reversible.
- `admin_only`, `channel_restrictions`, `coder_only` — visibility/scoping.

---

## Shipping tools as a pip package (entry points)

Third-party packages can extend VAF without touching its source, via the
`vaf.tools` entry-point group. In your package's `setup.py`:

```python
setup(
    name="vaf-weather",
    # ...
    entry_points={
        "vaf.tools": [
            "get_weather = vaf_weather.tools:WeatherTool",
        ],
    },
)
```

or in `pyproject.toml`:

```toml
[project.entry-points."vaf.tools"]
get_weather = "vaf_weather.tools:WeatherTool"
```

Each entry point must resolve to a `BaseTool` subclass. After
`pip install vaf-weather`, the tool is discovered automatically at agent startup
(a broken package logs an error and is skipped — it never breaks startup).

---

## What is and isn't stable

Stable public surface (safe to build on):

- `from vaf import Agent` — the façade: `Agent(config=...)`, `.run(prompt, on_token=...)`, `.core`.
- `vaf.CoreAgent` — the engine, for advanced embedding.
- `BaseTool` — the tool contract.
- The `vaf.tools` entry-point group.

Everything else under `vaf.core.*` is internal and may change between releases.
