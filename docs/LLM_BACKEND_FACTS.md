# LLM Backend – Fakten aus dem Code

## Welcher Backend wird genutzt?

In `chat_step()` (agent.py) gibt es genau **drei** Pfade:

1. **`if self.api_backend`** → API (OpenAI, Anthropic, DeepSeek, Google, OpenRouter). Kein lokales Modell.
2. **`elif self.use_server`** → HTTP an **127.0.0.1:8080** (nativer llama-server). Modell läuft im **Server-Prozess**.
3. **`else`** → **Library** (llama-cpp-python, `self.llm`). Modell läuft **im VAF-Python-Prozess**.

Es wird **immer genau einer** dieser drei Pfade genutzt. Welcher, steht in **`logs/backend.log`** (z. B. `chat_step backend=library(llama-cpp-python)` oder `backend=server(8080)`).

---

## Wann wird Server (8080) vs. Library genutzt?

In `load_model()` (siehe `vaf/core/agent.py`) ist das Verhalten heute:

- **Windows-Default:** Wenn `force_server` nicht explizit gesetzt ist, wird auf Windows standardmäßig **`True`** verwendet. Ziel: kein doppeltes Modell im Python-Prozess.
- **Server-Pfad:** Der Agent nutzt den HTTP-Server-Pfad (`127.0.0.1:8080`), wenn `self.use_server` aktiv ist.
- **Library-Pfad:** Nur wenn `self.use_server` nicht aktiv ist, läuft das Modell in-process via `llama-cpp-python`.

**Praxisfolge:** Die frühere Faustregel "Windows + kein `force_server` = immer Library" ist veraltet. In aktuellen Builds ist das Windows-Standardverhalten bereits server-freundlich.

**Wenn du VQ1 ausschließlich über Server (8080) erzwingen willst:** `force_server: true` bleibt der explizite Schalter (zusätzlich zur Windows-Default-Logik).

---

## Warum kam kein Thinking (<think>) an?

- **Server-Pfad (8080):** Liest `delta.get('reasoning_content')` **und** `delta.get('content')` und streamt beides (Thinking + Antwort). Passt für VQ1 auf dem Server.
- **Library-Pfad (llama-cpp-python):** Hatte bisher **nur** `delta.get('content')`. Ein separates Feld **`reasoning_content`** wurde **nicht** ausgewertet und **nicht** gestreamt.

**Änderung:** Im Library-Pfad wird `reasoning_content` jetzt genauso wie im Server-Pfad gelesen und als Thinking gestreamt (inkl. `<think>` / `</think>`). Wenn VQ1 über die Library `reasoning_content` liefert, erscheint es nun in der UI.

---

## Tool-Calls in <think>

Wenn das Modell einen Tool-Call **innerhalb** von `<think>...</think>` ausgibt (z. B. `<tool_call>{"name": "update_intent", ...}</tool_call>` im Think-Block), wird er trotzdem erkannt:

- **XML-Fallback** (agent.py): Es wird sowohl `full_response` als auch `full_reasoning` durchsucht (`text_to_search = full_response + "\n" + full_reasoning`). So werden Tool-Calls in `<think>` auch dann gefunden, wenn Thinking separat gestreamt wurde.
- **System-Prompt:** Der Agent wird angewiesen, Tool-Calls **in der Hauptantwort (nach `</think>`)**, nicht innerhalb von `<think>`, zu platzieren, damit sie zuverlässig ausgeführt werden.

---

## Logs for debugging

- **`logs/backend.log`**: One line per chat step with the backend in use, e.g. `chat_step backend=library(llama-cpp-python)`, `chat_step backend=server(8080)`, or `chat_step backend=api(openai)`.
- **`logs/memory.log`**: `[PROFILER]` entries (RAM every 30 s), plus compaction, usage, embedding load, `[WHISPER]` load.
- **`logs/startup_trace.txt`**: Tray and WebServer startup. "Model loaded" means the tray started the server (8080); the agent may still use the library if `load_model()` did not take the server path.

Together, **backend.log** and **startup_trace.txt** show whether the tray started the server and whether the agent is using the API, server (8080), or library.

---

## Context Window Configuration

When using the Local Server (llama-server), the context window size (`n_ctx`) is critical for tool calling to work correctly.

- **Minimum: 32768** — hardcoded in VAF regardless of config. With 100+ tools, the overhead alone is ~11K tokens (system prompt ~5.5K + tool schemas ~6K), leaving ~20K for conversation. Values below 32K cause the router safety net to trigger on every turn.
- **Configuration:** Set `n_ctx` in `config.json` (or via Settings → Advanced). Values below 32768 are silently raised to 32768.
- **KV Cache:** VAF uses `q8_0` for keys and `q4_0` for values — ~62.5% less VRAM than f16 with negligible quality loss.
- **VRAM estimate (RTX 3080, 10GB):** VQ-1 q4_k_m (~4GB) + 32K KV cache (~0.8GB) = ~4.8GB total, leaving ~5GB free.

## Native Chat Template and Tool Calling

VQ-1's embedded Jinja template uses `<tool_call>` XML format for function calls:
```
<tool_call>
{"name": "web_search", "arguments": {"query": "..."}}
</tool_call>
```

llama-server (with `--jinja`) parses these tags and converts them to standard OpenAI `tool_calls` objects in the API response. VAF's streaming parser then picks them up and executes the tools.

**Do not override with `--chat-template chatml`** — the generic chatml template does not include `<tool_call>` formatting, which causes the model to describe tool usage in text instead of emitting actual tool calls (hallucination).