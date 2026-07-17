# Voice Reflex System (live-agent overhaul)

Single source of truth for the voice-agent reflex overhaul: barge-in, continuous
listening with selective chime-in, and awareness of unknown speakers. Read this
before touching the reflex/policy pieces (`vaf/core/voice_policy.py`, the
`should_engage`/verdict path and the rolling context buffer in
`vaf/core/voice_agent.py` + `vaf/core/web_server.py`). It complements, and does not
replace, [VOICE_AGENT.md](VOICE_AGENT.md), which documents the live-call lane itself.

Status: design + phased build. Phase 0 (foundation) is being implemented; phases 1-3
follow. A visual reflex schema with the latency budget accompanies this doc.

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

**Addressee-ambiguity clarification (Phase 2).** Some cues are address checks -
"kannst du mich hoeren", "bist du da", "can you hear me", "are you there" (in the
`awareness_triggers` lexicon). When such a cue arrives but the agent cannot tell it was
addressed at IT (an unclear speaker, or possibly directed at another person in the
room), the agent should ASK a short clarification - "meinst du mich?" / "hast du mich
gemeint?" / "ich?" - instead of either barging in or silently ignoring it. The
clarification phrasing lives in a vocab key (`addressee_clarify`, to be added in
Phase 2); it never authorizes anything (anti-spoofing unchanged).

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
`voice_awareness_activity` (continuous, quiet .. active), implemented as a THRESHOLD in
the policy scoring - not extra cognitive load, not mode micromanagement. It scales how
often the agent, on an interesting utterance, actually chimes in audibly.

## Unknown speakers (guests)

Guests may talk to the agent and get answers to factual questions, but:

- the agent NEVER reveals the owner's private context/memory to a guest, and
- the agent NEVER runs a tool-call or delegation for a guest.

Identity comes from the voice: only a voice-verified owner (`label == self`) may create
work. The fail-closed gate `_speaker_ok` (`web_server.py:6429-6442`) and the delegate
drop when `not speaker_ok` (`voice_agent.py:585`) stay exactly as they are. A guest's
turn may reach the LLM with `speaker_ok=False` (it can speak, it cannot act). Unknown,
unenrolled speakers get an ephemeral session id (`Gast A/B`); a real profile is created
only on explicit owner confirmation via the existing lane
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
- **Phase 1 - guests:** open the gate so guest content flows into the policy and can
  produce a spoken (never acting) reply; ephemeral guest ids; anti-spoofing unchanged.
- **Phase 2 - continuous + chime-in:** durable buffer, the policy gate, a
  server-initiated spoken chime-in path behind the existing grounding/dedup/tracked-
  request lifecycle (not the thinking scheduler), scene-based modes and the activity dial.
- **Phase 3 - barge-in (web-call first):** browser AEC + full-duplex capture during TTS,
  streaming/chunked TTS, the barge-in trigger with in-flight LLM abort, `stop&listen`
  first (smart-resume later).

Continuous listening OUTSIDE a call (e.g. during a film) is carried by this
architecture but productionized only after phase 2/3.
