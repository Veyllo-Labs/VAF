# Korean (ko-KR) terminology and style

The binding reference for `web/messages/ko.json`. Korean needs its own document
because it disagrees with both CJK neighbours on the rules that matter most, and
because it carries one defect class neither of them has: the postposition after a
placeholder.

Where a rule here is machine-checkable it is a test, not prose:
[`tests/test_i18n_locale_style.py`](../../tests/test_i18n_locale_style.py) enforces the
particles, the spacing, the register, the punctuation, the ellipsis, the banned dashes,
the plural category, and the rule that identifiers survive translation.

## The particle rule

Korean postpositions are chosen by whether the syllable in front of them ends in a
consonant. When that word is an ICU placeholder, the value is unknown at authoring time.

1. **After a placeholder, write the doubled form, consonant part first, closed up:**
   `{name}이(가)`, `{name}을(를)`, `{name}은(는)`, `{name}과(와)`, `{name}(으)로`.
   Never the reversed `를(을)` / `가(이)` / `는(은)` / `와(과)`, never `(이)가`, never the
   slash form `이/가`, which belongs in a textbook and not in a product.
2. **Particles that do not alternate stay bare** after a placeholder: 에, 에서, 의, 에게,
   도, 만, 부터, 까지, 보다, 처럼. A doubled form on any of these is an error.
3. **After a Latin word the particle is RESOLVED, never doubled**, from the Korean reading:
   vowel-final takes 를/가/는/와/로, consonant-final takes 을/이/은/과/으로, and a reading
   ending in ㄹ takes 을/이/은/과 but 로. So `Docker를`, `MCP는`, `URL을`, `Outlook을`,
   `Slack을`, `Telegram을`, `WhatsApp을`, `IMAP을`, `Anthropic을`, `WebSocket을`, but
   `Gmail로`, `Google로`, `AGPL로`, `파일로`. Never `파일으로`, `Gmail으로`, `URL으로`.
4. **After a number plus a counter the particle is also resolved**, because the counter is a
   fixed syllable: `파일 3개를`, `사용자 2명이`, `호출 5회를`, `메일 3통을`, `30일로`.
5. **An identifier never takes a particle directly.** A Korean classifier noun carries it:
   `~/.vaf/config.json 파일에`, `debug_logs_enabled 설정을`, `vaf update --recover 명령을`.
   This keeps the identifier copy-pasteable and removes every judgement call about how a
   snake_case key is pronounced.
6. **A copula after a placeholder cannot be resolved either.** Restructure rather than
   guess: `현재 버전: {version}`, not `{version}이에요`.
7. **Restructure instead of doubling** when a doubled form would land on a button, a tab, a
   column header or a chip, or when one sentence would carry two of them. Move the variable
   into a non-alternating slot (`{0}에 대한 …`), or end with a colon and let it follow.

## Where the wording comes from

- **Microsoft**, and only from sources that are not machine output: the Terminology
  Collection (ko, 54,397 entries), the Korean Localization Style Guide, the shipped VS Code
  ko language pack and the Windows Terminal ko-KR resource files. **Every** localized
  `learn.microsoft.com` ko page checked returned `ms.translationtype: MT`, and not one HT
  page was found, so no Microsoft Korean prose is behind any term here. Reading the termbase
  also has a trap: a `ProperNoun` row is a shipped UI string, a `Verb` row is a dictionary
  lemma (닫다, 저장하다) that is never a label.
- **Korean-native products** for register and product nouns: 토스, 카카오, 네이버,
  당근, 노션 한국어, 슬랙 한국어, and Naver CLOVA Studio.
- **Red Hat, Kubernetes, AWS, Naver Cloud** ko documentation for infrastructure.
- **Outlook ko, Gmail ko, 네이버 메일, 다음 메일** for the mail client.
- **국립국어원 한글 맞춤법 and 문장부호 규정 (2015), W3C klreq** for typography.

## Decisions that were contested

| Term | Rendering | Why, and what lost |
|---|---|---|
| Password | 비밀번호 | Every shipped Microsoft string says 암호, but 비밀번호 is the consumer term and this is a consumer product. A deliberate departure from the fetched evidence. |
| skill | 스킬 | Microsoft ships 기술, which also means technology. Naver CLOVA Studio and Notion Korean ship 스킬. |
| tamper-evident | 변조 탐지 | 변조 방지 claims prevention. The hash chain detects. Same call as the Chinese and Japanese rounds. |
| fail-closed | 실패하면 거부 | 기본으로 거부 would say "deny by default", which is a different security property from "deny when the identity binding fails". |
| Reset | 초기화 | Microsoft's own products ship both 초기화 and 다시 설정, on one page of Windows Terminal at once. 초기화 is what a Korean phone shows. |
| Light mode | 라이트 모드 | The highest-risk term here: every source disagrees, and the Terminology Collection actually ships 밝음 모드, which no fetched product displays. 라이트 모드 mirrors the settled 다크 모드. |
| Action | 동작 | Shipped Microsoft desktop chrome says 작업, but this product shows Task on the same screens and Task is 작업. The users table's action column is 관리, which is the Korean admin-table convention. |
| Enabled / Disabled | 사용 / 사용 안 함 | 활성화 / 비활성화 also ships inside VS Code. The pair must not be mixed: 사용 pairs with 사용 안 함, 활성화 with 비활성화. On/off chips are 켜짐 / 꺼짐. |
| quarantine | 격리 보관 | A coinage, forced because this product also shows isolation, which is 격리. Korean antivirus convention is a bare 격리. |
| working memory | 단기 메모리 | Invented, as the readable contrast to 장기 메모리. 작업 기억 is the psychology term and reads clinical; 작업 메모리 would collide with 작업 = task. |
| room (A2A) | 대화방 | No Korean product puts agents in a thing it calls a 대화방. Being first, as with the Japanese ルーム. |
| Trash and the mail folders | 휴지통 with 받은편지함 / 보낸편지함 / 임시보관함 | The Korean portals (네이버, 다음, 카카오) ship the 메일함 family instead. The 편지함 family was chosen because this client connects Gmail and Outlook. If the users come from the portals, the whole family flips together or the UI looks broken. |
| Soul | `Soul`, left in Latin | It has to stay distinct from persona, which is 성격. |
| latency | 응답 시간 | 대기 시간 (Microsoft) means waiting time; 지연 시간 (VS Code) is a different sense. |
| hardening | 보안 강화 | 하드닝 is the only form verifiable from a fetched page (Kubernetes ko), and is deliberately not the one shipped. |

## Style rules

1. **해요체 everywhere**, in every sentence: ~해요 / ~이에요 / ~할 수 있어요 / ~했어요. Never
   합니다체, never 하십시오체, never 반말. Warmth comes from stripping the honorific layer,
   not from dropping politeness.
2. **Buttons, menu items, tabs, section titles, column headers and chips are bare nouns**
   (저장, 삭제, 완료, 적용) or -기 nominalisations for native stems (닫기, 보내기, 더보기,
   미리 보기, 새로 고침). Sino-Korean roots drop 하다: 저장, never 저장하기.
3. **Never 당신, 귀하, 고객님.** Korean drops the subject; 내 where a possessive is
   unavoidable. No honorific infix ~시~ in the product's own sentences.
4. **해 주세요 is rationed** to what only the user can do. What the app does, it reports:
   ~했어요. Capability is ~할 수 있어요. No 바랍니다, no 죄송합니다 in an error.
5. **띄어쓰기 is part of the term, not a style choice.** 수 is spaced on both sides
   (할 수 없어요). Keep the space in 새로 고침, 미리 보기, 세부 정보, 사용자 이름, 읽기 전용,
   쓰기 권한, 표준 시간대, 사용 안 함, 다크 모드. Write closed: 기본값, 대시보드, 로그아웃,
   온라인, 오프라인, 첨부파일, 받은편지함, 보낸편지함, 임시보관함, 전체답장, 숨은참조, 더보기.
6. **Korean/Latin boundary, two cases only**: a particle closes up (`Docker를`, `API를`); a
   separate noun takes one space (`API 키`, `MCP 서버`, `GitHub 토큰`, `AI 에이전트`).
7. **Punctuation is half-width**: `. , ? ! ( ) :`. Never the full-width set, which belongs to
   Chinese and Japanese. A parenthesis attaches with no leading space. Every explanatory
   sentence, error and toast ends with a period; labels, buttons, tabs, headings, column
   headers, chips and one-line empty states take none.
8. **Quotes are the curly single `' '`** around a UI control name or a value. Never `" "`,
   never 「」, which are vertical-writing marks.
9. **The ellipsis is one U+2026** with no space before it: 불러오는 중….
10. **No dash or tilde**: U+2014, U+2015, U+2013, U+301C, U+FF5E, U+223C. A range uses
    부터/까지 or the ASCII hyphen. The wave characters matter as much as the dash here,
    because Korean IMEs and reference PDFs hand you U+FF5E or U+223C for a range.
11. **Counters**: 개 for files, items, tools, agents, skills, messages and tokens · 건 for
    records, log entries and security events · 통 for emails · 명 for people · 회 for runs
    and retries · 대 for devices · 일/시간/분/초 for durations · 줄 for lines. No space
    before the counter.
12. **One plural category.** `Intl.PluralRules('ko')` is `['other']`, so an ICU `one {...}`
    branch is dead code.
13. **Empty states are one sentence with no period**, forward-looking: 아직 ~이 없어요.
14. **Errors are two beats**: diagnosis then recovery. 다시 시도해 주세요 in prose;
    다시 시도 is the button label only.
15. **Destructive copy**: 되돌릴 수 없어요 when the action stands, 복구할 수 없어요 when the
    data is gone. The affirmative button names the act (삭제, 로그아웃), never 예.
16. **Loanword or Sino-Korean by concept age, not taste**: new AI and infrastructure nouns
    are transliterated (에이전트, 스킬, 프롬프트, 워크플로, 메모리, 샌드박스, 컨테이너), while
    actions and states keep the native or Sino-Korean word (저장, 삭제, 허용, 거부, 차단,
    격리, 탐지, 감사, 복구, 정상). Mixing the two per term is correct.
17. **Numbers and dates**: half-width digits, comma grouping, `25%` and `12,000원` with no
    space, `2026년 8월 28일` with no zero padding, and the all-numeric form keeps its
    trailing period: `2026. 8. 28.`

## Still open

- The Korean reading of **Veyllo** and **VAF** has never been written down. Both plausible
  readings of Veyllo end in a vowel, so the particle table is stable either way, but the
  reading must be fixed before any Korean speech output exists. VAF is assumed to be read
  letter by letter.
- The backend [Vocabulary Book](VOCABULARY_BOOK.md) has no Korean at all, so the phrases the
  agent speaks itself fall back to English.

## Related

- [I18N.md](I18N.md) - how locales and keys are added.
- [I18N_JA_GLOSSARY.md](I18N_JA_GLOSSARY.md) and [I18N_ZH_GLOSSARY.md](I18N_ZH_GLOSSARY.md) -
  the two CJK companions, which disagree with this one on spacing and punctuation.
- [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md) - the technical specification.
