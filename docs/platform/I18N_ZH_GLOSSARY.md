# Simplified Chinese (zh-CN) terminology and style

The binding reference for `web/messages/zh.json`. It exists because the Chinese
catalogue makes decisions that the English and German ones never had to make, and
a later edit that ignores them will not fail the parity guard: a key can carry a
perfectly grammatical Chinese sentence and still say the wrong thing.

Where a rule here is machine-checkable it is a test, not prose:
[`tests/test_i18n_locale_style.py`](../../tests/test_i18n_locale_style.py) enforces
punctuation, spacing, the ellipsis, the informal address, Traditional-form leakage,
the plural category, the dash ban, and the rule that identifiers survive translation.
The rest of this document is the part a test cannot check.

## Where the wording comes from

Every rendering below was taken from a vendor that ships it, not invented:

- **Microsoft** - the Terminology Collection TBX (57,236 en to zh-Hans pairs), the
  Chinese (Simplified) Localization Style Guide, and the shipped VS Code zh-Hans
  language pack. Trap: `learn.microsoft.com` pages carry an `ms.translationtype`
  field, and `MT` pages are machine output containing real errors. Only `HT` pages,
  the TBX and product resource files were treated as evidence.
- **Alibaba** - 阿里云百炼 Model Studio, 通义, ModelScope. Weighted highest for
  AI-product wording, because it is native Chinese product language rather than a
  translation of English.
- **Other Chinese-native AI products** - 扣子 Coze, Trae, 腾讯元器, Kimi, Dify.
- **Red Hat / Kubernetes / Docker / Aliyun ACK** for infrastructure and security.
- **Outlook zh-CN, QQ邮箱, 网易163邮箱, 阿里邮箱** for the mail client.
- **W3C clreq, GB/T 15834-2011, Ant Design** for typography and register.

## Decisions that were contested

Each of these had a defensible alternative. The reason is what binds, not the choice.

| Term | Rendering | Why, and what lost |
|---|---|---|
| agent | 智能体 | `代理` (Microsoft) also means proxy, and this product has a real proxy on its own security screen. |
| sub-agent | 子智能体 | `子代理` appears in Anthropic's own zh-CN docs, but it would contradict the head term. |
| token (context/billing) | `Token`, kept Latin | Microsoft ships 令牌, 标记 and 词元 for the same thing, sometimes in one page. 令牌 reads as a security token and would collide with this product's OAuth strings, where 访问令牌 is correct. |
| tamper-evident | 篡改可被发现 | Every vendor ships 防篡改, which claims prevention. The audit chain only detects. Shipping the standard term would have overclaimed. |
| Soul (the system-prompt file) | 灵魂 | No Chinese product ships this metaphor, and 灵魂 reads as marketing. It stays because the product also has `persona`, which is 人设; collapsing the two would lose a distinction the UI makes on screen. |
| Librarian (the file sub-agent) | 文件管家 | 图书管理员 is the literal reading and means library staff. The agent finds its way around your files. |
| A2A room | 群聊 | 房间 is a calque nobody ships. |
| calendar event | 日程 | Chosen against both fetched vendors (Outlook 事件, Google 活动) because Chinese consumer calendars ship 日程. Revisit if the surface starts showing vendor wording next to it. |
| working memory | 工作记忆 | No Chinese AI vendor ships an equivalent. The consumer-readable 短期记忆 is a weaker contrast to 长期记忆 and would blur what the tool group actually holds. |
| quarantine / isolation | both 隔离 | Chinese has one word for both, and this product shows both on one screen. Mitigation: always attach the object (隔离文件 for quarantine, 用户隔离 / 容器隔离 for isolation). Never write a bare 隔离 where the object is not obvious. |
| two-factor authentication | 两步验证 | Microsoft alone ships three (双因素身份验证, 双重验证, 多重身份验证). This is the consumer register. |
| account | 账户 | Microsoft's 帐户 is a minority spelling outside its own ecosystem. |
| guest | 访客 | Microsoft ships 来宾. |
| sign out | 退出登录 | Microsoft ships both this and 注销; mixing them in one UI is the actual defect. |
| skill | 技能 | In Coze 技能 is an umbrella that includes tools. Here it sits beside 工具, so never write 技能 where 工具 is meant. |

## Style rules

1. **Full-width punctuation** throughout: `，。、；：？！（）“”`. Half-width is correct
   only inside a complete English sentence, for digits, in code and paths, in decimal
   and thousands separators, and in English abbreviations and product names.
2. **One ASCII space** between a Chinese run and adjacent Latin letters or digits
   (`同步 Gmail`, `3 条记录`), and **no space** touching full-width punctuation.
3. **The ellipsis is a single `…`** (U+2026). Never `...`, never `……`.
4. **No dash, in either form.** Neither the em dash nor the Chinese 破折号 `——`.
   Rephrase, or use `（）`, `：` or `，`. A hyphen is not a valid Chinese substitute.
   The 顿号 `、` is only for parallel items inside an enumeration.
5. **Address the user as 你, never 您.** The agent refers to itself as 我. This matches
   the German `du` and English `you` the catalogue was written from.
6. **请 is not the English "please".** Use it for instructions and error recovery;
   never on a button. Buttons are bare verbs of two to four characters with no
   terminal punctuation.
7. **No `。` on labels, titles, table cells, field hints or status chips.** Multi-sentence
   copy does take it.
8. **Progress strings take 正在…**, completed states take the 已 prefix.
9. **Chinese has one plural category.** CLDR gives `zh` only `other`, so an ICU
   `one {...}` branch is dead code; write `{count, plural, other {# 个文件}}`. Use an
   explicit `=0` branch for an empty state.
10. **Pick the measure word per noun**: 个 for agents, files and tools; 条 for messages
    and memory entries; 项 for items; 次 for runs and calls; 封 for emails; 位 for people.
11. **Numbers stay Western**: half-width digits, `,` thousands separator, `.` decimal
    point, `25%` with no space.
12. **Simplified forms only**, and the traps are orthographic: 账 not 帐, 网 not 網,
    数据 not 資料, 程序 not 程式, 默认 not 預設, 文件 not 檔案.
13. **Translate intent, not words.** Drop what Chinese grammar already carries: a
    possessive, an article, a repeated subject, a `的` the compound reads fine without.

## Rendering

`web/app/layout.tsx` stamps `<html lang>` from the persisted locale before first paint,
alongside the theme. Without it, Chinese was painted under `lang="de"` until hydration, and
Han unification picks the glyph shapes of shared characters from the declared language.

## Still open

- The flat locale code `zh` means Traditional-script browsers (`zh-TW`, `zh-HK`,
  `zh-MO`) silently receive Simplified. That is better than the German fallback they
  got before, and it becomes a bug the day a Traditional catalogue ships.
- The mail setup wizard names the credential generically (应用专用密码). Google, QQ,
  163 and Alibaba each ship a different word for it; a user of one of the other three
  has to map it themselves. The per-provider `authHint*` strings carry `{provider}`,
  which is what makes that survivable.
- The backend [Vocabulary Book](VOCABULARY_BOOK.md) is a separate system and is far
  less complete in Chinese than this catalogue: most keys have no `zh` list, so the
  phrases the agent speaks itself fall back to English.

## Related

- [I18N.md](I18N.md) - how locales and keys are added.
- [TRANSLATION_SYSTEM.md](TRANSLATION_SYSTEM.md) - the technical specification.
- [VOCABULARY_BOOK.md](VOCABULARY_BOOK.md) - the backend's own canned phrases.
