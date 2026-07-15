# Vocabulary Book (backend canned phrases)

The Vocabulary Book is the backend counterpart to the Web UI i18n system. The Web UI uses next-intl, which
is **frontend-only** — backend and CLI text are explicitly out of its scope (see [I18N.md](I18N.md)). The
Vocabulary Book localizes the small set of **fixed phrases that the backend emits itself** — for example the
thinking-run "are you there?" nudge — so they can vary and follow the user's language **without an LLM call at
runtime**.

## Scope

- **In scope:** short, fixed phrases the backend sends to the user, where we want variation and the user's
  language but do not want to pay for an LLM call (e.g. the thinking-run nudge).
- **Out of scope:**
  - Full agent replies and proactive questions — the model already writes those in the user's language.
  - Web UI strings — handled by next-intl ([I18N.md](I18N.md), [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md)).
  - Long-form or dynamic content.

## Architecture

- **Location:** `vaf/core/vocab/`.
- **Data format:** one JSON file per phrase **key** under `vaf/core/vocab/data/<key>.json`, mapping a 2-letter
  language code to a list of phrasings:

  ```json
  {
    "en": ["Hey {name}, you around?", "{name}, are you there?"],
    "de": ["Hey {name}, bist du da?", "{name}, alles klar bei dir?"]
  }
  ```

  A phrasing may contain `{placeholder}` tokens that are filled at pick time.
- **Seed:** `vaf/core/vocab/source/<key>.json` holds the hand-authored canonical phrasings (at least `en`,
  usually also `de`). The generator translates the `en` seed into other languages; seed languages are copied
  verbatim and never machine-translated.
- **Runtime never calls an LLM.** It only reads the generated `data/` JSON.

## File layout

```
vaf/core/vocab/
├── __init__.py          # API: pick(), phrasings(), resolve_user_language(), available_languages()
├── source/
│   └── <key>.json       # hand-authored seed (en) — the translation source
└── data/
    └── <key>.json       # generated: { "en": [...], "de": [...], "es": [...], ... }

scripts/
└── generate_vocab.py    # dev-time: translate the seed into many languages via the configured LLM
```

Current keys: `nudge` (thinking-run "are you there?"), the voice stack's
spoken lines (`voice_greeting`, `voice_greeting_anon`, `voice_tangled`,
`voice_delegate_ack`), the speaker-confirmation texts (`speaker_confirm_*`),
the guided-enrollment script (`speaker_enroll_*`; its `questions` key is
consumed as a FULL ordered list via `phrasings()`, not `pick()`), and
`stopwords` - per-language function-word lists (not phrasings) consumed as
full lists, e.g. by the memory lexical-search query filter; new consumers
should read them from here instead of hardcoding word lists.

## API

```python
from vaf.core import vocab

lang = vocab.resolve_user_language(user_scope_id, username)   # 'de', 'en', ...
text = vocab.pick("nudge", lang, scope=user_scope_id, name="Mert")
```

- **`pick(key, lang, scope=None, **fmt) -> str`** — returns one phrasing for `key` in `lang`, formatted with
  `**fmt`. Picks **rotate** per `(scope, key)` so consecutive calls differ. A missing `{placeholder}` returns
  the raw phrasing instead of raising. Returns `''` only if the key has no phrasings at all.
- **`resolve_user_language(user_scope_id=None, username=None) -> str`** — best-effort 2-letter language:
  `user_identity.preferred_language` (`vaf/auth/user_workspace.py`) → config `default_language` → `"en"`.
- **`available_languages(key) -> list[str]`** — languages a key currently has (diagnostics / the generator).

**Language fallback (in `pick`):** exact base language (`de-DE` → `de`) → `en` → first available. So a user
whose language is not yet in the book still gets a sensible English phrasing.

## Adding a new phrase key (how to expand later)

1. Create the seed `vaf/core/vocab/source/<key>.json` with at least an `en` list (5–6 short variants),
   optionally `de`. Use `{name}` (or other) placeholders as needed.
2. Generate the translations:
   ```bash
   python scripts/generate_vocab.py --key <key>
   ```
   This writes `vaf/core/vocab/data/<key>.json` for the built-in broad language set. A language that fails to
   translate is skipped (never written), so the data file is never corrupted; missing languages fall back to
   English at runtime.
3. Use it: `vocab.pick("<key>", vocab.resolve_user_language(scope, username), scope=scope, **fmt)`.

## Generating / expanding languages

`scripts/generate_vocab.py` translates the `en` seed into many languages via the configured LLM provider:

```bash
python scripts/generate_vocab.py                  # all default languages, keep ones already present
python scripts/generate_vocab.py --force          # re-generate every language (overwrite)
python scripts/generate_vocab.py --langs es,fr,ja  # only these
python scripts/generate_vocab.py --key nudge --provider deepseek
```

The default target set is a broad built-in list (the Web UI `languages.ts` is UI-only and currently minimal).
The script is **dev/build-time** tooling and requires a working LLM provider; the runtime does not depend on
it. Review generated phrasings before shipping — machine translations of casual idioms are not always perfect.

## Current keys

| Key | Used by | Placeholders |
|-----|---------|--------------|
| `nudge` | Thinking-run "are you there?" follow-up nudge (`_send_nudge`, `vaf/core/thinking_mode.py`) | `{name}` |

## Related documentation

- [I18N.md](I18N.md) — Web UI internationalization (next-intl); the frontend counterpart.
- [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md) — how UI strings are resolved per locale.
- [Thinking-Mode.md](../agents/Thinking-Mode.md) — the nudge and the follow-up flow that consume the `nudge` key.
