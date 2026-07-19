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
Also implemented on top of these: in-call pending-answer, owner-toggled GUEST ENGAGEMENT
(the dynamic scene block, a deterministic engage command, and a tightened arm gate),
OWNER RECOVERY when the voice is mislabeled ("did you mean me?" plus a never-silent answer
and screen/messenger confirmation), a multilingual reply prompt (the model is never pinned
to one language), a silent-drop backstop on addressed owner turns, and the SHARED
GROUP-CONVERSATION context that lets the model follow a multi-person, multi-language
dynamic. A visual reflex schema with the latency budget accompanies this doc.

## Why

Today the voice agent is strictly turn-based and half-duplex: one utterance becomes
one `voice_call_turn`, then STT, then exactly one LLM call, then TTS; while the agent
speaks, the microphone is deliberately held so it does not transcribe its own voice.
It answers one utterance at a time, with no way to keep listening or to interject.

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
   triggers + embedding similarity against the owner's configured topics. Stage 2: a
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

Two of these already exist in seed form: `should_engage()` (`voice_agent.py`) is a
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

**Answering the agent's own question (in-call pending answer, implemented).**
When the agent itself asks the user a question, the NEXT utterance is probably its answer,
but the verdict classifies every utterance independently, so a brief reply ("yes", "at
three") is never linked to the question. Linking it back takes care: a bystander's words
must not count as the owner's answer, and the owner's question must not leak to a guest.
The turn handler therefore arms a short-lived, call-scoped `pending_q` when
a reply is itself a question (`voice_agent.is_question`, a no-LLM multi-script "?" check),
and resolves it at the top of the NEXT turn via `voice_policy.answer_verdict`:

- `answer` (owner) - the owner replied (`speaker_ok`: the verified `self`, or - with no
  enrolled profile - the fail-open owner, the same model the rest of the pipeline uses). A
  terse reply or a 1:1 scene is the answer by adjacency (a short reply carries almost no
  embedding signal); in a multi-person scene a LONGER owner utterance must be on-topic
  (`answer_relevance` >= the scene/activity-scaled bar), else it reads as side-talk to the
  other person and the link is not forced. On an answer the agent's own question is injected
  into `voice_reply` (the owner-gated `_ANSWER_BLOCK`) and the pending state clears.
- `reask` - the owner asked to repeat ("wie bitte?", `voice_agent.is_unclear_reply`): the
  agent re-asks the SAME question once (the `reask_pending` vocab line, ~35 languages),
  capped by `MAX_REASK`, then continues.
- `answer` (guest) - a guest's clearly ON-TOPIC remark (relevance >= the bar) earns a spoken
  reply that NEVER acts and never receives the owner's question (`speaker_ok` False withholds
  the `_ANSWER_BLOCK` and drops any delegate); the owner's question stays OPEN, since they
  have not answered. A guest who addresses the agent by name is already handled by the normal
  wake-word gate, so this only adds the un-named on-topic case.
- `continue` - anything else (an off-topic reply, or a reply after the window): falls through
  as a normal turn. The pending state is bounded by a short TTL (`PENDING_Q_TTL_S`) and a turn
  budget (`PENDING_Q_TURNS`) so a stale question never hijacks a later unrelated utterance.

This authorizes nothing: a non-owner "answer" stays tool-locked by `speaker_ok`, and the
owner's (possibly private) question is never replayed to a guest (the answer block is gated
on `speaker_ok` like the chat/memory blocks). The scene comes from `derive_scene` and the
relevance bar from the one `voice_awareness_activity` dial; the relevance thresholds are
provisional and want live calibration, exactly like the chime band.

`interesting` = ( rule / keyword / embedding match ) AND ( embedding-matches the
owner's configured topic list, `voice_awareness_topics` ). No free guessing, no
hallucinated chime-ins.

## Scene-based internal modes + one activity dial

The behavior modes are INTERNAL policy states the system chooses itself; they are NOT
a user toggle and are never switched by voice command:

- `active` - ready to chime in (tends to apply in a 1:1 with the owner).
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
a call, which is deferred (see the roadmap). The chime-in gate fires only on genuine
`side_talk` (never on garbled non-owner STT) and never while the main agent is busy on a
delegated task (a remark over a running task is noise). The chime-in content is produced
by `voice_agent.chime_in_reply` - a non-acting content layer that offers ONE brief,
natural remark on a relevant line (the policy already decided it is worth a comment, so
the content layer phrases rather than re-judges) and stays silent on an irrelevant one -
deduped within the call via `voice_policy.similar_to_any` so the agent does not repeat
itself. The owner-privacy gate extends here too: a chime-in triggered by a
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

Identity comes from the voice: only a voice-verified owner may create work. `_speaker_ok`
is derived by `speaker_id.resolve_label` with in-call hysteresis - a confident `self`
verifies and bridges following borderline/short scores for a bounded window, while a CLEAR
stranger (reliable-length `other` well below the band, or a named match) flips immediately
(see [VOICE_AGENT.md](VOICE_AGENT.md) invariant 1) - and the delegate drop when `not
speaker_ok` (`voice_agent.py`) is unchanged. A clear guest's turn reaches the LLM with
`speaker_ok=False` (it can speak, it cannot act).

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

Unknown, unenrolled speakers are meant to get an ephemeral session id (`Guest A/B`).
Distinguishing guest A from guest B needs a per-speaker voice-print cluster, which needs
the whole-clip scorer to expose the utterance embedding (`score_wav` today returns only
`{score, label, name}`) plus a small per-call cluster with its own match threshold and
eviction. That is a refinement, not part of the three core wishes (a guest already gets a
guarded spoken reply and is tool-locked in Phase 1; the labels already separate
owner/known-named/unknown, which is what anti-spoofing needs), so it is DEFERRED rather
than shipped as a half-tuned heuristic that would mislabel people. A real profile is
still created only on explicit owner confirmation via the existing lane
(`vaf/core/speaker_confirm.py`).

**Owner-toggled guest engagement (implemented).** By default a guest who does not
address the agent is side-talk: the agent overhears and stays silent (`store_only`),
which is the right behavior when the owner is simply talking to someone else. The gap
was the opposite case - the owner WANTS the agent to take part ("answer her", "talk to
my mother, she is asking you something") - for which there was no path: the guest kept
being classified as side-talk and the agent never engaged. The fix is a per-call,
owner-only toggle, default OFF so the working side-talk detection is unchanged:

- **Arm.** On an owner turn the dynamic scene block (below) tells the model to emit
  `<talk_to_guest/>` when the owner asks it to include the other person. The marker is
  parsed out of the reply (never spoken), honored ONLY for a verified owner turn
  (`speaker_ok`, enforced in both `_postprocess_reply` and the web-server handler), and
  sets `_call['engage_guests']` with a sliding TTL (`voice_policy.GUEST_ENGAGE_TTL_S`,
  300 s, refreshed on every active turn).
- **Engage.** While the toggle is live, `classify_utterance(..., engage_guests=True)`
  turns a guest turn that would be `side_talk` into `respond_now` (reason
  `engage_guest`) - the guest gets a spoken reply. Garbled guest noise is still never
  engaged, and the owner path is untouched.
- **Still tool-locked.** An engaged guest turn runs with `speaker_ok=False` exactly like
  any guest reply: no delegation, no owner-private context, no call history. Engagement
  only lets the agent SPEAK to the guest; it never lets a guest act or read the owner's
  world. Anti-spoofing is unchanged.
- **Disarm.** The owner ends it with `<end_guest/>` (offered by the scene block while the
  mode is on), the sliding TTL lapses, or the call ends. A guest can never arm or disarm
  the mode (the markers are speaker_ok-gated).

The toggle is profile-gated like the rest of guest handling: without an enrolled voice
profile everyone is the owner, so there is no guest to engage (documented fail-open).
Targeting a SPECIFIC guest ("answer HER, not him") needs the per-speaker voice-print
cluster that is deferred above; v1 engages any non-owner in the room.

**Dynamic scene block (`voice_agent._scene_block`, implemented).** The reflex layer
already knows the situation (scene from `voice_policy.derive_scene`, the speaker label,
the conversation language, the engage toggle); it feeds a small English situation block
into the `voice_reply` system prompt so the content model behaves appropriately instead
of following hard-coded rules. It is EMPTY for a one-to-one call (the common case,
prompt unchanged) and only appears when a guest is present (multi-party). It states that
the call is multi-party, names the current language, and - depending on whose turn it is
and whether the toggle is on - either primes `<talk_to_guest/>`/`<end_guest/>` (owner
turn) or tells an engaged-guest turn to reply directly, in the GUEST'S OWN language, and
never with the silence marker. (The base prompt is multilingual by design - the first
layer replies in the language it is addressed in or the one the owner asks for, and is
told never to claim it cannot speak a language; a hard-coded single-language instruction
had made the model refuse other languages even though it is fluent in them.)
It carries NO owner-private data (only the presence of a guest and the behavior), so
unlike the chat/memory blocks it is safe to show on a guest turn. The web server builds
the `{multi, engage_guests}` scene dict from the current label plus the recent transcript
labels and passes it as `voice_reply(scene=...)`.

**Shared group-conversation context (`_GROUP_BLOCK`, implemented).** The single biggest
"it doesn't understand the dynamic" gap was that a guest turn is deliberately
context-starved (history/chat/memory all withheld for owner privacy), so the model saw
ONE context-free line and stalled or went silent. While guest engagement is active, the
model is instead given the SHARED, spoken-aloud room transcript - multilingual, in order,
each line labelled with who spoke (`[self]`/`[<name>]`/`[agent]`) - so it can follow the
back-and-forth and reply to the latest line in context. This is NOT owner-private: it is
what everyone present heard, and the web server scopes it to talk AFTER engagement started
(`engage_guests["since_wall"]`, filtered via `voice_context.recent(since=...)`), so the
owner's earlier private 1:1 never appears - the boundary is enforced at the build site, not
in `voice_reply`. The agent's own spoken replies are recorded into the transcript (label
`agent`) so the back-and-forth is complete. This block is what turns guest engagement from
a string of context-free single replies into a coherent multi-person conversation.

**Reliable arming (deterministic command + a tightened gate, implemented).** The marker
path depends on the (weak, local) model emitting `<talk_to_guest/>`; live, the model
sometimes chose silence on the owner's command turn and never armed. Two hardenings make
arming reliable AND strictly harder to abuse:

- **Deterministic engage command.** `voice_agent.engage_command_match` is a Tier-1 (no
  LLM) substring scan over a new vocab key `engage_guest_cmd` (per-language phrasings of
  "answer her / talk to the other person"). When an owner turn matches, the mode arms
  directly, independent of what the model emits. Owner-gated (see next), so a guest can
  never trigger it.
- **Arm gate = `speaker_ok AND confident != 'borderline'`.** Arming (marker OR command)
  is tightened from plain `speaker_ok` to a REAL verified self: a bridged-borderline
  sticky turn may still SPEAK as the owner, but a short/ambiguous clip right after the
  owner can no longer turn the mode on. With no profile enrolled (`confident is None`,
  fail-open owner) arming still works. This makes arming strictly harder, never easier -
  it never widens what a non-owner can do.

**Owner recovery when the voice is mislabeled (implemented).** The single biggest live
blocker was not the engagement logic but SPEAKER-ID: in a multi-person call every
reliable-length guest turn clears the owner's in-call sticky bridge
(`web_server` sets `last_self_ts=None` on a confident `other`), so the owner's own short
command can demote to `unsure` and is then treated as a non-owner - an explicit "answer
her" was silently dropped. Weakening the sticky is NOT the fix: an adversarial trace
confirmed that preserving it would bridge a guest's SHORT follow-up clip to the owner
(speaker_ok True), a real spoofing hole. Instead the agent RECOVERS the owner without
ever acting on an unverified turn:

- **Ask (`voice_agent.wants_speaker_recheck` + 2c-recheck).** An ambiguous turn (label
  `unsure`, a profile IS enrolled but the voice did not verify) that is nonetheless
  DIRECTED at the agent (second-person address, wake word, or an engage command) triggers
  a spoken "did you mean me?" (`addressee_clarify` vocab, in the turn language). Per-call
  cooldown so it never nags. It authorizes nothing - it is a question.
- **Fresh sample + out-of-band confirm.** The owner's answer is a new voice sample. The
  restrained screen/messenger confirmation already fires from the speaker block
  (`speaker_confirm.maybe_request_confirmation`), and an authenticated "yes" adaptively
  sharpens the owner profile (`speaker_id.add_owner_sample`) so the mislabel gets rarer
  over time - attacking the root cause, not just the symptom.
- **Recover (2a-recover).** The reply to "did you mean me?" is ALWAYS reacted to - the
  agent never stays silent on the answer to its own question (a live gap: a mislabeled
  "yes, I mean you" was dropped as side-talk). If the reply verifies as a REAL confident
  self, the owner is recovered and the pending check dropped; guest engagement arms ONLY
  from an engage command spoken on THIS verified-self turn ("yes, answer her"), never from
  the earlier asked-about text (that text came from an UNVERIFIED, possibly guest speaker,
  so honoring its stored command would let guest content arm the mode via an unrelated
  owner turn - a confused-deputy gap the adversarial review caught). If the reply is
  affirmative ("yes") but the voice STILL did not verify, the agent speaks a short "I could
  not place your voice, please confirm on your screen or messenger" (`speaker_recheck_confirm`
  vocab) and leans on the confirmation card already queued in the speaker block - an
  authenticated yes learns the voice, so the mislabel gets rarer. A clear "no" or an expired
  window just drops the pending check. A guest answering never scores confident self, so it
  can never recover the owner; the voice alone still grants nothing.

The guarantee stays intact: acting/arming still requires a real self score OR an
authenticated-channel confirmation; the voice itself never modifies the profile (learning
is confirmation-gated, see [../web-ui/SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md)).

**No silent-drop of an addressed owner turn (implemented).** On a turn explicitly directed
at the owner's agent (wake word, or a resolved answer to the agent's own question) the
prompt forbids the silence marker, but the weak local model sometimes emitted a bare
`<silent/>` anyway and the owner's turn was dropped. `_postprocess_reply(addressed=...)`
now overrides a bare `<silent/>` to a spoken "say that again" nudge when `addressed AND
speaker_ok` - so the owner is never silently ignored. Guests and non-addressed owner
side-talk keep the silence protocol untouched (the override is scoped to the verified
owner on an addressed turn).

## Reflex/policy module (local, non-llama)

`vaf/core/voice_policy.py` (new) holds the two-stage policy. Stage 1 is deterministic:

- vocab-backed trigger lexicon in a new key `awareness_triggers` (per-language keyword
  lists, exactly like the existing `stopwords.json` precedent under
  `vaf/core/vocab/data/`, consumed the way `vaf/memory/rag.py` consumes the stopwords),
  language resolved via `vocab.resolve_user_language`;
- embedding similarity against the owner's configured topics (`voice_awareness_topics`), using
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
- **Anti-spoofing.** Only a VOICE-VERIFIED owner may create work; `_speaker_ok` is
  fail-closed. Verification is STICKY within a call (a confident `self` bridges following
  borderline/short scores for a bounded window - an owner-approved usability trade-off; a
  clearly different voice, a reliable-length `other` well below the band or a named match,
  always flips). See [VOICE_AGENT.md](VOICE_AGENT.md) invariant 1. A clear guest speaks,
  never acts.
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
  guest ids `Guest A/B` (need per-speaker clustering, see the guests section), Stage 2
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
