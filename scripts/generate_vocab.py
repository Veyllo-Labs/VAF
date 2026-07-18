#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Generate / expand the VAF Vocabulary Book (backend canned phrases) into many languages via the
configured LLM. Reads the seed from `vaf/core/vocab/source/<key>.json` (must contain `en`) and writes
the translated phrasings into `vaf/core/vocab/data/<key>.json`.

The RUNTIME never calls an LLM for these phrases - this is dev/build-time tooling. A language that
fails to translate (or returns malformed output) is skipped, never written, so `data/<key>.json` is
never corrupted; missing languages simply fall back to English at runtime.

Usage:
    python scripts/generate_vocab.py                    # all default languages, keep ones already present
    python scripts/generate_vocab.py --force            # re-generate every language (overwrite)
    python scripts/generate_vocab.py --langs es,fr,ja   # only these
    python scripts/generate_vocab.py --key nudge --provider deepseek
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_VOCAB = ROOT / "vaf" / "core" / "vocab"

# A broad default target set (web/lib/languages.ts is UI-only and currently minimal). Override with --langs.
DEFAULT_LANGS = [
    "en", "de", "es", "fr", "it", "pt", "nl", "sv", "da", "no", "fi", "pl", "cs", "sk",
    "ro", "hu", "tr", "ru", "uk", "el", "bg", "hr", "sr", "ar", "he", "fa", "hi", "bn",
    "id", "ms", "vi", "th", "zh", "ja", "ko",
]


def _resolve_provider(explicit):
    if explicit:
        return explicit
    try:
        from vaf.core.config import Config
        return (Config.get("provider") or Config.get("api_provider") or "deepseek")
    except Exception:
        return "deepseek"


# Per-key hint so the LLM knows WHAT it is localizing (a spoken reply reads differently
# from a substring-matched cue lexicon). Unknown keys get a safe generic description.
_KIND_HINTS = {
    "nudge": "short, casual 'are you there?' chat nudges an assistant sends when the user went quiet",
    "awareness_triggers": ("short everyday cue words and sentence-openers people say when they want help, "
                           "ask a question, or mention a task/appointment - keep them as short natural "
                           "fragments (not full sentences), the way they are actually spoken"),
    "addressee_check": ("short phrases someone says to check whether a voice assistant can hear them or is "
                        "listening, e.g. 'can you hear me', 'are you there'"),
    "addressee_clarify": ("very short things a voice assistant says to ask whether it was the one being "
                          "addressed, e.g. 'do you mean me?', 'were you talking to me?', 'me?'"),
    "owner_claim": ("short first-person ways a person states their own name, e.g. 'I am {name}', "
                    "'this is {name}', 'my name is {name}'. ALWAYS include a claim word around the "
                    "placeholder (like 'I am' / 'this is'); NEVER output the bare {name} alone"),
    "reask_pending": ("short spoken lead-ins an assistant says when it RE-ASKS its own question "
                      "because the user did not catch it, e.g. 'Sorry, I asked: {question}', "
                      "'Let me repeat: {question}'. ALWAYS keep the {question} placeholder and wrap "
                      "a short natural lead-in around it; NEVER output the bare {question} alone"),
}


def _llm_translate(provider, phrasings, lang_code, kind="short, natural spoken phrases"):
    from vaf.core.api_backend import APIBackendManager
    mgr = APIBackendManager(provider)
    # Preserve whatever {placeholder} tokens the seed uses (some keys have {name}, most have none).
    placeholders = sorted({m for s in phrasings for m in re.findall(r"\{[a-z_]+\}", str(s))})
    ph_rule = (f"Keep every literal placeholder ({', '.join(placeholders)}) exactly as written in every line. "
               if placeholders else "")
    prompt = (
        f"Localize each of the following {kind} into the language with ISO code '{lang_code}'. "
        f"Keep them short, natural and idiomatic for that language (localize, do NOT translate word-for-word). "
        f"{ph_rule}Return ONLY a JSON array of {len(phrasings)} strings, in the same order, no prose, "
        f"no code fences.\n\n" + json.dumps(phrasings, ensure_ascii=False, indent=2)
    )
    chunks = list(mgr.chat_completion(
        [{"role": "user", "content": prompt}],
        max_tokens=max(900, len(phrasings) * 40), temperature=0.4, stream=False,
    ))
    text = "".join(c for c in chunks if isinstance(c, str))
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)  # the array; trailing control objects use {} not []
    if not m:
        raise ValueError(f"no JSON array in response for '{lang_code}'")
    arr = json.loads(m.group(0))
    out = [str(s).strip() for s in arr
           if str(s).strip() and all(p in str(s) for p in placeholders)]
    if len(out) < max(1, len(phrasings) - 1):
        raise ValueError(f"too few valid phrasings for '{lang_code}' ({len(out)}/{len(phrasings)})")
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate the VAF vocabulary book via the configured LLM.")
    ap.add_argument("--key", default="nudge", help="phrase key (source/<key>.json -> data/<key>.json)")
    ap.add_argument("--langs", default="", help="comma-separated ISO codes (default: a broad built-in set)")
    ap.add_argument("--force", action="store_true", help="re-generate even languages already present")
    ap.add_argument("--provider", default="", help="LLM provider (default: configured provider)")
    args = ap.parse_args()

    source_path = _VOCAB / "source" / f"{args.key}.json"
    data_path = _VOCAB / "data" / f"{args.key}.json"
    if not source_path.is_file():
        print(f"ERROR: seed not found: {source_path}", file=sys.stderr)
        return 2
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if not source.get("en"):
        print("ERROR: seed must contain an 'en' phrasing list", file=sys.stderr)
        return 2
    seed_en = source["en"]

    data = {}
    if data_path.is_file():
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    # Seed languages are canonical - always copy from source (never LLM-translate them).
    for lang, items in source.items():
        if isinstance(items, list) and items:
            data[lang] = [str(s) for s in items]

    targets = [c.strip().lower() for c in args.langs.split(",") if c.strip()] or list(DEFAULT_LANGS)
    provider = _resolve_provider(args.provider)
    kind = _KIND_HINTS.get(args.key, "short, natural spoken phrases")
    print(f"key={args.key} provider={provider} targets={len(targets)} force={args.force}")

    done = skipped = failed = 0
    for lang in targets:
        if lang in source:
            continue  # seeded
        if lang in data and not args.force:
            skipped += 1
            continue
        try:
            data[lang] = _llm_translate(provider, seed_en, lang, kind)
            done += 1
            print(f"  + {lang}: {data[lang][0]}")
        except Exception as e:
            failed += 1
            print(f"  ! {lang}: skipped ({e})", file=sys.stderr)

    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {data_path}  (generated={done} skipped={skipped} failed={failed}, total langs={len(data)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
