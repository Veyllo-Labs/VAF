# Japanese (ja-JP) terminology and style

The binding reference for `web/messages/ja.json`. Japanese needs its own document
rather than a section in [I18N_ZH_GLOSSARY.md](I18N_ZH_GLOSSARY.md), because on the
one rule that matters most the two languages are opposites: Chinese puts an ASCII
space at every Latin boundary, Japanese forbids it.

Where a rule here is machine-checkable it is a test, not prose:
[`tests/test_i18n_locale_style.py`](../../tests/test_i18n_locale_style.py) enforces the
spacing, the ellipsis, the banned dashes, half-width katakana, the long-vowel policy,
the second person, the kana/kanji conventions, the plural category, Simplified-Chinese
leakage, and the rule that identifiers survive translation.

## Where the wording comes from

- **Microsoft** - the Terminology Collection (ja, 58,715 entries), the Japanese
  Localization Style Guide, and the shipped VS Code ja and Windows Terminal ja resource
  files. Trap, and it is worse here than for Chinese: of roughly 45 `learn.microsoft.com`
  ja pages probed, only five carried `ms.translationtype: HT`. Every page that defines
  RAG, embeddings, function calling, agents, fine-tuning, STT and TTS was machine
  translated and was excluded. Even one HT page's title read プロンプト as "prompt, quick"
  and rendered it 迅速な. Product resource files are the reliable Microsoft source.
- **Japanese-native products** for register and product nouns: Cybozu kintone, freee,
  Slack ja, Notion ja, Figma ja, and the Japanese UI of the major assistants.
- **Red Hat, Kubernetes, AWS, Docker** ja documentation for infrastructure.
- **Outlook ja, Gmail ja, Thunderbird ja, Cybozu Garoon** for the mail client.
- **JTF日本語標準スタイルガイド, 内閣告示「外来語の表記」, JIS Z 8301, W3C jlreq,
  公用文作成の考え方 (2022)** for typography.

## The long-vowel policy, stated once

Applied to every katakana term without exception, because a mixed orthography is the
loudest sign that a Japanese catalogue was never proofed.

1. If the English ends in **-er, -or or -ar**, the ー stays:
   サーバー, ユーザー, コンピューター, プロバイダー, フォルダー, フィルター, コネクター,
   パラメーター, ブラウザー, エディター, コンテナー, スピーカー.
2. Otherwise the ー is dropped once the katakana word reaches four characters:
   メモリ, カテゴリ, セキュリティ, プロパティ, アクティビティ, プロキシ, クォータ.
   Under four characters it stays: メニュー, キュー.

`コンテナー` and `ブラウザー` are deliberate even though Docker, AWS and Kubernetes ja all
ship `コンテナ` and consumer Japanese overwhelmingly writes `ブラウザ`. One consistent
policy beats per-word field preference, and the guard would otherwise have no rule to
enforce. `ユーザ`, `サーバ`, `コンピュータ`, `フォルダ`, `ブラウザ` are build errors.

## Decisions that were contested

| Term | Rendering | Why, and what lost |
|---|---|---|
| two-factor authentication | 2段階認証 | 2要素認証 is the literally correct term and the one Microsoft's termbase pins, but this flow is TOTP with an authenticator app, and 2段階認証 is what a Japanese user has read on their own Google and Microsoft account screens. Recognition beat literal accuracy. |
| tamper-evident | 改ざん検知 | 改ざん耐性 claims resistance. The hash chain detects; it does not resist. Same call as the Chinese round. |
| the five sub-agents | コーディング / リサーチ / ドキュメント作成 / ファイル整理 / ブラウザー + エージェント | kintone, the strongest Japanese-original precedent, names specialists `〈what it does〉+AI` and ships zero `〜エージェント`. That shape reads more Japanese, but the product's own model is agents and sub-agents, and mixing the two systems is worse than either. 司書エージェント for the librarian was rejected outright. |
| room (A2A) | ルーム | No Japanese product puts agents in a thing it calls a ルーム, so this is being first. The alternative was renaming the concept to チーム in Japanese only, which would diverge from the English UI and the `vaf a2a` CLI. LINE WORKS ships トークルーム, so the noun itself is familiar. |
| Soul | `Soul`, left in Latin | ソウル collides with Seoul, and 魂 reads as literature. It also has to stay distinct from persona, which is 性格, because the product shows both. |
| trash | ごみ箱 | Gmail ja ships ゴミ箱, but Outlook, Thunderbird and Cybozu ship ごみ箱, and ごみ is a native word that takes hiragana. |
| working memory | 短期記憶 | Neither this nor ワーキングメモリ is attested in any shipped Japanese UI. 短期記憶 is the readable contrast to 長期記憶. |
| latency | 応答時間 | The glossary's 待機時間 means waiting time, which is not what a measured search latency is. |
| container | コンテナー | See the long-vowel policy above. |

## Style rules

1. **No space between Japanese and Latin or digits**: APIキー, MCPサーバー, Dockerが,
   2段階認証, 30日間. JTF basic rule 10. jlreq puts a quarter-em gap there in CSS; a typed
   U+0020 is a half em and cannot be suppressed at a line break. Microsoft's spaced style
   (`タイム ゾーン`) is legacy compensation and is not copied. A code identifier in prose is
   wrapped in 「」 rather than spaced apart.
2. **No space or 中黒 inside katakana compounds**: コンテキストウィンドウ, システムプロンプト,
   ダークモード. 中黒 is for listing nouns and foreign personal names.
3. **Sentence punctuation is full-width 、 and 。**, never ，or ．. Half-width comma only as
   a thousands separator, half-width period only as a decimal point or in a file name.
4. **Terminal 。**: labels, buttons, tabs, menu items, column headers, chips and one-line
   empty states take none. Every explanatory sentence, toast, error and warning takes it.
5. **Brackets**: 「」 for quoted strings, values, identifiers and screen titles; 『』 nested
   or for titles; full-width （） for asides. Never `“ ”`.
6. **The ellipsis is the single character …** (U+2026).
7. **No dash or tilde, in any form**: — ― – 〜 ～. A numeric or time range uses から.
   Never substitute ー, which is the long-vowel mark and corrupts words.
8. **No half-width katakana.**
9. **Register by element type.** Buttons, menu items, tabs, section titles and column
   headers are 体言止め, a bare noun (保存, 送信, 削除, 完了), with the established verb
   exceptions 閉じる, 戻る, 開く. Toggle labels are a plain dictionary-form verb.
   Descriptions, help text, errors, toasts and empty states are です・ます with 。.
   A destructive confirm button flips to polite first person: 「はい、削除します」.
10. **Never write あなた.** Japanese drops the subject; ご自身の where a possessive is
    unavoidable. The German source's `du` maps to です・ます, not to 常体: warmth comes from
    stripping the honorific layer above it, so no お客様, no 弊社, no いたします, no くださいませ.
11. **`〜してください` is rationed** to actions only the user can take. What the app can do
    itself, it does and reports: 〜しました. Capability is 〜できます.
12. **Errors have two beats**: 〜できませんでした。 then 〜お試しください。 The retry verb in prose
    is お試しください; 再試行 is the button label only. No 申し訳ございません.
13. **Empty states are one sentence**, no 。: 「まだ〈対象〉はありません」, or
    「該当する〈対象〉はありません」 when a filter matched nothing. The call to action lives in
    the adjacent button.
14. **Destructive copy**: 「この操作は元に戻せません。」 for irreversibility, and a specific
    affirmative button rather than a generic はい.
15. **Counters (助数詞), one per noun**: files, tools and agents 個 · messages, records,
    events, tasks and tool calls 件 · emails 通 · people 人 · runs and attempts 回 ·
    day spans 日間 · devices 台 · tokens bare (12,000トークン).
16. **One plural category.** `Intl.PluralRules('ja')` resolves to `['other']`, so an ICU
    `one {...}` branch is dead code. The counter never changes with the number.
17. **Numbers and dates**: half-width digits, `,` grouping, `25%` with no space,
    2026年8月27日 with no zero padding, a half-width colon in a time.
18. **Orthography**: すべて (never 全て) · 〜してください (never 〜下さい) · か月 (never ヶ月) ·
    改ざん in mixed script.
19. **Do not coin kanji for a loanword the vendors ship in katakana.** The texture of
    Japanese UI is katakana carrying the product nouns and native kanji verbs carrying the
    actions (通知, 設定, 削除, 保存, 表示, 実行).

## Rendering

`web/app/layout.tsx` stamps `<html lang>` from the persisted locale before first paint,
alongside the theme. This matters more for Japanese than for any other locale: Han
unification means the glyph shapes of shared kanji are chosen by the declared language,
so painting Japanese under `lang="de"` until hydration shows Simplified-Chinese forms on
a machine that has a Chinese font. The app ships no font stack, which is fine on Windows
(Yu Gothic UI) and macOS (Hiragino Sans); a Linux machine without a Japanese font
installed will still fall back to whatever CJK face it has.

## Two gaps that belong to the components, not the catalogue

Both were found by the Japanese review and both are ratcheted rather than cleared:

- **A hard space in front of a translated word** (`{count} {t('unit')}`). Chinese wants that
  gap, Japanese forbids it, so it comes from `common.unitSeparator`. The three sites the
  review named are fixed; eighteen older ones are pinned by count in the guard and may only
  go down.
- **A locale tag hardcoded in a date or number format.** `page.tsx` and `SubAgentWindow.tsx`
  hold 22 `en-US`/`en-GB` calls, which have always given every non-English locale the wrong
  format. Japanese is where it becomes a wrong VALUE rather than a wrong style, because
  German grouping renders 1234 as `1.234`, which reads as a decimal. The four sites on the
  Japanese security screen are fixed; the rest are pinned by count.

## Still open

- The flat locale code `ja` is sufficient, unlike Chinese: `Jpan` is a single unified
  script code, so there is no ja-Hans/ja-Hant split to route.
- The backend [Vocabulary Book](VOCABULARY_BOOK.md) is a separate system and has no
  Japanese at all, so the phrases the agent speaks itself fall back to English.

## Related

- [I18N.md](I18N.md) - how locales and keys are added.
- [I18N_ZH_GLOSSARY.md](I18N_ZH_GLOSSARY.md) - the Simplified Chinese companion.
- [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md) - the technical specification.
