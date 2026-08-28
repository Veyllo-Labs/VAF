# Thai (th-TH) terminology and style

The binding reference for `web/messages/th.json`. Thai needs its own document
because it inverts the rule the other locales in this repo are built on: Thai is
written without spaces between words, so a space here is punctuation rather than
a word boundary, and every mechanical rule below follows from that one fact.

Where a rule here is machine-checkable it is a test, not prose:
[`tests/test_i18n_locale_style.py`](../../tests/test_i18n_locale_style.py) enforces the
spacing at every script boundary, the register, the punctuation, the ellipsis, the banned
dashes and invisible characters, the orthography of SARA AM, the plural category, and the
rule that identifiers survive translation.

## Read this before trusting any term here

Thai is a materially thinner localization market than the CJK languages this repo already
ships, and the evidence base reflects that.

- `learn.microsoft.com` does **not** localize Thai article bodies at all. A `th-th` URL
  serves the English text inside a Thai site shell, and because no translation happened,
  the page carries no `ms.translationtype` field. Here the **absence** of the machine
  translation tag is bad news rather than good.
- Every `support.microsoft.com` Thai page checked was machine translated.
- There is **no Thai language pack for VS Code**; the request was closed as not planned.
- Gmail and Google Account Thai help pages carry Google's own AI translation disclaimer,
  and OpenAI's Thai help centre states outright that it is machine translated.

What is left as usable evidence is narrow but solid: the Microsoft Terminology Collection
(34,515 Thai entries), the Microsoft Thai Localization Style Guide, and shipped product
resource files, measured directly. The counts quoted below come from AOSP Settings (5,446
Thai strings), Chromium (10,596), Firefox (17,481) and Signal Android (4,914).

## The spacing rule

1. **Between Thai words there is no space.** Never insert one for readability and never
   split a compound (`โซนเวลา`, `บัญชีผู้ใช้`, `ความจำระยะยาว`). A space inside a Thai
   compound reads as a clause break, which makes it the loudest possible defect.
2. **Exactly one U+0020 on each side of embedded Latin text, of a numeral, and of a
   placeholder that will hold either.** Not zero, not two, never a non-breaking space.
   This is the most consistent finding of the whole round: across 62,592 measured vendor
   strings the glue rate at a Thai/Latin boundary is about 0.1%, and every non-zero case
   traces to a product name or to markup, never to prose. So `คีย์ API`, `เซิร์ฟเวอร์ SMTP`,
   `ในอีก 7 วัน`, `ขนาดเล็กกว่า 20 MB`.
3. **A placeholder is spaced according to the value it will hold.** A data placeholder
   becomes a Latin word or a digit at runtime and takes its space; Chromium's own split is
   decisive at 1,271 spaced data placeholders against 317 glued markup placeholders. A
   markup wrapper takes none, because nothing visible replaces it. **A placeholder filled
   with a translated Thai word is the one case the rule cannot cover**, because a space
   there would sit inside a Thai noun phrase. Every call site was checked: exactly two
   placeholders in this product are filled from another translated string, and both are
   positioned where a space is correct for an unrelated reason, one inside a quotation pair
   and one after a colon. Keep it that way rather than adding an exception to the guard: if
   a new string needs a translated value in mid-phrase, quote the value or restructure the
   sentence so the placeholder lands at a boundary that takes a space anyway.
4. **Word order is NOUN + NUMBER + CLASSIFIER** (`ไฟล์ 3 รายการ`, `ผู้ใช้ 2 คน`). The
   classifier introduces no space of its own; the spaces come from the numeral rule and
   nothing else. With a demonstrative instead of a numeral there is no numeral and the
   phrase is glued: `อุปกรณ์เครื่องนี้`.
5. **Parentheses and quotation marks take one space outside and none inside.** Compliance
   is 100% across all three vendor corpora, and it is the one mechanic where the Royal
   Society and Microsoft agree exactly.
6. **The colon closes up on its label and opens a space after it.** Measured: Chromium 187
   glued against 0 spaced, Firefox 775 against 17.
7. **The comma is replaced by a space in Thai running text.** The Style Guide is explicit,
   and the shipped rendering of "Update, Add, or Remove Data" is
   `การปรับปรุง การเพิ่ม หรือการลบข้อมูล`. The only commas that survive are the thousands
   separator inside a number, a series of numerals, and a list of Latin proper names.
8. **U+200B is forbidden**, and so are U+FEFF, U+2060 and U+00AD. Line breaking is the
   renderer's job: UAX #14 puts Thai in class SA, resolved by dictionary lookup, and
   Chromium, Firefox and Safari all ship the ICU Thai dictionary, applied by script with no
   `lang` attribute required. Chromium's own 10,596 Thai strings contain zero of them.
   Never insert a space to force or prevent a line break either, because in Thai that space
   is punctuation and it changes the sentence.

## Where the wording comes from

- **Microsoft**, and only from sources that are not machine output: the Terminology
  Collection and the Thai Localization Style Guide. No Microsoft Thai prose stands behind
  any term here, for the reasons above.
- **Shipped resource files** for register and frequency: AOSP Settings, Chromium, Firefox
  and Signal Android, all read directly rather than through a documentation site.
- **Google, Apple and Thai-native products** for consumer nouns, with the caveat that
  Google's Thai help pages are AI translated and Apple's style guide is not.
- **The Royal Society of Thailand** for typography, where it is followed, and where it is
  deliberately not followed the departure is named below.

## Decisions that were contested

| Term | Rendering | Why, and what lost |
|---|---|---|
| Dark mode | `โหมดมืด` on the settings toggle, bare `มืด` in the onboarding theme picker | The bare adjective is only correct directly under a `ธีม` heading, which is how every shipped Thai theme picker presents it. The settings toggle has no such heading, so it needs the `โหมด`, which no vendor ships. The two surfaces genuinely differ and the split is deliberate. |
| Logs | `บันทึกการทำงาน` | `บันทึก` alone is the standard word for a log, but it is also the Save button on the same screens. In a navigation item a Thai reader parses it as Save. |
| Promotions | `โปรโมชั่น` | Google ships `โปรโมชัน`, but that page carries an AI translation disclaimer, while Apple's non-machine guide spells it with mai-ek. The better-evidenced source wins over the provider the user actually connects to. |
| Inbox | `กล่องจดหมาย` | Google's form. Apple ships `กล่องเข้า` and Microsoft three further variants. `All inboxes` is attested only in Apple's vocabulary, so the Google-side `กล่องจดหมายทั้งหมด` here is shipped by nobody. If any of this family moves later, all of it has to move together or the mail client looks half translated. |
| agent | `เอเจนต์` | The AI sense is attested by both Microsoft and OpenThai, but Thai banks and Thai tech media keep `AI Agent` in Latin in their own writing. The Thai form reads better to a consumer, the Latin form to a professional. |
| fail-closed | `ปฏิเสธเมื่อล้มเหลว` | "Deny by default" would be a different security property from "deny when the check fails". The same call as the Japanese and Korean rounds, where that exact error shipped once. |
| tamper-evident | `ตรวจพบการแก้ไขได้` | The hash chain detects a change; it does not prevent one, so a word claiming prevention would be false. |
| Conversation, and a mail thread | `การสนทนา` for both | `บทสนทนา` is the transcript of a dialogue rather than the thing itself, and `กระทู้` is a web-board topic, which is what a Thai reader sees in a mail client. Both had leaked in and both are now one word. |
| Action | `การดำเนินการ` | `การกระทำ` is the deed in a moral or legal sense. |
| Usage | `การใช้งาน` | The bare `การใช้` is the termbase headword, not what ships. |
| Reset | `รีเซ็ต` | `คืนค่าเดิม` describes the outcome, not the control. |
| quarantine, isolation | `การกักกัน`, `การแยก` | The product shows both concepts on one screen, so they must not collapse into one word. |
| Search | `ค้นหา` | The Style Guide demotes it to `หา` in prose, but `ค้นหา` is the shipped chrome term and this is chrome. |
| 2FA | `การตรวจสอบสองชั้น`, spelled out | The research listed `2FA` among the acronyms to keep, alongside IMAP and OAuth, and a review will raise it again on that basis. It is translated here because it is a user-facing concept rather than a protocol name, and because every other non-Latin catalogue in this repo already spells it out: `两步验证`, `2段階認証`, `2단계 인증`. Only the Latin-script locales keep the acronym. Used for all 16 occurrences, with no bare `2FA` left in the file. |
| Sandbox, Soul, AI | left in Latin | `Sandbox` ships untranslated in five separate Microsoft entries, including inside Thai compounds. `Soul` stays Latin so it remains distinct from persona, which is `ลักษณะตัวตน`. `เอไอ` is journalism and never UI. |

Six security terms are **coinages with no Thai attestation anywhere**: hardening
(`การเพิ่มความปลอดภัย`), tamper-evident (`ตรวจพบการแก้ไขได้`), hash chain (`เชนแฮช`),
fail-closed (`ปฏิเสธเมื่อล้มเหลว`), context window (`หน้าต่างบริบท`) and embedding
(`การฝัง`). Each is built from attested parts and each is defensible, but Thai security
writing keeps several of them in English. This is a real fork: either accept the coinages
as a set or keep the set in Latin, and do not do half of each.

Six classifiers have **no vendor attestation either**. Across 62,592 shipped strings there
is not one counted Thai occurrence of a record, an event, a task, an email, a token or a
folder. The `รายการ` default used for all six reasons by analogy with the attested
`ผลลัพธ์ N รายการ` pattern rather than from observation. `ฉบับ` for an email was rejected
as an invention: it occurs once in the whole corpus, in the sense of "report".

## Style rules

1. **No sentence-final period.** The Style Guide is explicit that the period marks an
   abbreviation only. Measured: 94% of Firefox's 17,481 Thai values carry no terminal
   punctuation at all, and only 179 of Chromium's end in a period.
2. **No question mark and no exclamation mark.** A Thai question is already marked
   lexically by `ไหม` or `หรือไม่`, which makes the mark redundant. Google ships zero
   question marks in 10,356 strings; Mozilla ships 240, so both are correct Thai and this
   is a pick, made so the rule stays mechanically identical to the no-period rule.
3. **The ellipsis is one U+2026, glued** to the word before it: `กำลังโหลด…`. Both glyphs
   ship in the wild; the single character keeps this catalogue consistent with the Chinese,
   Japanese and Korean ones.
4. **Quotes are the curly `“ ”`**, one space outside and none inside. Never the straight
   ASCII pair, never the corner brackets, which belong to the Japanese catalogue.
5. **MAIYAMOK `ๆ` is glued** to the word it repeats: `อื่นๆ`. This follows Google (371 of
   371 glued in Chromium), the Microsoft termbase (21 glued to 1 spaced) and the Style
   Guide's own example. It knowingly departs from Royal Society rule 1.2.15.1 and from the
   Bank of Thailand's usage, both of which space it. A Thai reviewer restoring the spaces
   is citing a national standard rather than importing an error, which is exactly why the
   decision is pinned in the guard: so the review cannot thrash.
6. **Arabic numerals always.** CLDR's default numbering system for Thai is `latn`, and the
   Thai digits `๐-๙` appear zero times across Chromium, Firefox and the Terminology
   Collection. Comma groups the thousands and the period is the decimal point.
7. **SARA AM is the single U+0E33**, never the visually identical U+0E4D followed by
   U+0E32. The two never unify under NFC or NFD, so search, deduplication and string
   equality all fail silently on the decomposed form.
8. **Buttons take the bare verb; labels take the `การ` prefix.** `บันทึก`, `ลบ`, `ปิด`,
   `คัดลอก` on a control, against `การตั้งค่า`, `การแจ้งเตือน`, `การดำเนินการ` as a
   heading. `การ` on a button makes it read as a noun; dropping it from a heading makes it
   read as a command.
9. **Progressive states take `กำลัง` and keep the ellipsis** (`กำลังส่ง…`), completed states
   take the perfective `แล้ว` (`คัดลอกแล้ว`) because Thai has no participles, and ongoing
   states take `อยู่` (`ใช้งานอยู่`).
10. **Failure uses the fixed stems.** "Cannot", "Could not" and "Failed to" all become
    `ไม่สามารถ…ได้`, where the closing `ได้` is not optional; "Cannot find" becomes
    `ไม่พบ…`. Avoid the English passive, which the Style Guide says lowers Thai readability.
11. **One plural category.** `Intl.PluralRules('th')` returns `['other']` at 0, 1, 2, 100
    and 1.5 alike, so an ICU `one {...}` branch is dead code.
12. **No dash and no wave**: U+2014, U+2015, U+2013, U+301C, U+FF5E, U+223C. A range uses
    `ตั้งแต่…ถึง` or the plain ASCII hyphen. This matters more than usual here because the
    Style Guide notes that Thai does not distinguish the hyphen from the en and em dash.
13. **The affirmative button in a confirmation names the act** (`ลบ`, `ลงชื่อออก`,
    `ยืนยัน`), never `ใช่`.

## Register

1. **No gendered particle, ever.** `ครับ`, `ค่ะ`, `คะ` and sentence-final `นะ` are absent
   from every value. This is measured uniform vendor practice rather than taste: zero
   occurrences across all four shipped resource files, and zero on the Thai pages of SCB,
   LINE Help and the Google privacy policy. The ban is principled as well as conventional,
   because the particles do ship in Thai brand copy, but only where a **human** speaks for
   the brand. Kasikornbank's own developer replies use `ค่ะ` 24 times, signed by the bank.
   The Thai rule is a speaker rule: a support agent has a speaker and takes the particle, a
   system surface has none. The brand service voice also defaults female, which is why a
   genderless assistant cannot borrow it without silently picking a gender.
2. **Politeness is structural, not terminal.** Four devices carry it, in descending
   frequency: the softened imperative `หากต้องการ X ให้ Y` (90 occurrences in AOSP, 195 in
   Chromium, and the main politeness carrier in Thai UI); `โปรด` plus verb for a genuine
   request; the agentless `ระบบจะ…`, so the product states a consequence instead of issuing
   a command; and the bare verb with no marker, which is what a short button gets.
3. **`โปรด`, never `กรุณา`.** Measured: AOSP 69 against 0, Chromium 217 against 1, Firefox
   5 against 0. Ration it to what only the user can do, and omit it where the sentence
   already carries `ให้` or a bare verb.
4. **Address the owner as `คุณ`, written out.** The shipped corpora are emphatic: AOSP 525,
   Chromium 1,600, Firefox 90, Signal 1,466. Write it in any sentence about the owner's own
   data, permissions or consequences. Drop the subject only on short labels and where the
   subject is the product itself. Possessives postpose: `ของคุณ`.
5. **Never `ท่าน`.** It appears zero times as a pronoun in all four shipped resource files;
   every raw hit is the substring inside `เท่านั้น`. It appears immediately in Thai
   institutional copy, where it is the deferential teller-to-customer register, which is the
   wrong voice for a personal tool.
6. **First person is `ฉัน`**, which the Style Guide chooses deliberately to portray men and
   women as equals after rejecting the male-only `ผม`. Never `ผม`, never `ดิฉัน`.
7. **No gendered third person, and no inanimate `มัน`.** Use `คุณ`, a plural noun, or a role
   noun such as `ผู้ใช้` or `ผู้ดูแลระบบ`, and `ของตน` in place of `ของเขา`. For an object,
   name it again or drop it rather than reaching for `มัน`, which is the clearest sign that an
   English "it" was carried across. The guard bans `เขา`, `เธอ`, `มัน`, `ผม` and `ดิฉัน` as
   bare substrings, which is safe because a tone mark separates `เขา` from the very common
   `เข้า`.
8. **`ของตัวเอง` binds to the subject of its own clause, not to the nearest noun.** In a
   sentence the agent speaks about itself, `ฉันจะจำเสียงนั้นเป็นโปรไฟล์ของตัวเอง` says the agent
   keeps the voice as its OWN profile. Name the possessor instead: `โปรไฟล์แยกของคนคนนั้น`.
9. **Warmth comes from dropping formality markers, not from adding intimacy markers.** Thai
   has no informal second person to reach for. The safe informal end is `คุณ`, short
   sentences, `ไม่ได้` rather than the formal `ไม่สามารถ…ได้`, the casual question particle
   `ไหม` rather than `หรือไม่`, `ลอง` as the invitation verb, and omitting `โปรด` where the
   sentence is already soft.
10. **Apply the anti-formality word list**, because a formal translator produces the left
   column by default: `ประสบ` to `พบ`, `มีโอกาส` to `สามารถ`, `อย่างไรก็ตาม` to `แต่`,
   `ให้ความช่วยเหลือ` to `ช่วย`, `ให้คำแนะนำ` to `แนะนำ`, `อ้างอิงไปที่` to `ดู`,
   `เหมาะสม` to `ใช้ได้ดี`.

## Layout notes, because Thai fails differently

- **Thai runs short but tall.** Unlike German, a Thai string is usually the same length as
  the English or shorter in character count, so the layout risk is vertical clipping and a
  missing-glyph fallback, not horizontal overflow. Line height wants 1.6 to 1.75, because
  Thai stacks up to two diacritic levels above the baseline and one below.
- **Never apply `letter-spacing` to Thai.** Doing it correctly requires the renderer to
  decompose U+0E33 and reorder combining marks, which not every engine does. This is not
  yet true of the interface: 174 places set a `letterSpacing` value or a `tracking-*`
  utility, and several of them carry Thai text. It is a layout question rather than a
  catalogue one, so it is listed under Still open rather than fixed here.
- **Never truncate by code point or by byte.** The leading vowels `เ แ โ ใ ไ` are stored
  before their base consonant, unlike every other Indic script, so a naive cut leaves an
  orphan vowel. Truncate on grapheme clusters.

## Still open

- **The calendar era is a product decision that has not been made.** CLDR's default
  calendar for `th` is Buddhist, so an unpinned `Intl.DateTimeFormat('th-TH')` renders
  2026 as 2569. Verified at runtime. In the web UI 35 sites format with the selected
  interface language and a further 79 pass no argument at all and therefore follow the
  browser, so a Thai reader currently sees the Buddhist year nearly everywhere. Thai users
  read both eras, but next to an audit chain or a log timestamp the wrong one looks like a
  data bug rather than a locale choice, and Google, Microsoft and the Thai banks each do
  something different. Pinning the Gregorian era is `th-TH-u-ca-gregory`.
- **The voice lane is explicitly not settled by this round.** Every measurement behind the
  particle ban is on text resources. A spoken Thai assistant has an audible speaker, and
  Thai text-to-speech personas commonly do carry `ค่ะ`. That needs its own round, and this
  round's conclusion must not be carried into it unexamined.
- **The interface applies letter-spacing in 174 places** and stacks Thai inside chips and
  badges whose heights were tuned for Latin. Neither is decidable from the catalogue: both
  need one look at the running interface with Thai selected, and a Thai font installed.
- **The Thai reading of Veyllo and VAF has never been written down.** It does not affect
  this catalogue, but it has to be fixed before any Thai speech output exists. VAF is
  assumed to be read letter by letter.
- **The backend [Vocabulary Book](VOCABULARY_BOOK.md) carries Thai in 10 of its 32 phrase
  files.** The confirmation pair the agent matches an answer against is complete, so a Thai
  "ใช่" or "ไม่ใช่" is understood; the remaining phrases the agent speaks itself, the voice
  greetings and the enrollment prompts among them, still fall back to English while the
  interface is Thai. One Thai constraint applies to every phrase added there later: the
  matcher compares leading-only and treats a following letter as a word continuation, and
  Thai characters count as letters, so a Thai entry has to be the whole leading phrase
  rather than a stem. This is the same gap the Turkish round left open.

## Related

- [I18N.md](I18N.md) - how locales and keys are added.
- [I18N_KO_GLOSSARY.md](I18N_KO_GLOSSARY.md), [I18N_JA_GLOSSARY.md](I18N_JA_GLOSSARY.md) and
  [I18N_ZH_GLOSSARY.md](I18N_ZH_GLOSSARY.md) - the three companions. Thai disagrees with all
  of them on spacing, and shares only the dash ban, the single ellipsis and the one plural
  category.
- [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md) - the technical specification.
