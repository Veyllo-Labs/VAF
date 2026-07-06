# Analysis: queued input while a chat step is blocked, and idle unload during an in-flight request

## Summary

When the Web UI shows "Queued input for session …" and Lazy Load does not (re-)load
the model into VRAM in time, RAM usage can climb. The root cause is a blocked headless
worker combined with an idle unload during an in-flight or stuck chat request.

---

## What the logs show

Reconstructed from `queue.log`, `tray_debug.log`, and `backend.log`, the failure
sequence is:

1. A first user message is processed: `QUEUE_GET` → `QUEUE_CHAT_START` → RAG →
   `CHAT_STEP_CALL`.
2. For several minutes no `QUEUE_CHAT_END` appears for this session, so the headless
   worker is still inside `chat_step()` for the first message (either a very long
   stream or a hang).
3. A second user message is enqueued (`QUEUE_ADD` for the session). The Web UI shows
   "Queued input for session …". There is no `QUEUE_GET` for this second message — the
   worker never picks it up.
4. The idle timeout is reached and the Tray unloads the model ("Idle timeout reached.
   Unloading model (loaded=True)."). llama-server is stopped (`stop_server`), followed
   by "Model unloaded."
5. After that, repeated "WebSocket handshake failed" entries appear as the UI tries to
   reconnect.

### Key points

- **Single headless worker:** exactly one consumer calls `tq.get()` and then runs a
  full chat step (RAG, context, `chat_step`, compaction) end to end.
- **First step never finishes:** between `CHAT_STEP_CALL` and the second `QUEUE_ADD`
  there is no `QUEUE_CHAT_END`, so the first `chat_step()` was active (or hung) for
  several minutes.
- **Second message stays in the queue:** because the worker is still inside the first
  step, it never issues a second `tq.get()`, so the second message stays "Queued" and
  is never processed.
- **Lazy Load from the UI's perspective:** at the time of the second prompt the model
  was still loaded (`loaded=True` until the idle unload). Lazy Load did not fail to
  trigger; the worker is blocked and never picks up the second message. After the idle
  unload, Lazy Load only takes effect on the next `tq.get()` in the headless runner,
  which happens once the first step finally completes or aborts.

---

## Why RAM climbs

While the worker is stuck in `chat_step()` (or retrying), the VAF backend Python
process keeps holding, among other things:

1. **The full chat history** (`agent.history`) for the session.
2. **The current turn:** the assembled prompt, system/Soul, RAG context, and possibly
   tool calls.
3. **`response_parts`:** every streamed chunk of the in-flight response, which grows if
   the step streams for a long time or retries and keeps appending.
4. **RAG state:** memory search, snippets, and possibly the embedding model
   (Xenova/all-MiniLM-L6-v2) staying loaded.
5. **Server retry loop:** on a `ConnectionError` (e.g. when the Tray stops the server at
   idle), the agent calls `start_server`, sleeps, and retries. The existing context
   (history, prompt, `response_parts`) stays in the process; the step is not cleanly
   restarted.

Additionally:

- llama-server's prompt cache can reach a few hundred MiB. When the process is stopped,
  the OS reclaims the memory; if the headless runner then calls `start_server`, a new
  llama process again takes up RAM/VRAM.
- If the first step streams or hangs for a long time, `response_parts` and the
  history/context grow over minutes, increasing RAM in the Python process.

In short, RAM climbs because a single chat step runs for a long time or hangs, the
worker pulls no further tasks from the queue, yet keeps holding context and stream data
in the process, with server restarts or retries adding to it.

---

## Why Lazy Load does not help here

- At the time of the second prompt the model is still loaded, so "Activity" does not
  trigger another load.
- Lazy Load in the headless runner (`headless_runner.py`) only runs once the worker
  pulls a new chat task via `tq.get()` and then calls `agent.load_model(...)`.
- Since the worker never reaches the next `tq.get()` (it is blocked in the first
  `chat_step()`), Lazy Load is never executed for the second message.
- After the idle unload: if the first step eventually exits with a `ConnectionError` and
  the worker finishes the step, it picks up the second message on the next `tq.get()`
  and then attempts Lazy Load. Until then the UI stays at "Queued" and RAM stays high,
  because the first step is still holding resources.

---

## Mitigations (no code change)

1. **Raise the idle timeout** (e.g. `server_idle_timeout` in the config) so the model is
   not unloaded in the middle of a long response.
2. **Consider persistent mode** if chatting often resumes after a longer pause, so the
   model is not unloaded after going idle.
3. **After such an incident, restart VAF/Tray** so the blocked worker and the bloated
   process memory are cleared.

---

## Code fixes (for maintainers)

1. **Don't unload while a request is in flight**  
   The Tray should know whether a chat request is currently in the queue or running in
   the headless runner, and with `loaded=True` plus a request in flight it should not
   perform the idle unload (or only after the request finishes plus an additional idle
   timeout).

2. **Timeout for `chat_step()`**  
   So a hung step does not block the queue indefinitely: e.g. a timeout on the HTTP
   request/stream to the backend (8080) or on the overall `chat_step()` duration, then
   abort the step, log the error, write `QUEUE_CHAT_END`/`QUEUE_CHAT_FAIL`, and move on
   to the next `tq.get()`.  
   **Status:** Implemented for the local server (8080): connect 60 s, read 5 min per
   chunk; on read timeout the step ends and the queue continues. See **Local Server:
   Request Timeouts** in [docs/llm/API_INTEGRATION.md](API_INTEGRATION.md).

3. **Load on incoming chat message**  
   When a user message is enqueued and the model is not currently loaded, immediately
   trigger "Activity / Loading model" (not just on the heartbeat), so the server starts
   up before the worker picks up the task.

4. **Retry throttle**  
   On a `ConnectionError`, do not `start_server` and retry indefinitely; use a maximum
   number of retries or a time-based abort, then `QUEUE_CHAT_FAIL` and the next task.
