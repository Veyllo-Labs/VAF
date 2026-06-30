# Analysis: "Queued input" + Lazy Load not working → RAM fills up

## Summary

When the Web UI shows **"Queued input for session …"** and **Lazy Load** fails to (re-)load the model into VRAM in time, RAM usage can climb sharply. The cause is a **blocked headless worker** combined with an **idle unload** during an in-flight or stuck chat request.

---

## What the logs show

### Timeline (from queue.log, tray_debug.log, backend.log)

1. **15:00:19** – The first message **"wie ist dien name ?"** is processed:
   - `QUEUE_GET` → `QUEUE_CHAT_START` → RAG → `CHAT_STEP_CALL` (15:00:27).

2. **Up to 15:08:22** – **No** `QUEUE_CHAT_END` appears for this session.
   - So the headless worker is still **inside** `chat_step()` for the first message the entire time (either a very long stream or it's hung).

3. **15:08:22** – You send **"das ist cool und wer bin ich ?"**:
   - `QUEUE_ADD` for session `cyan538960`.
   - The Web UI shows: **"Queued input for session cyan538960: das ist cool und wer bin ich ? …"**
   - There is **no** `QUEUE_GET` for this second message – the worker never picks it up.

4. **15:08:48** – Tray: **"Idle timeout reached. Unloading model (loaded=True)."**
   - llama-server is stopped (`stop_server`).
   - Shortly after: **"Model unloaded."**

5. **After that** – Many **"WebSocket handshake failed"** entries (the UI keeps trying to reconnect).

### Key takeaway

- **Only one headless worker:** There is exactly one consumer that calls `tq.get()` and then runs a full chat step (RAG, context, `chat_step`, compaction) end to end.
- **First step never finishes:** Between `CHAT_STEP_CALL` (15:00:27) and `QUEUE_ADD` (15:08:22) there is **no** `QUEUE_CHAT_END`. So the first `chat_step()` was active (or hung) for about 8 minutes.
- **Second message stays in the queue:** Because the worker is still inside the first step, it never issues a second `tq.get()` → the second message stays "Queued" and is never processed.
- **Lazy Load "not working" from the UI's perspective:** At the time of the second prompt (15:08:22) the model was **still** loaded (`loaded=True` until 15:08:48). The problem isn't that Lazy Load failed to trigger, but that the **worker is blocked** and never picks up the second message. Later (after the idle unload), Lazy Load would only kick in on the **next** `tq.get()` in the headless runner – which only happens once the first step finally completes or aborts.

---

## Why RAM climbs sharply

While the worker is stuck in `chat_step()` (or retrying endlessly), the **Python process** (VAF backend) keeps holding, among other things:

1. **The full chat history** (`agent.history`) for the session.
2. **The current turn:** the assembled prompt, system/Soul, RAG context, and possibly tool calls.
3. **`response_parts`:** every streamed chunk of the in-flight response (if the first step streams for a long time, or retries repeatedly and keeps appending).
4. **RAG:** memory search, snippets, and possibly the embedding model (Xenova/all-MiniLM-L6-v2) staying loaded (memory.log).
5. **Server retry loop:** On a `ConnectionError` (e.g. because the Tray kills the server at 15:08:48), the agent calls `start_server`, `sleep(2)`, and **retries**. The existing context (history, prompt, `response_parts`) stays in the process – there is no clean restart of the step.

Additionally:

- **llama-server (server.log):** Prompt cache up to ~283 MiB. When the process is killed, the OS reclaims the memory; if the headless runner then calls `start_server`, a **new** llama process can again take up a lot of RAM/VRAM.
- If the first step streams or hangs **for a very long time**, `response_parts` and possibly the history/context grow over the course of minutes → **RAM growth in the Python process**.

In short: RAM climbs because **a single chat step runs for a very long time or hangs**, the worker **pulls no further tasks** from the queue, yet **keeps holding context and stream data** in the process – with server restarts or retries potentially adding to it.

---

## Why "Lazy Load" doesn't help here

- **At the time of the second prompt (15:08:22):** the model was still loaded; "Activity" wouldn't trigger another load here.
- **Lazy Load in the headless runner** (headless_runner.py) only runs once the worker pulls a **new** chat task via `tq.get()` and then calls `agent.load_model(...)`.
- But since the worker **never** reaches the next `tq.get()` (because it's blocked in the first `chat_step()`), Lazy Load is **never** executed for the second message.
- **After the idle unload (15:08:48):** if the first step eventually exits with a ConnectionError and the worker finishes the step, it would pick up the second message on the **next** `tq.get()` and **then** attempt Lazy Load. Until then the UI stays at "Queued" and RAM stays high, because the first step is still holding resources.

---

## Possible improvements (without changing code)

1. **Raise the idle timeout** (e.g. `server_idle_timeout` in the config) so the model isn't unloaded in the middle of a long response.
2. **Consider persistent mode** if you often resume chatting after a longer pause – the model then isn't unloaded after going idle.
3. After an incident like this: **restart VAF/Tray** so the blocked worker and the bloated process memory are gone.

---

## Possible code fixes (for maintainers)

1. **Don't unload while a request is in flight**  
   The Tray should know whether a chat request is currently in the queue or running in the headless runner, and with `loaded=True` + "request in flight" it should **not** perform the idle unload (or only after the request finishes plus an additional idle timeout).

2. **Timeout for `chat_step()`**  
   So a hung step doesn't block the queue indefinitely: e.g. a timeout on the HTTP request/stream to the backend (8080) or on the overall `chat_step()` duration, then abort the step, log the error, write `QUEUE_CHAT_END`/`QUEUE_CHAT_FAIL`, and move on to the next `tq.get()`.  
   **Status:** Implemented for the local server (8080): connect 60 s, read 5 min per chunk; on read timeout the step ends and the queue continues. See **Local Server: Request Timeouts** in `docs/llm/API_INTEGRATION.md`.

3. **Load on incoming chat message**  
   When a user message is enqueued and the model isn't currently loaded, immediately trigger "Activity / Loading model" (not just on the heartbeat), so the server starts up before the worker picks up the task.

4. **Retry throttle**  
   On a ConnectionError, don't `start_server` + retry indefinitely; e.g. a maximum number of retries or a time-based abort, then `QUEUE_CHAT_FAIL` and the next task.

If you'd like, we can flesh out one of these (e.g. "Don't unload while a request is in flight" or "Timeout for chat_step") concretely in the code.
