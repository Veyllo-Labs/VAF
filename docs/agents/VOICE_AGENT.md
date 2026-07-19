# Voice Agent (Live Call)

The voice agent is the fast conversational FIRST LAYER of a live call in the
Web UI. On a call the user talks to this layer, not to the main agent: one LLM
step per turn, no tool loop, RAG snippets as the only grounding - that keeps
per-turn latency at speech level. Anything that needs real work (tools, files,
messages, research) is DELEGATED to the main agent through the normal
TaskQueue; the user keeps talking while the main agent works, and the finished
result is spoken into the call as an update.

Read this before changing: `vaf/core/voice_agent.py`, `vaf/core/voice_model.py`
(dedicated voice GGUF + one-server swap), the `voice_call_*` /
`speaker_enroll_*` handlers in `vaf/core/web_server.py`,
`web/components/VoiceCallLayer.tsx`, `web/components/VoiceCallBar.tsx`,
`web/lib/voiceCallStore.ts`, or `vaf/core/speaker_id.py` /
`vaf/core/speaker_confirm.py`.

## Requirements

- A live LLM. The lane is CONFIGURABLE (`voice_agent_provider`/`voice_agent_model`,
  admin-only, Settings > Voice; `vision_infer.select_vision_backend` pattern):
  empty = ride the main provider (API main with a key, or local main via the
  llama server); `local` = a DEDICATED local voice GGUF (default: Gemma 4 E4B,
  `voice_model.py` - chosen for natural spoken German), where the ONE llama
  server SWAPS models: the voice model holds it during the call, the main
  model takes it back while a delegated task runs (model-aware
  `backend.ensure_local_model`; the runner has a swap-back belt so a
  main-agent turn never runs on the voice model); or an API provider id =
  the call runs on that API regardless of the main provider.
  `voice_agent.available()` probes `/v1/models` for the local flavors;
  in-process library mode has no voice lane. Inherit-local is TIME-SHARED:
  the one model serves the voice agent first, and while a delegated task runs the voice
  agent goes temporarily mute (see invariant 10). `available()` is checked
  at call start: when it is false in local mode the handler feeds the same
  activity heartbeat a chat message feeds (`tray_context.register_activity()`,
  the tray watchdog then runs its locked model load - never a second
  server) and replies `reason: "model_loading"`; with a DEDICATED voice
  model the handler instead loads the voice GGUF directly
  (`voice_model.ensure_voice_model_async`, bypassing the tray heartbeat -
  which would load the MAIN model - and pushing `model_state` itself when
  ready). The window shows a loading state with a soft phone-ring tone
  (425 Hz every 2.5 s, audio only per the GPU rule) and re-sends
  `voice_call_start` once the `model_state` push reports loaded, so the
  call heals itself and greets (live incident: the call button never
  triggered the lazy load and the call opened dead until the user sent a
  chat message). The inherit flavor loads the MAIN model directly in a
  worker thread (`ServerManager.ensure_model_present` +
  `ensure_local_model`), so this works WITHOUT the desktop tray too
  (headless/server installs); the tray heartbeat is still fed, and the
  lock/reuse semantics prevent a second server either way. With local
  vision enabled the dedicated voice model also loads its OWN projector
  (its repo comes from the voice ref), so vision keeps working mid-call.
  A non-local provider that is unavailable keeps `reason: "no_model"` and
  the muted-mic state.
- The speech stack (STT + TTS, local Docker or a cloud voice provider - see
  [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md)).
- Optional but strongly recommended: an enrolled speaker profile
  (Settings > Voice). Without one, delegation is not voice-gated.

## Turn pipeline (server side, `voice_call_turn`)

0. **Exclusive-model belt** (local time-sharing): when
   `voice_agent.is_exclusive()` and the turn carries `main_busy`, the server
   answers `voice_call_error "busy_local"` immediately, before the noise
   gate and STT (invariant 10). The frontend normally never sends these
   turns; this is the server-side belt.
1. **Noise gate**: `voice_agent.active_speech_seconds()` - clips with less
   than 0.3 s of audible 30 ms frames never reach STT (Whisper-class models
   hallucinate text on silence). Convenience gate, fail-open on analysis
   errors.
2. **STT** via `speech_client.transcribe` (provider lane first). The
   detected language drives LANGUAGE FOLLOW: when it differs from the call
   language and the lane the call actually speaks with can speak it
   (`SpeechManager.call_lane_speaks`: a configured cloud TTS provider
   counts as multilingual, else the Docker container is asked for its
   INSTALLED languages; fail-closed, never a download mid-call, verdict
   cached per call), the turn answers and speaks in the detected language. The call base language is
   identity `preferred_language`, else the `default_language` config (the
   user's default voice language), else the UI locale.
3. **Speaker label**: `speaker_id.score_wav` against the owner profile and
   all named third-party profiles; the transcript gets a `[Name]: ` prefix.
   With an enrolled profile the check is AUTHORITATIVE for delegation
   (`_speaker_ok`, see invariants). The raw score runs through
   `speaker_id.resolve_label` for IN-CALL HYSTERESIS + length-awareness (per-call
   `last_self_ts`): a confident `self` verifies and, for `STICKY_WINDOW_S`, keeps a
   following borderline/short/missing score as the owner (short clips score too
   noisily to trust a downgrade), while a CLEAR stranger (reliable-length `other`
   well below the band, or a named match) flips immediately - so the owner's own
   quick replies stop flickering to a guest. A non-owner turn may queue a confirmation
   question (`speaker_confirm.maybe_request_confirmation`) without interrupting
   the call: promptly if the speaker CLAIMS to be the owner (a spoofing check),
   or, restrained, on a plain "unsure" score (adaptive reclaim). See the
   confirmation flow in [../web-ui/SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md).
   The heard utterance is also appended to the durable rolling transcript
   (`voice_context.record`, session/scope-scoped, bounded, fail-open) that the
   reflex policy reads - it outlives the 16-entry call ring and is cleared at
   call end. See [VOICE_REFLEX.md](VOICE_REFLEX.md).
3a. **Pending-answer resolution** (`voice_policy.answer_verdict`, no LLM): if the agent
   asked a question on the previous turn, a short-lived call-scoped `pending_q` was armed
   (`voice_agent.is_question`). This turn resolves it: the owner's reply (`speaker_ok`, so
   also the fail-open owner when no profile is enrolled) becomes the ANSWER (the prior
   question is injected into `voice_reply`'s owner-gated
   `_ANSWER_BLOCK`, so a terse "yes" / "at three" is understood as the answer - in a
   multi-person scene a LONGER owner utterance must be on-topic (`answer_relevance`), else it
   reads as side-talk); a "say that again" (`voice_agent.is_unclear_reply`) RE-ASKS the same
   question once (the `reask_pending` vocab line, a `reask: true` reply); a guest's clearly
   on-topic remark earns a spoken, never-acting reply while the owner's question stays open;
   anything else CONTINUES as a normal turn. Bounded by a TTL + turn budget so a stale
   question never hijacks a later utterance; it authorizes nothing (a non-owner "answer"
   stays tool-locked, and the owner's question is never replayed to a guest). See
   [VOICE_REFLEX.md](VOICE_REFLEX.md).
3b. **Addressee clarification** (`voice_agent.wants_addressee_clarification`,
   no LLM): an address-check cue ("can you hear me", "are you there", "kannst du
   mich hoeren"; the lexicon is per-language) from a
   NON-owner speaker who did not name the agent is ambiguous - the agent speaks a
   short "did you mean me?" (`addressee_clarify` vocab) and the turn ends. Never
   for the verified owner or an unlabeled call. Authorizes nothing.
4. **Addressee gate + reflex chime-in** (`voice_agent.should_engage`, no LLM):
   side talk from other/named speakers and garbled non-owner STT noise do not reach
   the direct-reply LLM. Genuine side talk (`side_talk`) is offered to the reflex
   policy (`voice_policy.chime_decision`): if it is GROUNDED in the owner's
   `voice_awareness_topics` and the scene mode + activity dial permit, the agent
   briefly CHIMES IN with a grounded, non-acting remark (`voice_agent.chime_in_reply`,
   silence-biased, deduped via `similar_to_any`), delivered as a `voice_call_reply`
   with `chime_in: true`. Never while the main agent is busy, never on garbled noise,
   and never unless the owner configured topics (empty topics = no chime-in). Otherwise
   the text enters the call history as room context and the reply is `silent`
   (invariant 8).
5. **First-layer reply**: `voice_agent.voice_reply()` - one
   `chat_completion` with `tools=None`, system prompt + RAG memory block +
   a structural digest of the OPEN CHAT (built ownership-gated at call
   start via `build_chat_digest`; the prompt tells the model to DELEGATE a
   lookup for details beyond the digest instead of guessing) + last call
   turns as history (plus the agent's own prior question when step 3a resolved
   this turn as its answer). After the reply is spoken, a `pending_q` is armed
   if the reply itself is a question (owner-only). The model may append
   `<delegate>task</delegate>` (parsed out, never spoken) or answer with
   exactly `<silent/>` (silence protocol: no TTS, keep listening). Spoken
   replies are capped at a sentence boundary (`_cap_spoken`).
6. **Delegation**: the task text goes into the TaskQueue for the CALLING
   session (`origin_channel: "voice_call"`); the main agent runs it as a
   normal turn.
7. **TTS** and the `voice_call_reply` payload (`user_text`, `speaker_label`,
   `reply`, `audio`, `delegated`, `silent`, and the flags `greeting`/`chime_in`/
   `clarify`/`reask` for the call-opening greeting, a proactive chime-in, an addressee
   check and a re-ask of the agent's own question - a silent reply has no audio and
   the frontend simply keeps listening).

Every turn writes one forensic log line (`voice_call turn: active=... label=...
speaker_ok=... text=... -> reply_len=... delegate=...`).

## Invariants (each one exists because a live incident taught it)

1. **Voice-gated delegation (anti-spoofing).** With an enrolled profile, only a
   VOICE-VERIFIED owner may create main-agent work. `web_server` derives
   `_speaker_ok` from `speaker_id.resolve_label`, fail-closed (profile present ->
   False until verified). Verification is STICKY within a call (per-call
   `last_self_ts`): a confident `self` verifies, and for `STICKY_WINDOW_S` a
   following borderline/short/missing score keeps the owner verified, so their own
   quick replies do not flicker to a guest (a clip below ~1.5s scores too noisily to
   trust a downgrade). A CLEAR stranger flips immediately: a reliable-length `other`
   well below the band, or a named third-party match, drops `_speaker_ok` and breaks
   the sticky. `voice_reply(speaker_ok=False)` still drops any delegate marker in
   CODE, and the prompt pins that the voice label outranks spoken claims ("I am Mert"
   from `[anderer_Sprecher]` stays another speaker). The sticky window is a
   deliberate, owner-approved usability/anti-spoofing trade-off: a guest speaking in
   the borderline band immediately after the owner can inherit the window, but a
   clearly different voice cannot. Named third-party profiles are known but never
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
4. **Small talk stays out of the chat.** Only delegations appear in the chat;
   the conversation itself lives in audio. A delegation renders as its own
   thing, not as a typed user message: a red-ringed bubble with a soft static
   glow and a "voice agent" tag left of the timestamp. The marker is
   `kind="voice_delegation"` - set live by the frontend and persisted by the
   headless runner on the session message (same mechanism as the
   timer/thinking bubble tags), so the styling survives reloads.
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
   Tier 0 wake word (no LLM, `addressed_by_name`): an utterance that says
   the agent's persona NAME (fuzzy >= 0.59 against each token, plus exact
   substring - STT garbles names) ALWAYS engages, for any speaker label and
   even garbled text, and pins an "you were addressed - answer, never the
   silence marker" block into the prompt. It authorizes NOTHING: the label
   rules stay in force and delegations remain voice-verified. The prompt
   also carries the persona: the configured agent name (Settings > Persona,
   fetched per call start) replaces the generic "VAF" self-description, and
   a hard-capped 500-char Soul excerpt keeps the first layer in character
   without eating the latency budget.
   Tier 1 (no LLM, `voice_agent.should_engage`): another/named speaker who
   does not address the agent (no name, no second-person form) is side talk,
   and garbled STT noise from anyone but the verified owner is dropped - the
   text still enters the call history as room context. Tier 2 (same LLM call
   that would answer anyway): the prompt's silence protocol lets the model
   reply with exactly `<silent/>` when an owner utterance is not directed at
   it - no TTS, keep listening. Spoken replies are additionally capped in
   code at a sentence boundary (`_cap_spoken`) so a derailed model can never
   fill the token budget with a monologue.
9. **A local voice turn must answer, not think.** `_local_chat` sends
   `chat_template_kwargs: {enable_thinking: false}`: a local reasoning model
   (Qwen) otherwise burns the whole token budget on `reasoning_content` and
   the turn ends with nothing to speak or delegate (live incident 18:39,
   runtime-verified both ways against Qwen3.5-4B; 7.6 s of the 8 s turn was
   silent thinking). Defense in depth for models that think anyway: a
   reply that is empty after reasoning-strip degrades to the tangled nudge,
   and the delegate-ack line ("one moment") is spoken ONLY when a delegation
   actually survived the gates - an empty reply without a surviving delegate
   must never sound like a promise. The system prompt also carries the
   user-local current time (timezone SSOT `user_time.py`), so clock/date
   questions are answered by the first layer instead of being delegated or
   refused. The delegation rule is phrased capability-first with a worked
   marker example, an explicit "never claim you have no tools" line and a
   blanket rule of thumb (EVERY request needing a tool, live data or an
   action goes to the main agent): a small local model read the old "you
   CANNOT use tools" opener as a reason to refuse a weather request instead
   of delegating it (live incident; verified against Qwen3.5-4B that
   weather/mail/news requests delegate while clock questions and small talk
   do not).
10. **Local mode time-shares ONE server, it never runs two inferences.**
   `voice_agent.is_exclusive()` is True whenever voice and main need the
   SAME llama server: inherit + local main (time-share the one model) and
   dedicated voice model + local main (the one server swaps GGUFs; a voice
   turn during a main task would fight the swap). It is False for an API
   voice lane and for dedicated-local voice + API main (there the server
   serves ONLY the call). The backend sends `exclusive` in
   `voice_call_started` and the frontend mirrors it in the store. While a
   delegated task runs (`mainTask` set) the voice agent is temporarily
   mute: the recorder loop stops sending turns, the window shows the dimmed
   avatar + muted-mic badge with `status_deaf_busy`, and a server-side belt
   answers any turn that slips through with `voice_call_error "busy_local"`
   before the noise gate and STT (pipeline step 0). The belt has its own
   server-side truth on top of the frontend flag: live SUB-AGENTS (any
   session, `subagent_ipc.get_active_tasks`) also hold the one model - the
   main turn may already have ended, but a swap mid-inference crashed a
   sub-agent live. The frontend additionally passes a `mainBusy` hint
   (chat generation / workflow / sub-agent, the stop-button condition) into
   the layer, so ANY main-lane work mutes the call - not only
   voice-delegated tasks. Call-start load/pre-warm paths are guarded the
   same way, and the call is pinned to its ORIGIN session (chat switches
   must not re-route turns or results). When the result
   callback fires, listening resumes; with a dedicated voice model the
   first turn after the result pays the swap back to the voice GGUF
   (seconds, warm). This applies the repo-wide local-mode rule to the call:
   ONE llama server, never a second concurrent inference (swapping, not
   parallelism).

## WebSocket events

Client -> server: `voice_call_start` (`ui_lang`), `voice_call_turn` (`audio`
base64 WAV 16 kHz mono, `format:"wav"`, `sessionId`, `main_busy`,
`pending_task`), `voice_call_end`, `voice_call_speak` (`text` - result
announcements; replies as `speaker_enroll_tts` so the chat TTS handler never
reacts). Server -> client: `voice_call_started` (`ok`, `lang`, `exclusive`,
and when ok is false `reason: "model_loading"` - local model load kicked,
the frontend shows a loading state and re-sends `voice_call_start` on the
`model_state {loaded:true}` push - or `"no_model"`), `voice_call_reply`,
`voice_call_error` (`no_call` | `bad_format` | `no_speech` | `llm_failed` |
`busy_local` - the frontend keeps listening). A `voice_call_reply` may carry
`chime_in: true` (a proactive grounded remark on overheard side talk) or
`clarify: true` (a "did you mean me?" addressee check); both carry audio and play
through the existing reply path unchanged, so the frontend needs no special handling.

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
  (info left, waveform centered, mute/hangup right). The user-side waveform
  is a REAL mic level meter (AnalyserNode on the shared stream from
  `web/lib/voiceCallAudio.ts`) with a draggable noise-gate marker: bars left
  of the line are gray (ignored), right of it red (recorded); the marker
  sets `store.gateLevel`, which IS the live VAD threshold in
  `VoiceCallLayer` (persisted in localStorage, effective mid-call).
- During a call the chat avatars animate away (`voice-call-hide-avatars`);
  the window is the single agent presence.

## Barge-in (Phase 3, 8a stop-and-listen)

The user can interrupt the SPEAKING agent naturally. The mic is opened with
`echoCancellation` (plus `noiseSuppression`/`autoGainControl`), so the browser
cancels the agent's own TTS from the mic input and the two lanes can be live at
once. While the agent speaks, `watchForBargeIn` (`VoiceCallLayer.tsx`) runs a
level detector on the AEC'd mic; sustained voiced energy above the VAD gate PLUS
`BARGE_MARGIN` (a residual-echo guard, since browser AEC is imperfect) for
`BARGE_SUSTAIN_MS` pauses the agent audio (`agentAudioRef.current.pause()`) and
hands control straight back to the listen loop, with a short "go ahead" earcon.
The interrupted reply is abandoned (8a: stop and listen, not resume); the user's
continued speech is captured by the resumed listen loop. The onset lost before
the cut is bounded by `BARGE_SUSTAIN_MS`; those tunables are meant to be adjusted
live if the agent talks over the user or trips on its own voice.

Because the reply's LLM turn and TTS finish on the backend BEFORE the audio plays,
a barge-in during playback has no in-flight backend work to cancel - pausing the
audio is enough. Interrupting during the "thinking" window (backend still
generating), streaming/chunked TTS, a cooperative `voice_call_cancel`, and 8b
smart-resume are the deferred next increment (see
[VOICE_REFLEX.md](VOICE_REFLEX.md), Phase 3). Barge-in is web-call only for now;
a reference-signal AEC is the fallback only if the browser AEC proves insufficient
in live use.

## Speaker identification and confirmation

The voice DB (owner profile + named third parties), the speaker-confirmation
flow (claim/unsure paths; messenger question with audio attachment, web card fallback, "no,
that's Peter" naming) and its hard rules live in
[SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md) (section "Speaker
identification and the confirmation flow"). Core modules:
`vaf/core/speaker_id.py`, `vaf/core/speaker_confirm.py`.

## Spoken strings

Every fixed line the voice stack speaks or sends (call greeting, spoken
fallbacks, the confirmation question and acks, the enrollment script) lives
in the vocabulary book (`vaf/core/vocab`, see
[VOCABULARY_BOOK.md](../platform/VOCABULARY_BOOK.md)) under the keys
`voice_*`, `speaker_confirm_*`, `speaker_enroll_*` and the reflex key
`addressee_clarify` (the spoken "did you mean me?"). The reflex system also
reads two MATCHED lexicons from the same book, `awareness_triggers` and
`addressee_check` (cues it substring-matches, not lines it speaks). Adding a language
means adding phrasings there (or running `scripts/generate_vocab.py`);
missing languages fall back to English per phrase. Never hardcode a spoken
string in voice code. Detection heuristics (reasoning-leak patterns) stay in
`voice_agent.py` on purpose: they track the language the MODEL thinks in,
not the user's.

## Config

`speaker_id_enabled`, `speaker_id_threshold`, `speaker_id_band`,
`speaker_id_confirmation_enabled` (see
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)). The voice lane's own keys are
`voice_agent_provider` and `voice_agent_model` (admin-only, empty = follow
the main provider; see Requirements above); TTS/STT follow the speech stack.
Reflex awareness (user-writable, see [VOICE_REFLEX.md](VOICE_REFLEX.md)):
`voice_awareness_activity` (the one quiet..active chime-in dial) and
`voice_awareness_topics` (the owner's interest topics that ground a chime-in).

## Tests and change notes

- `tests/test_voice_agent.py` - first-layer contracts (gating, delegate
  protocol, busy/speaker guards, reasoning filter, noise gate, the
  configurable lane: dedicated-local swap + exclusivity, API override),
  guest privacy, chime-in (grounded/silent/no-delegate/guest-memory), and
  addressee clarification.
- `tests/test_voice_policy.py` - reflex Stage 1: trigger match, embedding
  interest score, scene modes + activity dial (`chime_decision`), chime dedup.
- `tests/test_local_model_swap.py` - model-aware server reuse,
  `ensure_local_model` swap contract, voice/vision ref resolution.
- `tests/test_speaker_id.py`, `tests/test_speaker_confirm.py` - voice DB and
  confirmation flow.
- Changes under `vaf/core/` need a backend restart; `web/` changes need
  `npm run build` (dev reload is not enough for the packaged app).
