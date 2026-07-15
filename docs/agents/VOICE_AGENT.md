# Voice Agent (Live Call)

The voice agent is the fast conversational FIRST LAYER of a live call in the
Web UI. On a call the user talks to this layer, not to the main agent: one LLM
step per turn, no tool loop, RAG snippets as the only grounding - that keeps
per-turn latency at speech level. Anything that needs real work (tools, files,
messages, research) is DELEGATED to the main agent through the normal
TaskQueue; the user keeps talking while the main agent works, and the finished
result is spoken into the call as an update.

Read this before changing: `vaf/core/voice_agent.py`, the `voice_call_*` /
`speaker_enroll_*` handlers in `vaf/core/web_server.py`,
`web/components/VoiceCallLayer.tsx`, `web/components/VoiceCallBar.tsx`,
`web/lib/voiceCallStore.ts`, or `vaf/core/speaker_id.py` /
`vaf/core/speaker_confirm.py`.

## Requirements

- An API provider (`provider != "local"` with a key): the lane rides the main
  provider like `vision_infer.py`; pure-local calls are a later iteration.
  `voice_agent.available()` gates the UI button.
- The speech stack (STT + TTS, local Docker or a cloud voice provider - see
  [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md)).
- Optional but strongly recommended: an enrolled speaker profile
  (Settings > Voice). Without one, delegation is not voice-gated.

## Turn pipeline (server side, `voice_call_turn`)

1. **Noise gate**: `voice_agent.active_speech_seconds()` - clips with less
   than 0.3 s of audible 30 ms frames never reach STT (Whisper-class models
   hallucinate text on silence). Convenience gate, fail-open on analysis
   errors.
2. **STT** via `speech_client.transcribe` (provider lane first).
3. **Speaker label**: `speaker_id.score_wav` against the owner profile and
   all named third-party profiles; the transcript gets a `[Name]: ` prefix.
   With an enrolled profile the check is AUTHORITATIVE for delegation
   (`_speaker_ok`, see invariants). An "unsure" score queues a confirmation
   question (`speaker_confirm.maybe_request_confirmation`) without
   interrupting the call.
4. **Addressee gate** (`voice_agent.should_engage`, no LLM): side talk from
   other/named speakers and garbled non-owner STT noise stop here - the text
   enters the call history as room context and the reply is `silent`
   (invariant 8).
5. **First-layer reply**: `voice_agent.voice_reply()` - one
   `chat_completion` with `tools=None`, system prompt + RAG memory block +
   last call turns as history. The model may append
   `<delegate>task</delegate>` (parsed out, never spoken) or answer with
   exactly `<silent/>` (silence protocol: no TTS, keep listening). Spoken
   replies are capped at a sentence boundary (`_cap_spoken`).
6. **Delegation**: the task text goes into the TaskQueue for the CALLING
   session (`origin_channel: "voice_call"`); the main agent runs it as a
   normal turn.
7. **TTS** and the `voice_call_reply` payload (`user_text`, `speaker_label`,
   `reply`, `audio`, `delegated`, `silent` - a silent reply has no audio and
   the frontend simply keeps listening).

Every turn writes one forensic log line (`voice_call turn: active=... label=...
speaker_ok=... text=... -> reply_len=... delegate=...`).

## Invariants (each one exists because a live incident taught it)

1. **Voice-gated delegation (anti-spoofing).** With an enrolled profile, only
   a turn verified as the OWNER (`label == "self"`) may create main-agent
   work. `web_server` computes `_speaker_ok` fail-closed (profile present ->
   False until proven "self"; unsure/other/failed scoring stay False) and
   `voice_reply(speaker_ok=False)` drops any delegate marker in CODE. The
   system prompt additionally pins: the label comes from voice verification
   and outranks spoken claims ("I am Mert" from `[anderer_Sprecher]` stays
   another speaker). Named third-party profiles are known but never
   authorized.
2. **No delegation while the main agent works.** The frontend sends
   `main_busy` + `pending_task`; the prompt carries a busy block and the code
   drops markers anyway (a casual "okay thanks" must never spawn or disturb a
   run). Promises require the marker in the SAME reply, and "still running"
   may only be claimed when the busy block says so.
3. **Reasoning never reaches TTS.** Reasoning models stream thoughts as
   `<think>` sentinel chunks; the streaming walker drops them (handles open
   and close in one chunk), `_strip_reasoning` also removes UNCLOSED blocks
   (truncation), the token budget leaves room to finish thinking, and an
   all-reasoning reply degrades to a short spoken fallback.
4. **Small talk stays out of the chat.** Only delegations appear in the chat
   (as the voice agent's message to the main agent); the conversation itself
   lives in audio.
5. **Result callback is anchored on `message_complete`** (the same event that
   plays the completion chime): only THIS session's completion counts,
   `[ASYNC_ACK]` and empty content keep waiting, think/context blocks are
   sanitized before speaking (`sanitizeForSpeech`), the mic is held while the
   result plays (the agent must not transcribe its own voice), a TTS failure
   gets a short spoken notice, and the spoken text is appended to the call
   history via `voice_call_speak` so the voice agent KNOWS the result was
   delivered.
6. **Mute is real mute.** The audio track is disabled while muted (the
   recorder captures silence), toggling discards the in-flight utterance, and
   unmuting opens a 400 ms grace window that swallows the toggle click. A
   turn needs >= 350 ms of ACCUMULATED voiced time (`MIN_SPEECH_MS`) - a
   click never becomes a message.
7. **All call animations are transform/opacity only and finite** (GPU leak
   rule); enter/exit choreography runs through the store's `closing` phase.
8. **Addressee gating on the always-open mic, without extra LLM turns.**
   Tier 1 (no LLM, `voice_agent.should_engage`): another/named speaker who
   does not address the agent (no name, no second-person form) is side talk,
   and garbled STT noise from anyone but the verified owner is dropped - the
   text still enters the call history as room context. Tier 2 (same LLM call
   that would answer anyway): the prompt's silence protocol lets the model
   reply with exactly `<silent/>` when an owner utterance is not directed at
   it - no TTS, keep listening. Spoken replies are additionally capped in
   code at a sentence boundary (`_cap_spoken`) so a derailed model can never
   fill the token budget with a monologue.

## WebSocket events

Client -> server: `voice_call_start` (`ui_lang`), `voice_call_turn` (`audio`
base64 WAV 16 kHz mono, `format:"wav"`, `sessionId`, `main_busy`,
`pending_task`), `voice_call_end`, `voice_call_speak` (`text` - result
announcements; replies as `speaker_enroll_tts` so the chat TTS handler never
reacts). Server -> client: `voice_call_reply`, `voice_call_error`
(`no_call` | `bad_format` | `no_speech` | `llm_failed` - the frontend keeps
listening).

Enrollment (guided live call in Settings): `speaker_enroll_start/round/
finalize/abort`, `speaker_enroll_speak`, `speaker_profile_get/delete` with
replies `speaker_enroll_started/round_result`, `speaker_profile`,
`speaker_enroll_aborted`, `speaker_enroll_tts`.

Speaker confirmation events (`speaker_confirm_*`) are documented in
[WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md).

## Frontend pieces

- `web/lib/voiceCallStore.ts` - Zustand store shared by bar and controller
  (`active`, `closing`, `speaker`, `agentMode`, `statusKey`, `mainTask`,
  `muted`, `hangupRequested`).
- `web/components/VoiceCallLayer.tsx` - controller + agent window (top-left):
  hands-free VAD loop (silence auto-stop, max utterance cap), noise gates,
  mute handling, result callback, enter/exit choreography, red in-call ring.
- `web/components/VoiceCallBar.tsx` - the red bar overlaying the chat input
  (info left, waveform centered, mute/hangup right).
- During a call the chat avatars animate away (`voice-call-hide-avatars`);
  the window is the single agent presence.

## Speaker identification and confirmation

The voice DB (owner profile + named third parties), the "unsure" confirmation
flow (messenger question with audio attachment, web card fallback, "no,
that's Peter" naming) and its hard rules live in
[SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md) (section "Speaker
identification and the confirmation flow"). Core modules:
`vaf/core/speaker_id.py`, `vaf/core/speaker_confirm.py`.

## Spoken strings

Every fixed line the voice stack speaks or sends (call greeting, spoken
fallbacks, the confirmation question and acks, the enrollment script) lives
in the vocabulary book (`vaf/core/vocab`, see
[VOCABULARY_BOOK.md](../platform/VOCABULARY_BOOK.md)) under the keys
`voice_*`, `speaker_confirm_*` and `speaker_enroll_*`. Adding a language
means adding phrasings there (or running `scripts/generate_vocab.py`);
missing languages fall back to English per phrase. Never hardcode a spoken
string in voice code. Detection heuristics (reasoning-leak patterns) stay in
`voice_agent.py` on purpose: they track the language the MODEL thinks in,
not the user's.

## Config

`speaker_id_enabled`, `speaker_id_threshold`, `speaker_id_band`,
`speaker_id_confirmation_enabled` (see
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)). The voice lane itself has no
own keys: provider/model follow the main provider; TTS/STT follow the speech
stack.

## Tests and change notes

- `tests/test_voice_agent.py` - first-layer contracts (gating, delegate
  protocol, busy/speaker guards, reasoning filter, noise gate).
- `tests/test_speaker_id.py`, `tests/test_speaker_confirm.py` - voice DB and
  confirmation flow.
- Changes under `vaf/core/` need a backend restart; `web/` changes need
  `npm run build` (dev reload is not enough for the packaged app).
