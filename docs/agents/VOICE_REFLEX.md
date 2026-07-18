# Voice Reflex System (live-agent overhaul)

Single source of truth for the voice-agent reflex overhaul: barge-in, continuous
listening with selective chime-in, and awareness of unknown speakers. Read this
before touching the reflex/policy pieces (`vaf/core/voice_policy.py`, the
`should_engage`/verdict path and the rolling context buffer in
`vaf/core/voice_agent.py` + `vaf/core/web_server.py`). It complements, and does not
replace, [VOICE_AGENT.md](VOICE_AGENT.md), which documents the live-call lane itself.

Status: phased build. Phase 0 (foundation), Phase 1 (guest privacy), Phase 2
(continuous listening + grounded chime-in, scene modes, the activity dial, addressee
clarification) and the first half of Phase 3 (barge-in: interrupt the speaking agent,
8a stop-and-listen, with browser AEC) are implemented; the rest of Phase 3
(thinking-window barge-in, streaming TTS, cooperative cancel, 8b smart-resume) follows.
A visual reflex schema with the latency budget accompanies this doc.

## Why

Today the voice agent is strictly turn-based and half-duplex: one utterance becomes
one `voice_call_turn`, then STT, then exactly one LLM call, then TTS; while the agent
speaks, the microphone is deliberately held so it does not transcribe its own voice.
That makes it feel like a chatbot answering one word at a time, not a lively presence.

The goal is a reflex system, not a bigger chatbot: turn-taking, barge-in and the
"may I speak?" decision run WITHOUT the big LLM. The large model is called only when a
real content answer or tool-call is warranted.

## The three layers, ordered by cost

Each layer passes upward only when it must. Cheap-and-frequent at the top,
expensive-and-rare at the bottom.

1. **Hard-realtime (no LLM, target < 300 ms).** VAD (speech on/off), barge-in
   detection, turn-end, noise gate, and acoustic echo cancellation. Pure audio/energy
   logic: the browser energy VAD plus Silero VAD (sherpa-onnx, CPU) - both already
   present, today used batch-only.
2. **Policy / awareness (local, two-stage, ~10-40 ms).** Stage 1: rules + vocab/keyword
   triggers + embedding similarity against the owner's interests/memory. Stage 2: a
   small ONNX classifier for the ambiguous cases. Emits exactly one verdict. It is
   ALWAYS local and non-llama; it never becomes a second inference on the one llama
   server.
3. **Content LLM (only on `respond_now`).** The existing `voice_reply`
   (`vaf/core/voice_agent.py`), one call, `enable_thinking=false`, grounded, never
   fabricating. Reasoning never reaches TTS.

## The verdict

The policy layer classifies each completed utterance into exactly one state:

- `respond_now` - a real answer or tool-call is worth it; wake the content LLM.
- `store_only` - worth remembering, no reason to speak; append to the rolling
  transcript as context. Reuses the existing `<silent/>` protocol
  (`voice_agent.py` `_SILENT_MARKER`).
- `ignore` - noise, unrelated side-talk, ungrounded small-talk; dropped, costs nothing.

Two of these already exist in seed form: `should_engage()` (`voice_agent.py:172`) is a
no-LLM gate, and `<silent/>` is the "store, do not speak" primitive.

**Addressee-ambiguity clarification (Phase 2, implemented).** Some cues are address
checks - "kannst du mich hoeren", "bist du da", "can you hear me", "are you there". When
such a cue arrives from a NON-owner speaker (label `other`/`named`/`unsure`) who did not
name the agent, it is ambiguous whether it was addressed at IT or at another person in
the room, so the agent ASKS a short clarification - "meinst du mich?" / "hast du mich
gemeint?" / "ich?" - instead of barging in or silently ignoring it. The cue lexicon is
the vocab key `addressee_check`; the clarification phrasing is `addressee_clarify`. This
runs Tier-1 (no LLM: `voice_agent.wants_addressee_clarification` + a deterministic spoken
line) and never fires for the verified owner (`self`) nor for an unlabeled call (no
enrolled profile -> everyone is the owner, so there is no ambiguity). It authorizes
nothing - anti-spoofing is unchanged; it is only a spoken question.

`interesting` = ( rule / keyword / embedding match ) AND ( docks onto the owner's
memory/interests OR is on the owner's configured topic list ). No free guessing, no
hallucinated chime-ins.

## Scene-based internal modes + one activity dial

The behavior modes are INTERNAL policy states the system chooses itself; they are NOT
a user toggle and are never switched by voice command:

- `active` - lively, ready to chime in (tends to apply in a 1:1 with the owner).
- `notes_only` - listen and record only, surface later.
- `quiet` (default) - silent by default, audible only on a high interestingness score
  (tends to apply when the owner is talking to someone else).

The mode is derived deterministically from scene (1:1 / call with another person /
multi-person room, from diarization + channel), speaker label, and score.

Orthogonal to the modes there is exactly ONE high-level user setting,
`voice_awareness_activity` (continuous, quiet .. active, config default `0.5`),
implemented as a THRESHOLD in the policy scoring - not extra cognitive load, not mode
micromanagement. It scales how often the agent, on an interesting utterance, actually
chimes in audibly.

This is implemented in `voice_policy`: `derive_scene(label, recent_labels)` reads the
scene from the current speaker label plus the recent transcript labels;
`derive_mode(scene, label, activity)` picks the mode (dial at its `0.0` floor pins
`notes_only`; a 1:1 with the verified owner tends to `active`; anything else stays
`quiet`); `chime_decision(...)` folds the mode into the one dial as a threshold shift and
returns the final `speak` verdict. The owner sets a single ruler; the mode biases it per
scene - there is never a second knob to manage.

**Chime-in delivery (Phase 2).** The browser already VAD-segments the open mic and sends
EVERY utterance as a `voice_call_turn` (it holds the mic only while the agent speaks), so
overheard side-talk already reaches the backend. A chime-in therefore rides the RESPONSE
to the turn that carried the overheard utterance (a `voice_call_reply` with `chime_in:
true` and synthesized audio) - it needs no separate server-push event and no bypass of
the request/response frame. A genuinely server-INITIATED spoken push (the agent speaking
during a silence with no incoming audio) is only needed for continuous listening OUTSIDE
a call, which is deferred (see the roadmap). The chime-in content is produced by
`voice_agent.chime_in_reply` - a silence-biased, non-acting second opinion that may still
decline - and deduped within the call via `voice_policy.similar_to_any` so the agent does
not repeat itself. The owner-privacy gate extends here too: a chime-in triggered by a
guest (`speaker_ok=False`) is grounded only in general knowledge and the guest's own
words - the rolling room transcript is WITHHELD, because it can hold the owner's earlier
private talk from before the guest arrived (the buffer lives ~20 min), exactly like the
call history is withheld from a guest reply. The two ONNX-backed policy calls
(`chime_decision`, `similar_to_any`) run off the shared event loop (executor), like every
other blocking step in the live turn.

## Unknown speakers (guests)

Guests may talk to the agent and get answers to factual questions, but:

- the agent NEVER reveals the owner's private context/memory to a guest, and
- the agent NEVER runs a tool-call or delegation for a guest.

Identity comes from the voice: only a voice-verified owner (`label == self`) may create
work. The fail-closed gate `_speaker_ok` (`web_server.py:6429-6442`) and the delegate
drop when `not speaker_ok` (`voice_agent.py:585`) stay exactly as they are. A guest's
turn may reach the LLM with `speaker_ok=False` (it can speak, it cannot act).

**Guest privacy (Phase 1, implemented).** A guest who addresses the agent already gets a
spoken reply, but `voice_reply` used to build that reply with the owner's chat digest
and memory RAG in the prompt. With `speaker_ok=False` those private blocks are now
WITHHELD entirely (not merely protected by a prompt rule), the prior call HISTORY is
dropped too (it holds the owner's earlier turns and the agent's owner-grounded replies,
which are equally private - a guest must not make the model replay them), and a
`_GUEST_BLOCK` rule is added: the agent helps a guest with general questions but never
shares the owner's memory, notes, schedule, messages or contacts, and never acts on a
guest's behalf. The data is removed from the message stream, so a weak model cannot leak
what it cannot see; a guest turn is the system prompt plus only the guest's own words.
The running-delegated-task notice (`busy_block`) is withheld from guests too, since it
names the owner's private task verbatim. The agent's persona/Soul IS still expressed to
guests by design (it is the agent's character, not the owner's private data - an owner
who writes genuinely private facts into the Soul is choosing to voice them). Note this
guarantee holds only with an enrolled voice profile: without one there is no signal to
tell speakers apart, so everyone is treated as the owner (documented fail-open).

Unknown, unenrolled speakers are meant to get an ephemeral session id (`Gast A/B`).
Distinguishing guest A from guest B needs a per-speaker voice-print cluster, which needs
the whole-clip scorer to expose the utterance embedding (`score_wav` today returns only
`{score, label, name}`) plus a small per-call cluster with its own match threshold and
eviction. That is a refinement, not part of the three core wishes (a guest already gets a
guarded spoken reply and is tool-locked in Phase 1; the labels already separate
owner/known-named/unknown, which is what anti-spoofing needs), so it is DEFERRED rather
than shipped as a half-tuned heuristic that would mislabel people. A real profile is
still created only on explicit owner confirmation via the existing lane
(`vaf/core/speaker_confirm.py`).

## Reflex/policy module (local, non-llama)

`vaf/core/voice_policy.py` (new) holds the two-stage policy. Stage 1 is deterministic:

- vocab-backed trigger lexicon in a new key `awareness_triggers` (per-language keyword
  lists, exactly like the existing `stopwords.json` precedent under
  `vaf/core/vocab/data/`, consumed the way `vaf/memory/rag.py` consumes the stopwords),
  language resolved via `vocab.resolve_user_language`;
- embedding similarity against the owner's interests/memory + configured topics, using
  the existing MiniLM singleton (`vaf/memory/embeddings.py`) under the memory-safe onnx
  recipe (CPU, one thread, singleton behind a lock).

Stage 2 is a small ONNX classifier for the ambiguous middle, built to the same
memory-safe recipe so it coexists with the one llama server without contending for it.
It is not built yet - Phase 2 ships Stage 1 only (the grounding gate is sufficient and
conservative); Stage 2 plugs in later for the borderline cases.

The Phase 2 chime-in decision lives in `voice_policy.chime_decision(text, label, *,
recent_labels, topics, activity)`: it derives the scene and mode, folds the activity dial
into a threshold, and requires GROUNDING (`is_interesting` - an embedding match to the
owner's `voice_awareness_topics` above the mode-scaled bar; a trigger phrase only lowers
the bar, never satisfies it alone). No topics configured => no chime-in ever (the safe
default). The durable transcript that feeds the mode/context is
`voice_context` (session/scope-scoped, bounded, fail-open), recorded on every heard
utterance in the live turn handler and cleared at call end.

## Languages

The reflex system is broadly multilingual, with a few layers that are language-scoped:

- **Language-agnostic (all STT languages):** speaker labels (voice embeddings, not text),
  barge-in (energy), the chime-in REMARK and the clarification question (the LLM writes
  them in the call language), and the per-turn language follow.
- **Vocab cue/response lexicons** (`awareness_triggers`, `addressee_check`,
  `addressee_clarify`): shipped in ~35 languages, generated from the English seed with
  `scripts/generate_vocab.py` (the runtime never translates - it reads the generated
  `data/` files; unlisted languages fall back to English).
- **Address heuristic** (`_ADDRESS_RE`, "was this aimed at someone"): distinctive
  second-person forms for ~11 major languages, plus fully language-agnostic
  addressing-by-name (`addressed_by_name`).
- **Grounding embedding** (`all-MiniLM-L6-v2`): English-native with usable cross-lingual
  reach (German scores comparably); weaker for distant languages. A multilingual
  embedding model would lift non-Latin-script grounding - deferred.
- **Spoken output** is bounded by the installed TTS voices (the language follow only
  switches to a language the call lane can actually speak).

## Hard invariants (must not break)

- **One llama server, never two concurrent inferences** (CLAUDE.md Rule 4.6,
  `voice_agent.is_exclusive`). The policy layer is non-llama (rules/embeddings/onnx),
  CPU only. A model swap is a full reload (kills the prompt cache) and is never used for
  a per-utterance decision.
- **Anti-spoofing is absolute.** Only `label == self` may create work; `_speaker_ok` is
  fail-closed. Guests speak, never act.
- **User isolation.** The rolling transcript buffer, guest state and awareness state key
  on session/scope, never process-global.
- **Reasoning never reaches TTS**; a local reasoning model answers, it does not think
  aloud (`enable_thinking=false`).
- **A chime-in is never forced** - a forced weak model invents something to say; it must
  be grounded in real retrieved memory/history or stay silent.
- **Settings independence.** STT, TTS and LLM each follow the user's own local/cloud
  choice (mixed is allowed, including always-on STT). The reflex/policy layer always
  runs locally regardless.

## Phased roadmap

- **Phase 0 - foundation (no behavior change):** streaming-capable VAD wrapper, a
  durable session/scope-scoped rolling transcript buffer, the three-way verdict
  (`respond_now`/`store_only`/`ignore`) as a superset of today's `should_engage`, and
  the local policy module skeleton (conservative defaults so live behavior is unchanged).
- **Phase 1 - guests:** a guest who addresses the agent gets a spoken (never acting)
  reply with the owner's private context WITHHELD (guest-privacy guardrail); anti-spoofing
  unchanged. Ephemeral guest ids move to Phase 2 (they need diarization).
- **Phase 2 - continuous + chime-in (implemented):** the durable rolling transcript is
  wired into the live turn handler (`voice_context.record` on every heard utterance,
  cleared at call end); the policy chime-in gate (`voice_policy.chime_decision`, grounding
  required) upgrades interesting overheard side-talk into a brief grounded spoken remark
  (`voice_agent.chime_in_reply`, silence-biased, never acting) that rides the turn
  response (`chime_in: true`) and is deduped within the call (`similar_to_any`); the
  scene-based internal modes (`derive_scene`/`derive_mode`) and the one activity dial
  (`voice_awareness_activity`) drive it; addressee-ambiguity clarification asks "did you
  mean me?" on an ambiguous non-owner address-check. Deferred here on purpose: ephemeral
  guest ids `Gast A/B` (need per-speaker clustering, see the guests section), Stage 2
  ONNX, and any server-INITIATED push (only needed for out-of-call always-on).
- **Phase 3 - barge-in (web-call first):** the first half is implemented - browser AEC
  (`getUserMedia echoCancellation`) so the mic can stay live while the agent speaks, and
  the barge-in trigger (`watchForBargeIn` in `VoiceCallLayer.tsx`): sustained user speech
  above the gate plus a margin cuts the agent off mid-sentence (`agentAudioRef.pause()`)
  and hands control back to the listen loop (8a stop-and-listen). A barge-in during
  playback has no in-flight backend work (the LLM turn and TTS finished before the audio
  played), so pausing is enough. Deferred to the next increment: interrupting during the
  backend "thinking" window (needs the mic live before the reply arrives, a cooperative
  `voice_call_cancel` so the one local model is freed sooner, and discarding the
  superseded reply), streaming/chunked TTS, and 8b smart-resume (continue the interrupted
  reply instead of dropping it). A reference-signal AEC is a later fallback only if the
  browser AEC proves insufficient in live use.

Continuous listening OUTSIDE a call (e.g. during a film) is carried by this
architecture but productionized only after phase 2/3.
