# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Style invariants a translated catalogue cannot be checked for by eye.

`test_i18n_catalog_parity.py` guards the SHAPE of the message catalogues: same
keys, same ICU arguments. It says nothing about what is inside a value, and the
two failure modes that matter most in a translation are both invisible to it.

The first is an identifier that got translated. A CLI command, a config key or a
file path inside a UI string is something the reader has to type back verbatim;
translating `debug_logs_enabled` or `vaf update --recover` turns working help
text into an instruction that cannot be followed. Nothing catches that in review
either, because the sentence around it still reads correctly.

The second is CJK typography, and it is not one rule set but one per language,
because on the rules that matter most the three disagree with each other. zh-CN
wants an ASCII space at every Chinese/Latin boundary; ja-JP forbids exactly that
space; ko-KR is a spaced script where the answer depends on what follows, a
particle closing up and a noun taking a space. zh and ja want full-width
punctuation, ko wants half-width. What all three share is the dash ban, the
single U+2026 ellipsis, and a single CLDR plural category, so an ICU `one {...}`
branch is dead code in every one of them.

Korean also carries a defect class the other two cannot have. Its postpositions
are selected by the final sound of the word in front of them, so a particle after
an ICU placeholder cannot be resolved at authoring time: it has to ship as the
doubled form, consonant part first, and the reversed order or a bare guess is
simply wrong for half the runtime values. After a Latin word or a counter the
same particle must be RESOLVED instead, from the Korean reading. None of this is
visible to a reviewer reading for meaning, and none of it is visible to a
reviewer who does not read Korean at all.

The locales listed here are the ones authored against those rules. `de.json` and
`en.json` predate them and are deliberately not included: they carry em dashes
that a sweep has not reached yet, so adding them would assert a state that does
not exist.
"""
import json
import re
from pathlib import Path

import pytest

_MESSAGES = Path(__file__).resolve().parents[1] / "web" / "messages"
_REFERENCE = "en"

# Locales authored under the rules below. A new locale joins this list in the
# same change that adds its catalogue.
_STYLED_LOCALES = ["tr", "zh", "ja", "ko"]

_CJK = r"[一-鿿]"
# Japanese runs include kana, which Chinese never does.
_JA = r"[぀-ヿ㐀-鿿]"
# Hangul syllables. Korean shares none of its letterforms with the other two.
_KO = r"[가-힣]"


def _load(locale):
    return json.loads((_MESSAGES / f"{locale}.json").read_bytes())


def _flatten(node, prefix=""):
    flat = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                flat[f"{path}[{i}]"] = item
        else:
            flat[path] = value
    return flat


# --------------------------------------------------------------------------
# Identifiers the reader has to type back, so they may never be translated.
# --------------------------------------------------------------------------

# A path or a path-like fragment: ~/.vaf/config.json, /api/email/oauth/callback,
# logs/usage_*.log. Two segments minimum, so a bare word never qualifies.
_PATH = re.compile(r"~?/[A-Za-z0-9_.*-]+(?:/[A-Za-z0-9_.*-]+)+")
# snake_case with at least one underscore: debug_logs_enabled, gate_bypassed.
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
# Literals with no shape a regex can generalise.
_LITERALS = (
    "vaf run",
    "vaf a2a export",
    "vaf update --recover",
    "vaf secure rotate-db",
    "trust.json",
    "SKILL.md",
    "SHA-256",
    "GENESIS",
    "mcp_<server>_<tool>",
    "vaf_live_",
    "ghp_",
    "imap.example.com",
    "smtp.example.com",
    "deepseek-chat",
    "github.com/settings/tokens",
)


def _identifiers(text):
    if not isinstance(text, str):
        return set()
    # A sentence-final period sits inside the path character class, so strip the
    # punctuation a path never really ends in before comparing.
    paths = {match.rstrip(".,;:!?)") for match in _PATH.findall(text)}
    found = paths | set(_SNAKE.findall(text))
    found |= {lit for lit in _LITERALS if lit in text}
    return {ident for ident in found if ident}


@pytest.mark.parametrize("locale", _STYLED_LOCALES)
def test_identifiers_survive_translation(locale):
    reference = _flatten(_load(_REFERENCE))
    other = _flatten(_load(locale))
    lost = []
    for path, text in reference.items():
        translated = other.get(path)
        if not isinstance(translated, str):
            continue
        for ident in sorted(_identifiers(text)):
            if ident not in translated:
                lost.append(f"{path}: {ident!r} missing from {locale}.json")
    assert not lost, "translated identifier(s) the user has to type back:\n" + "\n".join(lost[:10])


# --------------------------------------------------------------------------
# The dash, banned in every form these catalogues could reach for. The wave
# dash pair is here because U+301C and U+FF5E look identical, are the Japanese
# habit for a numeric range, and silently break search and diffs against each
# other; a range is written with から instead.
# --------------------------------------------------------------------------

_DASHES = {"\u2014": "em dash", "\u2015": "horizontal bar", "\u2013": "en dash",
           "\u301c": "wave dash", "\uff5e": "fullwidth tilde", "\u223c": "tilde operator"}


@pytest.mark.parametrize("locale", _STYLED_LOCALES)
def test_no_dash_in_any_form(locale):
    hits = []
    for path, text in _flatten(_load(locale)).items():
        if not isinstance(text, str):
            continue
        for char, name in _DASHES.items():
            if char in text:
                hits.append(f"{path}: {name} (U+{ord(char):04X})")
    assert not hits, f"{locale}.json uses a banned dash:\n" + "\n".join(hits[:10])


# --------------------------------------------------------------------------
# Chinese typography.
# --------------------------------------------------------------------------

_HALFWIDTH_NEXT_TO_CJK = re.compile(rf"(?:{_CJK}[,;:!?]|[,;!?]\s*{_CJK})")
_BAD_ELLIPSIS = re.compile(r"\.\.\.|。。。|……")
# A Chinese run touching Latin/digits with no separating space.
_MISSING_SPACE = re.compile(rf"(?:{_CJK}[A-Za-z0-9]|[A-Za-z0-9]{_CJK})")
# A space that should not be there: full-width punctuation carries its own sidebearing.
_SPACE_AT_FULLWIDTH = re.compile(
    r"[ ]+[，。、；：？！）】》”]|[（【《“][ ]+|[，。、；：？！][ ]+(?![A-Za-z0-9$~/])"
)
# CLDR gives zh only "other"; an "one {" branch can never be selected.
_ICU_ONE_BRANCH = re.compile(r"plural[^}]*\bone\s*\{")

# Japanese: the space at a Latin boundary is the defect, not its absence, and a
# placeholder counts as a boundary because ICU fills it with a word.
_JA_STRAY_SPACE = re.compile(rf"(?:{_JA}[ ]+[A-Za-z0-9{{]|[A-Za-z0-9}}][ ]+{_JA})")
_HALFWIDTH_KATAKANA = re.compile(r"[｡-ﾟ]")
# Loanwords whose English ends in -er/-or/-ar keep the 長音符. Dropping it is the
# pre-2008 style and the loudest sign that a Japanese catalogue was not proofed.
_DROPPED_LONG_VOWEL = re.compile(
    r"(ユーザ|サーバ|コンピュータ|フォルダ|ブラウザ|プロバイダ|エディタ|フィルタ|パラメータ"
    r"|コンテナ|スピーカ|コネクタ)(?!ー)"
)
_WESTERN_QUOTES = re.compile(r"[“”]")
_KANJI_THAT_STAY_KANA = re.compile(r"全て|下さい|ヶ月")
_JA_HALFWIDTH_SENTENCE_PUNCT = re.compile(rf"{_JA}[,\.](?![0-9A-Za-z])")

# Korean postpositions are chosen by the final sound of the word in front of them,
# which is unknowable when that word is an ICU placeholder. The doubled form is the
# shipped answer, consonant part first; the reversed order and the slash form are
# both wrong, and a bare particle after a placeholder cannot be right for every value.
_KO_DOUBLED = r"(?:을\(를\)|이\(가\)|은\(는\)|과\(와\)|\(으\)로)"
_KO_REVERSED_PARTICLE = re.compile(r"를\(을\)|가\(이\)|는\(은\)|와\(과\)|\(이\)가|\(으로\)로|로\(으로\)")
_KO_BARE_PARTICLE = re.compile(r"\}(?!" + _KO_DOUBLED + r")(?:을|를|이|가|은|는|과|와|으로|로)(?![가-힣(])")
_KO_COPULA_AFTER_PLACEHOLDER = re.compile(r"\}(?:이에요|예요|이었|였|이라|라는)")
_KO_SPACED_PARTICLE = re.compile(r"\}\s+(?:을|를|이|가|은|는|과|와|으로|로)(?![가-힣])")
# A word whose reading ends in a vowel or in ㄹ takes 로, never 으로.
_KO_WRONG_EURO = re.compile(r"(?:파일|줄|일|Gmail|Google|URL|AGPL|이메일)으로")
# 수 is a dependent noun and is spaced on both sides.
_KO_MISSING_SPACE = re.compile(r"[할볼쓸읽갈올]수\s*(?:있|없)")
# Korean UI punctuation is half-width; the full-width set belongs to zh and ja.
_KO_FULLWIDTH = re.compile(r"[。、？！，．：（）「」『』]")
_KO_SPACE_BEFORE_ELLIPSIS = re.compile(r"[가-힣]\s+…")
_KO_HONORIFIC = re.compile(r"하십시오|시기 바랍니다|죄송합니다")

_LOCALE_RULES = {
    "zh": (
        ("informal address", re.compile("您")),
        ("half-width punctuation beside a Chinese character", _HALFWIDTH_NEXT_TO_CJK),
        ("ellipsis that is not a single U+2026", _BAD_ELLIPSIS),
        ("missing space at a Chinese/Latin boundary", _MISSING_SPACE),
        ("space touching full-width punctuation", _SPACE_AT_FULLWIDTH),
        ("ICU plural 'one' branch (zh has only 'other')", _ICU_ONE_BRANCH),
    ),
    "ja": (
        ("second person written out", re.compile("あなた")),
        ("space at a Japanese/Latin boundary", _JA_STRAY_SPACE),
        ("half-width katakana", _HALFWIDTH_KATAKANA),
        ("long vowel mark dropped from a loanword that keeps it", _DROPPED_LONG_VOWEL),
        ("Western quotation marks instead of the corner brackets", _WESTERN_QUOTES),
        ("kanji where the kana form is the convention", _KANJI_THAT_STAY_KANA),
        ("ellipsis that is not a single U+2026", _BAD_ELLIPSIS),
        ("half-width comma or period as sentence punctuation", _JA_HALFWIDTH_SENTENCE_PUNCT),
        ("ICU plural 'one' branch (ja has only 'other')", _ICU_ONE_BRANCH),
    ),
    "ko": (
        ("second person written out", re.compile("당신|귀하|고객님")),
        ("reversed or slashed doubled particle", _KO_REVERSED_PARTICLE),
        ("unresolvable particle straight after a placeholder", _KO_BARE_PARTICLE),
        ("copula straight after a placeholder", _KO_COPULA_AFTER_PLACEHOLDER),
        ("space between a placeholder and its particle", _KO_SPACED_PARTICLE),
        ("으로 after a reading that ends in a vowel or in ㄹ", _KO_WRONG_EURO),
        ("missing space before the dependent noun 수", _KO_MISSING_SPACE),
        ("full-width punctuation", _KO_FULLWIDTH),
        ("space before the ellipsis", _KO_SPACE_BEFORE_ELLIPSIS),
        ("honorific register above 해요체", _KO_HONORIFIC),
        ("ellipsis that is not a single U+2026", _BAD_ELLIPSIS),
        ("ICU plural 'one' branch (ko has only 'other')", _ICU_ONE_BRANCH),
    ),
}

_SCRIPTS = {"zh": _CJK, "ja": _JA, "ko": _KO}

_RULE_CASES = [(loc, label, pattern) for loc, rules in _LOCALE_RULES.items() for label, pattern in rules]

# Traditional-only forms that a converted or hand-typed string leaks. Every
# character here has a distinct simplified counterpart, so its mere presence is
# the defect; characters that are valid in both scripts must never be added.
_TRADITIONAL = "帳網資訊預設檔體們來對開關這樣說話點擊選擇華國學實記憶發佈調個"


@pytest.mark.parametrize(
    "locale,label,pattern", _RULE_CASES, ids=[f"{loc}: {label}" for loc, label, _ in _RULE_CASES]
)
def test_cjk_typography(locale, label, pattern):
    script = _SCRIPTS[locale]
    offenders = []
    for path, text in _flatten(_load(locale)).items():
        if not isinstance(text, str) or not re.search(script, text):
            continue
        match = pattern.search(text)
        if match:
            offenders.append(f"{path}: {ascii(match.group(0))} in {ascii(text[:60])}")
    assert not offenders, f"{locale}.json, {label}:\n" + "\n".join(offenders[:10])


# --------------------------------------------------------------------------
# Punctuation the component supplies, which no catalogue rule can reach.
# --------------------------------------------------------------------------

_WEB = Path(__file__).resolve().parents[1] / "web"
# A translation call whose result is immediately followed by a hardcoded ASCII
# colon: `${tMain('roomInfoLastSeen')}: ${value}` or `{tLocalNet('port')}: {v}`.
_TRANSLATION_CALL = re.compile(r"\bt[A-Za-z]*\(\s*['\"]")
_HARDCODED_SEPARATOR = re.compile(r"\)\}\s*:\s")
# A hard space in front of a translated word: `{count} {t('unit')}`. Chinese wants
# that gap and Japanese forbids it, so it belongs to common.unitSeparator, not to JSX.
_HARDCODED_GAP = re.compile(r"\}\s+\$?\{\s*t[A-Za-z]*\(")


def test_a_label_separator_is_never_hardcoded_beside_a_translated_string():
    """A colon glued to a translated label is a colon the locale cannot change.

    zh-CN needs the full-width `：` where German and English need `: `, and a
    component that writes the ASCII one itself renders `最近出现: 2026/8/27` in
    every Chinese string it touches. The catalogue guards above cannot see it,
    because the defect is in the JSX, not in the value. Six sites did this
    before `common.labelSeparator` existed; the separator now comes from the
    catalogue like any other string.
    """
    offenders = []
    for path in sorted(_WEB.glob("**/*.tsx")):
        if "node_modules" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _TRANSLATION_CALL.search(line) and _HARDCODED_SEPARATOR.search(line):
                offenders.append(f"{path.relative_to(_WEB.parent)}:{number}: labelSeparator")
    assert not offenders, (
        "hardcoded separator after a translated string; use t('common.labelSeparator'):\n"
        + "\n".join(offenders[:10])
    )


# The same shape one step earlier: a hard space in FRONT of a translated word, as in
# `{count} {t('unit')}`. Chinese wants that gap and Japanese forbids it, so it belongs
# to common.unitSeparator. A ratchet rather than a clean sheet: the three sites the
# Japanese review found are fixed, these eighteen are older and are not touched here.
_GAP_DEBT = {
    "web/components/NotificationsModal.tsx": 6,
    "web/app/page.tsx": 5,
    "web/components/connections/GitHubSetupWizard.tsx": 3,
    "web/components/connections/MailAccounts.tsx": 3,
    "web/components/settings/McpServerEditor.tsx": 1,
}


def test_a_hardcoded_gap_before_a_translated_word_never_spreads():
    counts = {}
    for path in sorted(_WEB.glob("**/*.tsx")):
        if "node_modules" in path.parts:
            continue
        hits = len(_HARDCODED_GAP.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[str(path.relative_to(_WEB.parent))] = hits
    new_files = sorted(set(counts) - set(_GAP_DEBT))
    assert not new_files, (
        "a hard space in front of a translated word; use common.unitSeparator so the gap\n"
        "follows the language:\n" + "\n".join(new_files)
    )
    grew = [f"{f}: {n} (was {_GAP_DEBT[f]})" for f, n in counts.items() if n > _GAP_DEBT[f]]
    assert not grew, "known hardcoded-gap debt grew:\n" + "\n".join(grew)
    shrunk = [f"{f}: {_GAP_DEBT[f]} -> {counts.get(f, 0)}" for f in _GAP_DEBT if counts.get(f, 0) < _GAP_DEBT[f]]
    assert not shrunk, "debt was paid down; lower the numbers in _GAP_DEBT:\n" + "\n".join(shrunk)


# A BCP-47 tag written into a formatting call, so every locale gets one language's
# dates and digit grouping. German thousands separators reach an English screen as
# well, but Japanese is where it turns into a wrong number: 1.234 reads as a decimal.
_HARDCODED_LOCALE = re.compile(r"toLocale(?:String|DateString|TimeString)\(\s*['\"][a-z]{2}(?:-[A-Za-z]{2,4})?['\"]")
# The en dash used as an empty-value placeholder. The dash ban covers the message
# catalogues; a component can smuggle one back in as a literal.
_EN_DASH_LITERAL = re.compile(r"['\"]\u2013['\"]")


# ICU treats the ASCII apostrophe as its escape character, so `'{name}'` makes the
# placeholder literal text and the value never substitutes. It renders as the raw
# token, which no placeholder-parity check can see, because the token is still
# there. Every catalogue is checked, not only the translated ones: this shipped in
# the German master and English and Turkish inherited it.
_ICU_ESCAPING_APOSTROPHE = re.compile(r"'\{|\}'")


@pytest.mark.parametrize("locale", sorted(p.stem for p in _MESSAGES.glob("*.json")))
def test_an_apostrophe_never_quotes_a_placeholder(locale):
    offenders = [
        path
        for path, text in _flatten(_load(locale)).items()
        if isinstance(text, str) and _ICU_ESCAPING_APOSTROPHE.search(text)
    ]
    assert not offenders, (
        f"{locale}.json: an ASCII apostrophe beside a placeholder makes ICU treat it as\n"
        "literal text, so the value never substitutes. Use typographic quotes:\n"
        + "\n".join(offenders[:10])
    )


def test_no_en_dash_literal_in_a_component():
    """The catalogues are dash-free; a component can smuggle one back as a literal."""
    offenders = []
    for path in sorted(_WEB.glob("**/*.tsx")):
        if "node_modules" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _EN_DASH_LITERAL.search(line):
                offenders.append(f"{path.relative_to(_WEB.parent)}:{number}")
    assert not offenders, "en dash as a literal UI value; use a plain hyphen:\n" + "\n".join(offenders[:10])


# A ratchet, not a clean sheet. These two files hardcode en-US and en-GB in date and
# number formatting, which has always given every non-English locale the wrong format;
# the Japanese round is where it became a wrong VALUE, because 1.234 reads as a decimal
# rather than as 1234. Those 22 sites are older than this change and are not fixed here.
# The count may only go down, and a file not listed may not have any at all.
_HARDCODED_LOCALE_DEBT = {
    "web/app/page.tsx": 13,
    "web/components/SubAgentWindow.tsx": 9,
}


def test_a_hardcoded_locale_never_spreads():
    counts = {}
    for path in sorted(_WEB.glob("**/*.tsx")):
        if "node_modules" in path.parts:
            continue
        hits = len(_HARDCODED_LOCALE.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[str(path.relative_to(_WEB.parent))] = hits
    new_files = sorted(set(counts) - set(_HARDCODED_LOCALE_DEBT))
    assert not new_files, (
        "a formatting call hardcodes a locale tag; pass the active locale instead:\n"
        + "\n".join(new_files)
    )
    grew = [f"{f}: {n} (was {_HARDCODED_LOCALE_DEBT[f]})" for f, n in counts.items() if n > _HARDCODED_LOCALE_DEBT[f]]
    assert not grew, "known hardcoded-locale debt grew:\n" + "\n".join(grew)
    shrunk = [f"{f}: {_HARDCODED_LOCALE_DEBT[f]} -> {counts.get(f, 0)}" for f in _HARDCODED_LOCALE_DEBT if counts.get(f, 0) < _HARDCODED_LOCALE_DEBT[f]]
    assert not shrunk, "debt was paid down; lower the numbers in _HARDCODED_LOCALE_DEBT:\n" + "\n".join(shrunk)


# Simplified-only forms. Japanese kept the traditional shapes of all of these, so
# one appearing in ja.json means a Chinese string leaked across.
_SIMPLIFIED_ONLY = "记忆说门车东马见认识设备时间网络页级别务现产权护习开关闭题项类样传输错误连线组织统计划"


def test_no_traditional_forms_in_chinese():
    offenders = []
    for path, text in _flatten(_load("zh")).items():
        if not isinstance(text, str):
            continue
        leaked = sorted(set(text) & set(_TRADITIONAL))
        if leaked:
            offenders.append(f"{path}: {ascii(''.join(leaked))} in {ascii(text[:60])}")
    assert not offenders, "zh.json carries Traditional forms:\n" + "\n".join(offenders[:10])


def test_no_simplified_forms_in_japanese():
    offenders = []
    for path, text in _flatten(_load("ja")).items():
        if not isinstance(text, str):
            continue
        leaked = sorted(set(text) & set(_SIMPLIFIED_ONLY))
        if leaked:
            offenders.append(f"{path}: {ascii(''.join(leaked))} in {ascii(text[:60])}")
    assert not offenders, "ja.json carries Simplified-Chinese forms:\n" + "\n".join(offenders[:10])
