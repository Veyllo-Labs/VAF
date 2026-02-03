# Kontext-Kompression – Ablauf Schritt für Schritt

Dieses Dokument beschreibt **genau**, was bei der Kontext-Kompression in VAF passiert: wo sie ausgelöst wird, was der `ContextManager` macht und wie das mit dem dynamischen System-Prompt zusammenhängt.

---

## 1. Wo wird Kompression ausgelöst?

**Datei:** `vaf/core/agent.py`

Die Kompression wird **einmal pro Nutzer-Turn** in `chat_step()` geprüft, **bevor** die neue User-Nachricht an die History angehängt wird.

Reihenfolge im `chat_step()`:

1. `context_manager.decay_state()` – TTL von State-Einträgen (Dateien, etc.) wird dekrementiert.
2. **Dynamic Context:** Bei `user_input` und `prompt_manager`:
   - Sprache erkennen, `analyze_context(user_input)` aufrufen, `new_prompt = build_prompt(...)` bauen.
   - `new_prompt` wird **nur in diesem Block** gesetzt, **nicht** in die History geschrieben.
3. **Context Compression:**
   - **Bedingung:** `context_manager.should_compress(self.history)` muss `True` sein.
   - Wenn **ja:** `compress()` aufrufen, dann Context Glue an `new_prompt` hängen, System-Prompt in `history[0]` mit diesem `new_prompt` überschreiben (inkl. Glue + ggf. PROJECT CONTEXT).
   - Wenn **nein:** Es passiert nichts mit der History; ein zuvor gebauter `new_prompt` wird **nicht** in `history[0]` geschrieben (bekannter Bug).

---

## 2. Wann gilt „soll komprimiert werden“?

**Datei:** `vaf/core/context.py`

```python
def should_compress(self, history: List[Dict]) -> bool:
    usage = self.get_usage_percent(history)
    return usage >= self.trigger_threshold
```

- **`trigger_threshold`:** Standard **0.85** (85 % des Kontext-Limits).
- **`get_usage_percent(history)`:**  
  `estimate_tokens(history) / max_tokens`  
  – also geschätzte Token der aktuellen History geteilt durch das konfigurierte Kontext-Limit (z. B. 8192 oder 128000).

**Kurz:** Kompression wird ausgelöst, sobald die geschätzte Nutzung der History **≥ 85 %** des `max_tokens` ist.

---

## 3. Token-Schätzung (`estimate_tokens`)

**Datei:** `vaf/core/context.py`

- Pro Nachricht:
  - `content`: Wenn `"```"` im Inhalt → `len(content) / 2.5` (Code), sonst `len(content) / 3.0` (Text).
  - `role`: `len(role) / 3.0`.
- Danach: **+10 %** Sicherheitsmargin auf die Gesamtsumme (Spezial-Tokens, Formatierung).

Es wird **kein** echtes Tokenisieren (z. B. tiktoken) verwendet, nur Zeichen-basierte Schätzung.

---

## 4. Ablauf von `compress(history)` – Schritt für Schritt

**Datei:** `vaf/core/context.py`, Methode `compress()`.

### 4.1 Voraussetzung

- Wenn `len(history) <= recent_memory_size + 2` (Standard: 12): **keine** Kompression, `history` wird unverändert zurückgegeben.

### 4.2 Schritt 1: Archivieren

- `_archive_history(history)` wird aufgerufen.
- **In-Memory:** Ein `ContextSnapshot` (Timestamp, History-Kopie, Intent, State, Token-Count) wird an `self.archive` angehängt; maximal 3 Snapshots, ältester wird entfernt.
- **Auf Disk:** Eine JSON-Datei unter `~/.vaf/context_archive/` wird geschrieben (`context_YYYYMMDD_HHMMSS_<hash>.json`) mit History, Intent, State und Token-Count. Optional, Fehler werden still ignoriert.

### 4.3 Schritt 2: Intent und State aus **aller** History aktualisieren

- Über **alle** Nachrichten in `history` wird iteriert:
  - Bei `role == "user"`: `update_intent(msg["content"])` – Extraktion von Zielen, Keywords, Constraints (Regex-Muster).
  - Für **jede** Nachricht: `update_state(msg)` – Extraktion von:
    - Dateien (created/read/modified),
    - Fehlern (error/failed/fehler),
    - Tools (aus `role=="tool"`),
    - „Key decisions“ aus Assistant-Text (ohne `<think>`),
    - Code-Snippets aus Code-Blöcken.

Damit sind Intent und State **vor** dem Verwerfen der alten Nachrichten auf dem neuesten Stand.

### 4.4 Schritt 3: Kritische Tool-Ergebnisse aus dem „Mittelteil“

- **Mittelteil:** `history[1 : -recent_memory_size]` (alles außer erstem Eintrag und den letzten `recent_memory_size` Nachrichten).
- Darin werden Nachrichten mit `role == "tool"` und `name` in `preserve_tools` gesucht (Default: `["set_todos", "write_file", "read_file"]`).
- Pro Treffer: Inhalt auf 300 Zeichen gekürzt, als Nachricht mit `role`, `name`, `content`, `tool_call_id` in `critical_tools` gesammelt.
- Später werden maximal die **letzten 5** dieser kritischen Tool-Nachrichten in die neue History übernommen.

### 4.5 Schritt 4: Bausteine der neuen History

- **System-Prompt:** `system_prompt = history[0]` (wird **immer** übernommen; der Inhalt kann im Agent danach noch durch `new_prompt` ersetzt werden).
- **Recent:** `recent_messages = history[-recent_memory_size:]` (Standard: letzte 10 Nachrichten) – bleiben **unverändert** („raw“).

### 4.6 Schritt 5: Context Summary („Glue“) bauen

- `_build_context_summary()` erzeugt einen Textblock aus:
  - **Narrative Summary** (falls vom State gesetzt),
  - **Projekt-State:** Created/Modified/Read Dateien,
  - **Errors,** Key Decisions,
  - **Primary Goal** aus Intent.
- Format: Markdown mit Überschriften wie `### 📝 RECENT SUMMARY`, `### 📁 PROJECT STATE`, etc.

### 4.7 Schritt 6: Neue History zusammensetzen

- `new_history = [system_prompt]`
- Falls `context_summary` nicht leer: Eine **zweite System-Nachricht** mit Inhalt `context_summary` wird angehängt.
- Dann: bis zu 5 kritische Tool-Nachrichten (siehe 4.4).
- Dann: `recent_messages` (die letzten 10 Nachrichten).

Ergebnis: Deutlich weniger Nachrichten, stark reduzierte Token-Zahl bei erhaltener „Stabilität“ (Intent, State, letzte N Nachrichten).

### 4.8 Logging

- UI-Meldungen z. B. „Compressing (X/Y tokens, Z%)…“, „Compressed: N → M msgs, X → Y tokens“, „Preserved K critical tool results“, „Full history archived. Use /restore to recover.“

---

## 5. Was passiert im Agent **nach** `compress()`?

**Datei:** `vaf/core/agent.py` (direkt nach `self.history = self.context_manager.compress(self.history)`):

1. **Context Glue einbauen:**  
   `context_glue = self.context_manager._build_context_summary()` wird **nochmal** gebaut und an **`new_prompt`** angehängt (`new_prompt += ...`).  
   Achtung: `new_prompt` existiert nur, wenn in **diesem** Turn der Block „Dynamic Context“ lief (also `user_input` und `prompt_manager`). Sonst wäre `new_prompt` hier undefiniert (potenzieller Bug).

2. **PROJECT CONTEXT erhalten:**  
   Wenn in `self.history[0]["content"]` der Abschnitt `## PROJECT CONTEXT` vorkommt, wird dieser Teil extrahiert und an `new_prompt` angehängt.

3. **System-Prompt ersetzen:**  
   `self.history[0]["content"] = new_prompt`  
   – damit landet der dynamische System-Prompt (inkl. Glue und PROJECT CONTEXT) **nur bei Kompression** in der History.

---

## 6. Kurzüberblick: Wann wird was gemacht?

| Schritt                    | Wo / Wann |
|---------------------------|-----------|
| Prüfung „soll komprimiert werden?“ | Jeder Turn in `chat_step()`, wenn `usage >= 0.85` |
| `compress(history)`       | Nur wenn `should_compress(history)` True |
| Archiv (Memory + Disk)    | Immer zu Beginn von `compress()` |
| Intent/State aus History   | In `compress()` über alle Nachrichten |
| Behalten: System + letzte N + Glue + kritische Tools | In `compress()` |
| Glue + PROJECT CONTEXT in System-Prompt | Im Agent nur **wenn** in diesem Turn komprimiert wurde **und** `new_prompt` gesetzt wurde |

---

## 7. Konfiguration (ContextManager)

- **`max_tokens`:** Wird beim Anlegen des `ContextManager` gesetzt (z. B. aus Agent-Config/`n_ctx`), Standard 8192 (kann z. B. auf 128000 erhöht werden).
- **`trigger_threshold`:** 0.85 (85 %).
- **`recent_memory_size`:** 10 (letzte 10 Nachrichten bleiben roh).
- **`preserve_tools`:** `["set_todos", "write_file", "read_file"]` – nur diese Tool-Ergebnisse werden im Mittelteil explizit erhalten (max. 5).

---

## 8. Bekannte Probleme

1. **Dynamischer System-Prompt nur bei Kompression:**  
   `new_prompt` wird bei jedem User-Input gebaut, aber `history[0]["content"]` wird nur im Kompression-Block mit `new_prompt` überschrieben. Ohne Kompression bleibt der alte System-Prompt erhalten.

2. **`new_prompt` undefiniert bei Kompression ohne User-Input:**  
   Wenn in einem Turn **kein** `user_input` da ist (z. B. nur Sub-Agent-Ergebnisse), wird `new_prompt` nie gesetzt. Tritt in dem Turn trotzdem Kompression ein, führt `new_prompt += ...` zu einem `NameError`.

Diese Punkte sollten in `agent.py` behoben werden (z. B. `new_prompt` immer setzen wenn `prompt_manager` existiert, und System-Prompt auch ohne Kompression aktualisieren; bei Kompression ohne `new_prompt` den bestehenden `history[0]` behalten oder aus `history[0]` ableiten).

---

## 9. Where user-related info appears (system prompt vs. separate message)

The model receives user-related information in two ways:

### 9.1 In the system prompt: "User identity (current user)" block

**Location**: `vaf/core/system_prompt.py`, `build_prompt()` → block **"## User identity (current user)"**.

- **When `username` is set**: Name and `preferred_language` (and preferences, do's, don'ts) are read from **`user_identity.json`** in that user's workspace (`~/.vaf/users/<username>/user_identity.json`). See [USER_IDENTITY.md](USER_IDENTITY.md).
- **When `user_scope_id` is set**: In addition, **"Known facts from memory"** is read from a cache file:
  - Path: `Config.APP_DIR / "user_profile_cache" / f"{user_scope_id}.txt"`
  - Content: Result of a RAG search with the fixed query `"user profile facts preferences about this user"` (k=8).
  - **When the cache is written**: In `vaf/memory/rag.py`, `refresh_user_profile_summary(user_scope_id)` runs after **session compaction** (every N user turns). So the cache is updated periodically, not every turn.

The main system prompt therefore includes the current user profile (from `user_identity.json`) and, if present, the cached RAG summary. This is in `history[0]` once `build_prompt()` has run and the result is written into history (every turn when the dynamic prompt is applied).

### 9.2 Second system message: "Memory context (relevant to this query)"

**Location**: `vaf/core/agent.py`, immediately before the LLM call (`api_backend.chat_completion` or server payload).

- `memory_context` is passed into `chat_step(..., memory_context=None)` by the caller (headless runner, gateway, or automation), which runs a RAG search with the **current user message** as the query.
- The messages sent to the LLM are built as: first the main system prompt (`history[0]`), then a second system message with content `"## Memory context (relevant to this query)\n\n" + (memory_context or a "No memories found" placeholder)`. So the model sees: `[system prompt, memory context message, user, assistant, ...]`.

The RAG results for this specific query are therefore in a separate system message, not inside the main system prompt string. The model sees both; user-related info comes partly from the prompt (user_identity.json + optional cache) and partly from that second message (query-specific RAG results).
