# Analyse: "Queued input" + Lazy Load funktioniert nicht → RAM voll

## Kurzfassung

Wenn die WebUI **"Queued input for session …"** anzeigt und der **Lazy Load** das Modell nicht (rechtzeitig) wieder in den VRAM lädt, kann der RAM stark steigen. Ursache ist ein **blockierter Headless-Worker** plus **Idle-Unload** während einer laufenden oder hängenden Chat-Anfrage.

---

## Was die Logs zeigen

### Timeline (aus queue.log, tray_debug.log, backend.log)

1. **15:00:19** – Erste Nachricht **"wie ist dien name ?"** wird verarbeitet:
   - `QUEUE_GET` → `QUEUE_CHAT_START` → RAG → `CHAT_STEP_CALL` (15:00:27).

2. **Bis 15:08:22** – Es erscheint **kein** `QUEUE_CHAT_END` für diese Session.
   - Der Headless-Worker ist also die ganze Zeit **noch in** `chat_step()` für die erste Nachricht (entweder sehr langer Stream oder hängt).

3. **15:08:22** – Du schickst **"das ist cool und wer bin ich ?"**:
   - `QUEUE_ADD` für Session `cyan538960`.
   - WebUI zeigt: **"Queued input for session cyan538960: das ist cool und wer bin ich ? …"**
   - Es gibt **kein** `QUEUE_GET` für diese zweite Nachricht – der Worker holt sie nie ab.

4. **15:08:48** – Tray: **"Idle timeout reached. Unloading model (loaded=True)."**
   - llama-server wird beendet (`stop_server`).
   - Kurz danach: **"Model unloaded."**

5. **Danach** – Viele **"WebSocket handshake failed"** (UI versucht, sich neu zu verbinden).

### Wichtige Erkenntnis

- **Nur ein Headless-Worker:** Es gibt genau einen Consumer, der `tq.get()` macht und dann einen kompletten Chat-Step (RAG, Kontext, `chat_step`, Compaction) durchzieht.
- **Erster Step nie beendet:** Zwischen `CHAT_STEP_CALL` (15:00:27) und `QUEUE_ADD` (15:08:22) steht **kein** `QUEUE_CHAT_END`. Der erste `chat_step()` war also ~8 Minuten lang aktiv (oder hing).
- **Zweite Nachricht bleibt in der Queue:** Weil der Worker noch im ersten Step steckt, macht er kein zweites `tq.get()` → die zweite Nachricht bleibt "Queued" und wird nie verarbeitet.
- **Lazy Load "funktioniert nicht" aus Sicht der UI:** Zum Zeitpunkt des zweiten Prompts (15:08:22) war das Modell **noch** geladen (`loaded=True` bis 15:08:48). Das Problem ist nicht, dass der Lazy Load nicht getriggert wurde, sondern dass der **Worker blockiert** ist und die zweite Nachricht nie abholt. Später (nach Idle-Unload) wäre Lazy Load erst beim **nächsten** `tq.get()` im Headless dran – der aber erst kommt, wenn der erste Step endlich fertig ist oder abbricht.

---

## Warum der RAM stark steigt

Während der Worker in `chat_step()` steckt (oder endlos retried), bleibt im **Python-Prozess** (VAF Backend) u.a.:

1. **Voller Chat-Verlauf** (`agent.history`) für die Session.
2. **Aktueller Turn:** gebauter Prompt, System/Soul, RAG-Kontext, ggf. Tool-Calls.
3. **`response_parts`:** alle gestreamten Chunks der laufenden Antwort (wenn der erste Step lange streamt oder mehrfach retried und weiter anhängt).
4. **RAG:** Memory-Suche, Snippets, ggf. Embedding-Modell (Xenova/all-MiniLM-L6-v2) bleibt geladen (memory.log).
5. **Server-Retry-Loop:** Bei `ConnectionError` (z.B. weil Tray den Server um 15:08:48 killt) macht der Agent `start_server`, `sleep(2)`, und **retry**. Dabei bleibt der bisherige Kontext (History, Prompt, `response_parts`) im Prozess – es kommt kein sauberer Neustart des Steps.

Zusätzlich:

- **llama-server (server.log):** Prompt-Cache bis ~283 MiB. Wenn der Prozess beendet wird, gibt der OS den Speicher frei; wenn der Headless danach `start_server` aufruft, kann ein **neuer** llama-Prozess wieder viel RAM/VRAM belegen.
- Wenn der erste Step **sehr lange** streamt oder hängt, wachsen `response_parts` und ggf. History/Kontext über Minuten → **RAM-Anstieg im Python-Prozess**.

Kurz: Der RAM steigt, weil **ein einziger Chat-Step sehr lange läuft oder hängt**, der Worker **keine weiteren Tasks** aus der Queue holt, und trotzdem **weiter Kontext und Stream-Daten** im Prozess gehalten werden – und ggf. zusätzlich Server-Neustarts oder -Retries dazu kommen.

---

## Warum "Lazy Load" hier nicht greift

- **Zum Zeitpunkt des zweiten Prompts (15:08:22):** Modell war noch geladen; "Activity" würde hier kein erneutes Load auslösen.
- **Lazy Load im Headless** (headless_runner.py) läuft erst, wenn der Worker einen **neuen** Chat-Task mit `tq.get()` holt und dann `agent.load_model(...)` aufruft.
- Da der Worker aber **nicht** zum nächsten `tq.get()` kommt (weil er im ersten `chat_step()` blockiert), wird für die zweite Nachricht **nie** Lazy Load ausgeführt.
- **Nach dem Idle-Unload (15:08:48):** Wenn der erste Step irgendwann mit ConnectionError rauskommt und der Worker den Step beendet, würde er beim **nächsten** `tq.get()` die zweite Nachricht holen und **dann** Lazy Load versuchen. Bis dahin bleibt die UI bei "Queued" und der RAM hoch, weil der erste Step noch Ressourcen hält.

---

## Mögliche Verbesserungen (ohne Code zu ändern)

1. **Idle-Timeout erhöhen** (z.B. `server_idle_timeout` in der Config), damit das Modell nicht mitten in einer langen Antwort entladen wird.
2. **Persistent-Modus** erwägen, wenn du oft nach längerer Pause weiterchattest – dann wird das Modell nicht nach Idle entladen.
3. Nach so einem Vorfall: **VAF/Tray neu starten**, damit der blockierte Worker und der große Prozess-Speicher weg sind.

---

## Mögliche Code-Fixes (für Maintainer)

1. **Nicht entladen, wenn Request läuft**  
   Tray sollte wissen, ob gerade ein Chat-Request in der Queue oder im Headless läuft, und bei `loaded=True` + "request in flight" den Idle-Unload **nicht** ausführen (oder erst nach Ende des Requests + zusätzlichem Idle-Timeout).

2. **Timeout für `chat_step()`**  
   Damit ein hängender Step die Queue nicht dauerhaft blockiert: z.B. Timeout für den HTTP-Request/Stream zum Backend (8080) oder für die gesamte `chat_step()`-Dauer, danach Step abbrechen, Fehler loggen, `QUEUE_CHAT_END`/`QUEUE_CHAT_FAIL` schreiben und zum nächsten `tq.get()` gehen.  
   **Status:** Implemented for the local server (8080): connect 60 s, read 5 min per chunk; on read timeout the step ends and the queue continues. See **Local Server: Request Timeouts** in `docs/API_INTEGRATION.md`.

3. **Laden bei eingehender Chat-Nachricht**  
   Wenn eine Nutzer-Nachricht in die Queue gestellt wird und das Modell aktuell nicht geladen ist, sofort "Activity / Loading model" auslösen (nicht nur auf Heartbeat), damit der Server schon startet, bevor der Worker den Task holt.

4. **Retry-Bremse**  
   Bei ConnectionError nicht unbegrenzt `start_server` + Retry; z.B. maximale Anzahl Retries oder Abbruch nach Zeit, dann `QUEUE_CHAT_FAIL` und nächster Task.

Wenn du willst, können wir einen davon (z.B. "Nicht entladen bei Request in flight" oder "Timeout für chat_step") konkret im Code ausarbeiten.
