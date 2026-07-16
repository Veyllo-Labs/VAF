# FAQ - Developers Building on VAF

Short answers with pointers, for developers embedding or extending VAF.
Product/install troubleshooting lives in
[INSTALLATION_GUIDE.md](setup/INSTALLATION_GUIDE.md) and the platform setup
docs; debugging runtime issues in [DEBUGGING.md](DEBUGGING.md).

## Install and requirements

**Is VAF on PyPI?**
Not yet. Install from source: `git clone` + `pip install -e .` (slim base) or
with extras like `.[memory,server]` - see [EMBEDDING.md](EMBEDDING.md).

**Which Python versions are supported?**
3.10 or newer (`python_requires` in setup.py).

**Do I need Docker?**
The agent itself runs without it. Docker is hard-required for code-execution
tools (`python_sandbox`, the test runner - there is deliberately no host
fallback) and for the memory/RAG stack (PostgreSQL, Redis). Without Docker
those tools return error strings and the run continues. See the security
posture section in [EMBEDDING.md](EMBEDDING.md).

## Embedding

**Why does a tool come back with `[ERROR] Tool '...' requires confirmation`?**
The embedded default is `VAF_NONINTERACTIVE=1`: confirmation-gated tools
return an error string instead of blocking on a human. Grant specific tools
via the trust mechanisms (`mark_trusted_dir`, `set_tool_policy`) - see
"Headless safety" in [EMBEDDING.md](EMBEDDING.md).

**Why is `run()` slow or downloading gigabytes on first use?**
Local mode readies the model on the first `run()`: a multi-GB download plus
model load. Use an API provider for a quick first test.

**Can one `Agent` be shared across threads?**
No. One instance is one conversation and is effectively single-threaded;
create one instance per parallel conversation. There is no async API today.
See the concurrency contract in [CORE_AGENT.md](CORE_AGENT.md).

**Why did `chat_step()` return `"..."` instead of the answer?**
That is its contract: the return value is a status, the real answer arrives
via the stream callback and `history[-1]`. Use the `vaf.Agent` facade (its
`run()` handles this) or read the return contract in
[CORE_AGENT.md](CORE_AGENT.md).

**VAF writes log files into my venv/site-packages. How do I stop that?**
Set `VAF_LOG_DIR` before starting (a couple of writers ignore it - the
sub-agent debug tree and the desktop leak diagnostics). The full logging map,
including what `debug_logs_enabled` does and does not silence, is in
[DEBUGGING.md](DEBUGGING.md).

**Can I run the main agent on Ollama/vLLM/LM Studio?**
Not today. The `local_api_url` key redirects only the API-backend consumers
(browser agent, local vision, cloud-to-local failover); the main chat loop in
local mode always manages its own llama server. See
[EMBEDDING.md](EMBEDDING.md) and [PROVIDER_MODES.md](llm/PROVIDER_MODES.md).

**How do I integrate VAF from a non-Python application?**
Spawn `vaf prompt --output-format stream-json` and parse the NDJSON event
stream. Contract and a runnable parser:
[OBSERVABILITY.md](OBSERVABILITY.md) and
[examples/03_stream_json_subprocess.py](../examples/03_stream_json_subprocess.py).

## Extending

**How do I add a tool without forking VAF?**
Four lanes: per Agent instance via `agent.add_tool(MyTool())` (simplest for
embedding; runnable in
[examples/04_inline_tool.py](../examples/04_inline_tool.py)), a pip package
exposing a `BaseTool` subclass via the `vaf.tools` entry-point group
(recommended for distribution; complete example in
[examples/vaf_example_tool/](../examples/vaf_example_tool/)), the
update-surviving `custom_tools/` folder in the platform data dir, or an
in-tree `vaf/tools/*.py` file for contributions. Registering MCP servers adds
external tools too. See [EMBEDDING.md](EMBEDDING.md) and
[vaf/tools/README.md](../vaf/tools/README.md).

**Can I add my own LLM provider?**
Not without forking today: the provider factory is a fixed set. The supported
escape hatch is `local_api_url` for the API-backend consumer lanes. A
provider registry is a known, deliberately deferred seam (see the closing
notes in [ARCHITECTURE.md](ARCHITECTURE.md)); if it lands, it will be
announced in [CHANGELOG.md](../CHANGELOG.md).

**Can I swap the memory backend?**
No - long-term memory is PostgreSQL + pgvector. The memory stack is optional
though: without it, memory tools fail soft.

## Stability and licensing

**The version says 0.1.0aN - can I build on this?**
The declared public surface (`from vaf import Agent`, `CoreAgent` at the
documented level, `BaseTool`, the entry-point group) follows the
backward-compatibility rules in [RELEASING.md](setup/RELEASING.md) and is
pinned by a CI test; everything else under `vaf.core.*` may change between
releases. During the alpha, breaking changes to the surface are still
possible and are announced in the changelog. See
[ARCHITECTURE.md](ARCHITECTURE.md).

**Can I use VAF inside a closed-source product?**
VAF is AGPL-3.0-or-later with an additional permission for plugins, tools and
workflows, and commercial licensing exists - read
[LICENSING.md](../LICENSING.md) and [COMMERCIAL.md](../COMMERCIAL.md); this
FAQ is not the authoritative text.
