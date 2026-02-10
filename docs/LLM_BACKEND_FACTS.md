# LLM Backend – Fakten aus dem Code

## Welcher Backend wird genutzt?

In `chat_step()` (agent.py) gibt es genau **drei** Pfade:

1. **`if self.api_backend`** → API (OpenAI, Anthropic, DeepSeek, Google, OpenRouter). Kein lokales Modell.
2. **`elif self.use_server`** → HTTP an **127.0.0.1:8080** (nativer llama-server). Modell läuft im **Server-Prozess**.
3. **`else`** → **Library** (llama-cpp-python, `self.llm`). Modell läuft **im VAF-Python-Prozess**.

Es wird **immer genau einer** dieser drei Pfade genutzt. Welcher, steht in **`logs/backend.log`** (z. B. `chat_step backend=library(llama-cpp-python)` oder `backend=server(8080)`).

---

## Wann wird Server (8080) vs. Library genutzt?

In `load_model()` (agent.py, ca. Zeile 1364–1410):

- **Server-Pfad** wird nur betreten, wenn **eine** der Bedingungen gilt:
  - Python **3.13**, **oder**
  - **macOS**, **oder**
  - **`force_server=True`** in der Config.
- Sonst (z. B. **Windows + Python 3.12** und **kein** `force_server`): Es wird **direkt** die **Library** geladen (`Llama(...)`), **ohne** 8080 zu prüfen.

**Folge:** Unter Windows ohne `force_server` und ohne Py3.13 nutzt der Agent **immer** die Library (llama-cpp-python). Der Tray startet zwar den nativen llama-server (8080), der Agent verwendet ihn in diesem Fall **nicht** – er lädt das Modell ein zweites Mal im Python-Prozess. Das erklärt den hohen RAM im VAF-Prozess (~12 GB+).

**Wenn du VQ1 nur im Server (8080) laufen lassen willst:** In der Config **`force_server: true`** setzen (oder unter Windows auf Python 3.13 wechseln). Dann prüft der Agent 8080, nutzt den Tray-Server und lädt **kein** Modell in den Python-Prozess.

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

When using the Local Server (llama-server), the context window size (`n_ctx`) is critical for handling long conversations and RAG contexts.

- **Configuration:** Set `n_ctx` in `config.json` (or via Settings → Advanced).
- **Startup:** When the server is started via the System Tray or Headless Runner, this value is passed directly to the `llama-server` process via the `-c` argument.
- **Verification:** You can verify the active context limit by checking `logs/server.log` for the `llama_new_context_with_model` or `limits` entry.
- **GPU Offloading:** The `gpu_layers` setting is also respected, ensuring optimal VRAM usage alongside the context buffer.

**Note:** Increasing `n_ctx` significantly increases VRAM usage. Ensure your hardware can support the target size (e.g., 16k context may require ~10GB+ VRAM depending on the model quantization).