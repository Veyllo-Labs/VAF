# Skills (Agent Skills / SKILL.md)

## Overview

A **skill** is a reusable, expert procedure packaged in the Anthropic Agent
Skills format: a folder containing a `SKILL.md` file (YAML frontmatter plus a
Markdown instruction body) and optional bundled files. Skills are the **second
routing tier under workflows**: when the router finds no matching workflow, it
checks whether a skill matches the request and, if so, suggests it.

Skills use **progressive disclosure**. Only each skill's `name` and
`description` are ever loaded for routing. The full instruction body is loaded
on demand — when the agent calls `use_skill(<id>)` — and any bundled files are
read only when the instructions reference them. This keeps the routing context
cheap regardless of how many (or how large) the installed skills are.

Skills are user content: an admin authors them in the settings editor or
uploads a `.zip` bundle. Every skill is security-scanned before installation
(see [Security scanning](#security-scanning)).

---

## Skill vs. Workflow vs. Tool vs. Whare Wananga

These are distinct layers; the terms are easy to confuse.

| Layer | What it is | Loaded how | Authored as |
|-------|-----------|-----------|-------------|
| **Tool** | A single capability the model can call (`web_search`, `coding_agent`, …). | Scoped per turn by the tool router. | Python `BaseTool` subclass. |
| **Workflow** | A fixed, multi-step pipeline of tool calls that runs deterministically on the `WorkflowEngine`. | Matched by the router; executed via `execute_workflow`. | `WORKFLOW` dict (`~/.vaf/workflows/<id>.py`). |
| **Skill** | Expert *instructions* the agent reads and then follows flexibly using its own tools. | Matched by name+description; body loaded on demand via `use_skill`. | `SKILL.md` folder (`~/.vaf/skills/<id>/`). |
| **Whare Wananga** | Per-tool know-how the agent *learns* about how to call one tool correctly. | Injected into a tool's description when scoped. | Learned, not authored. See [WHARE_WANANGA.md](../memory/WHARE_WANANGA.md). |

A workflow is a deterministic recipe; a skill is guidance the agent interprets.

---

## SKILL.md format

```
~/.vaf/skills/<skill_id>/
    SKILL.md            # required: YAML frontmatter + Markdown body
    scripts/ ...        # optional bundled files (read-only references)
    references/ ...
```

```markdown
---
name: PDF Form Filler
description: Fills PDF forms from a JSON field map. Use when the user has a
            fillable PDF and the values to put in it.
---
# PDF Form Filler

1. Read the PDF fields with ...
2. Map the data keys to fields.
3. Use write_file to produce the filled PDF.
```

- **Frontmatter** is parsed with `yaml.safe_load`. Required keys: `name` and
  `description` (both non-empty strings). Additional keys are preserved but not
  interpreted.
- **Body** is everything after the closing `---`. It is the on-demand payload.
- **`skill_id`** is derived from the *folder name*, not the frontmatter `name`:
  lowercased, non-`[a-z0-9_]` characters collapsed to `_` (e.g. folder
  `pdf-form-filler` → id `pdf_form_filler`). The folder is the identity; `name`
  is a display label.
- A malformed skill (missing fence, invalid YAML, missing required field) is
  **never silently dropped** — it parses as `valid=false` with an error and is
  shown as "broken" in the settings list, but excluded from routing.

The parser is the format authority: `vaf/skills/skill_md.py` (pure parsing, no
I/O policy, no LLM, no network).

---

## Storage and scoping

Skills are stored per-item under `~/.vaf/skills/`, mirroring the workflow layout
(`~/.vaf/workflows/`), with a `manifest.json` that mirrors the custom-tools
registry:

```json
{
  "version": 1,
  "skills": {
    "pdf_form_filler": {
      "folder": "pdf_form_filler",
      "created_by": "admin",
      "created_at": "...",
      "updated_at": "...",
      "shared_with": ["*"],
      "owner_scope_id": null,
      "scan": { "score": 0, "level": "clean", "count": 0 }
    }
  }
}
```

Visibility uses the same rules as custom tools (see
[User Isolation](../security/USER_ISOLATION.md)):

- `shared_with: ["*"]` — visible to every user.
- `shared_with: []` — admin only.
- `shared_with: ["<scope_id>", ...]` — those users plus admin.

**Visibility** (read/list/use) is governed by `shared_with` (above). **Edit/delete
authority** is governed separately by ownership: the optional `owner_scope_id` field
records which user owns a skill. `can_user_edit_skill(skill_id, user_scope_id)` returns
True for an admin (scope `None` or the local-admin scope), or for the owner
(`owner_scope_id == user_scope_id`); skills with no `owner_scope_id` (legacy / admin
WebUI skills) are admin-only to edit.

There are two mutation paths:

- **Admin WebUI / WebSocket** — create / edit / delete / upload and permission changes
  are **admin-only** (enforced in the handlers); these can set any `shared_with` and may
  override a high-risk scan. They do not set `owner_scope_id` (the field stays absent).
- **Agent self-service tools** — a regular user manages their **own** skills through the
  agent (`create_skill` / `update_skill` / `delete_skill`); see
  [Self-service skill tools](#self-service-skill-tools-per-user). These stamp
  `owner_scope_id`, keep the skill **private** (`shared_with=[owner]`), and cannot make a
  skill public or override the scanner.

The registry is `vaf/core/skills_registry.py` (`register_skill` carries the optional
`owner_scope_id`; `can_user_edit_skill` is the authority check); discovery and the
routing-facing list are `vaf/skills/templates.py` (`list_skills`, `reload_skills`).

---

## Routing (the second tier)

Skill matching shares the single workflow-router LLM call (no extra round-trip).
`analyze_workflow()` lists both workflows and skills and the router returns one
token:

```
AVAILABLE WORKFLOWS:
- create_website: ...
AVAILABLE SKILLS:
- pdf_form_filler: Fills PDF forms from a JSON field map. ...

=> output exactly one of:  workflow:<id>   skill:<id>   none
```

Control flow:

```
chat_step()
  → _try_workflow()
      → analyze_workflow()  [one LLM call: workflows + skills]
          workflow:<id> → run / suggest workflow (existing behavior)
          skill:<id>    → set self._pending_skill_match → return None
          none          → return None
  → if a skill matched: inject a one-shot [SKILL SUGGESTION] into the user turn
```

The injected hint mirrors `[WORKFLOW SUGGESTION]`:

```
[SKILL SUGGESTION] The skill "PDF Form Filler" (pdf_form_filler) looks relevant
to this request.
To load its full instructions call: use_skill(skill_id="pdf_form_filler")
Then follow the instructions and read any bundled files it references.
```

- The hint is **one-shot**: consumed and cleared immediately.
- A skill match is set **only when no workflow matched**, so the workflow and
  skill hints are mutually exclusive.
- Skill routing runs inside the workflow-router pass, so it is gated the same
  way: it never fires during a background thinking run, and setting
  `workflows_enabled: false` also disables skill auto-suggestion (the
  `use_skill` tool itself stays callable).
- When a skill is suggested, `use_skill` is pinned into the turn's active tool
  set so the agent can actually load it.

---

## Progressive disclosure and `use_skill`

Disclosure happens in three tiers:

```
Tier 1 (always)     name + description      → router / list_skills
Tier 2 (on demand)  the Markdown body       → use_skill(skill_id)
Tier 3 (on demand)  individual bundled files → read_file <absolute path>
```

`use_skill` (`vaf/tools/use_skill.py`) is a read-only `BaseTool`. Given a
`skill_id` it returns the instruction body plus an absolute-path listing of the
skill's bundled files, for example:

```
[SKILL: pdf_form_filler - "PDF Form Filler"]

<full Markdown body>

--- BUNDLED FILES (read with read_file using the absolute path) ---
- scripts/fill.py  ->  /home/<user>/.vaf/skills/pdf_form_filler/scripts/fill.py
```

- The body is capped (~14 KB) to keep the turn bounded; large reference material
  belongs in bundled files, loaded on demand.
- Bundled scripts are surfaced as **read-only references**, never executed by
  `use_skill`. To run one, the agent uses the normal `bash` / `python` tools,
  which carry their own confirmation gate.
- Visibility is scoped: `use_skill` resolves only skills visible to the calling
  user (`user_scope_id`); an unknown or out-of-scope id returns an actionable
  error listing the available skills.
- `~/.vaf/skills` is readable by the existing `read_file` safety check
  (`is_safe_path`), so no special file-access path is introduced.

---

## Self-service skill tools (per-user)

Beyond `use_skill`, the agent can manage a user's own skills directly, so a user can grow
their skill library by talking to the agent instead of only the admin WebUI. Five
read/write `BaseTool`s under `vaf/tools/`, all **user-isolated** (the calling user's
`user_scope_id` is injected at dispatch, `None` = admin):

| Tool | Does | Authority |
|------|------|-----------|
| `list_skills` | List the skills visible to the caller; flags `[yours]` the ones they own. | visibility (`shared_with`) |
| `read_skill` | Return a visible skill's raw `SKILL.md` source (inspect before editing). | visibility |
| `create_skill` | Create a new **private** skill owned by the caller. | any user |
| `update_skill` | Edit a skill the caller **owns** (full content replace). | ownership |
| `delete_skill` | Delete a skill the caller **owns** (`dangerous` → confirmation). | ownership |

Isolation and safety rules:

- **Private by default.** `create_skill` sets `shared_with=[owner_scope]` and
  `owner_scope_id=owner_scope` — visible to the owner and admins only. The agent can
  never set `["*"]` (making a skill public stays an admin/WebUI action).
- **Own skills only.** `update_skill`/`delete_skill` require
  `can_user_edit_skill`; another user's private skill returns a uniform *"not found or not
  yours"* (no existence leak). Admin (scope `None`) may edit/delete any skill and does not
  take ownership of it. `update_skill` preserves `owner_scope_id`, `shared_with`, and
  `created_at`, and never widens visibility.
- **Same validation + scan as the WebUI.** Create/edit build `SKILL.md` (from `name` +
  `description` + `body`, or a raw `skill_md`), validate with `parse_skill_md_text` before
  writing, and run the static scanner — a **`high`** result blocks the write. Unlike the
  admin WebUI there is **no override** exposed to the agent. SKILL.md only: bundled
  scripts still come via the admin zip upload.
- Every mutation calls `reload_skills()` so the router list and `list_skills` cache stay
  current.

**Surfacing.** The unified tool router force-activates these tools, **verb-scoped**, when
the message is about skills (EN/DE keywords incl. "skill"/"fähigkeit"): list/show →
`list_skills`+`read_skill`; create/learn → `create_skill`+`read_skill`; edit →
`update_skill`+`read_skill`+`list_skills`; delete → `delete_skill`+`list_skills`
(`use_skill` is added too). The verb scoping keeps each turn under the per-turn tool cap.

---

## Security scanning

Because skill bundles carry instructions (prompt injection) and scripts
(dangerous code) that the agent will load, every skill is statically scanned
before installation. The scanner (`vaf/skills/scanner.py`) is **static only** —
regex and heuristics, no LLM, no network, and it never executes anything.

It scans the SKILL.md body for authored/edited skills, and the body plus every
bundled text/code file for uploads. Frontmatter metadata is not scanned as
prose.

Categories include: prompt injection, system-prompt leak, covert action, data
exfiltration, remote code execution (`curl … | sh`), destructive commands,
credential/secret access, hardcoded secrets (AWS/OpenAI/GitHub keys, private
keys), dangerous code (`eval`/`exec`/`os.system`/`subprocess shell=True`/
`child_process`), obfuscation (base64), and hidden/bidi control characters.

Each finding has a severity; the scan yields a score (0–100) and a level:

| Level | Gate |
|-------|------|
| `high` | **Blocked** — not installed unless an admin overrides. |
| `medium` | Allowed; recorded and badged as caution. |
| `low` / `clean` | Allowed. |

- A blocked install returns the findings to the editor and offers an admin
  **override** (`override: true`) for trusted skills. Uploads raise
  `SkillScanBlocked`, which carries the full scan.
- The scan result (`{score, level, count}`) is recorded in the manifest and
  surfaced by `list_skills`, so the settings grid shows a risk badge.
- Static analysis produces false positives by design; the admin override is the
  intended escape hatch, not a bypass to remove.

This is inspired conceptually by NVIDIA SkillSpector's risk taxonomy; the
implementation is native to VAF (no third-party code or dependency).

---

## WebSocket API

All skill operations go over the existing WebSocket channel. Mutations are
admin-only.

| Message (client → server) | Payload | Effect |
|---------------------------|---------|--------|
| `get_skills` | — | Returns `skills_list` scoped to the user (admins also get invalid skills and the raw `source`). |
| `get_skill_source` | `skill_id` | Returns `skill_source` with the raw SKILL.md (admin). |
| `create_skill` | `skill_id`, `name`, `description`, `body` (or raw `skill_md`), `shared_with?`, `override?` | Writes the folder, scans, registers. |
| `update_skill` | same as create | Rewrites SKILL.md (bundled files preserved), scans, re-registers. |
| `delete_skill` | `skill_id` | Removes the folder and manifest entry. |
| `update_skill_permissions` | `skill_id`, `shared_with` | Changes visibility. |
| `upload_skill` | `data` (base64 zip), `shared_with?`, `override?` | Safe-extracts, scans, registers. |

| Message (server → client) | Meaning |
|---------------------------|---------|
| `skills_list` | The (scoped) list of skills, each with `valid`, `scan`, `shared_with`, and (for admins) `source`. |
| `skill_created` / `skill_updated` / `skill_deleted` | Operation succeeded; a fresh `skills_list` is broadcast to all clients. |
| `skill_permissions_updated` | Permissions changed. |
| `skill_error` | Operation failed. On a scanner block it includes `scan` (findings) and `can_override: true`. |

Zip import is hardened: a `SKILL.md` must exist at the archive root or inside a
single top-level folder; entries that escape the destination (Zip-Slip /
absolute paths) and symlink entries are rejected; extraction is staged in a temp
directory and moved into place only after validation.

---

## Settings UI

Settings → Advanced → **Skills** (directly under Workflows). The grid lists the
user's visible skills with a risk badge for scanned medium/high skills and a
"broken" badge for skills that fail to parse. Admins can:

- **Create** a skill (name, description, Markdown instructions).
- **Upload** a `.zip` folder bundle.
- **Edit** or **delete** an existing skill.
- **Override** a high-risk scan via a checkbox shown after a block.

Components: `web/components/settings/SkillsEditor.tsx`,
`web/components/SettingsModal.tsx`, wired in `web/app/page.tsx`.

---

## File map

| File | Role |
|------|------|
| `vaf/skills/skill_md.py` | SKILL.md parser; `derive_skill_id`; in-memory text validator. |
| `vaf/skills/templates.py` | Discovery, `list_skills`, `reload_skills`. |
| `vaf/skills/scanner.py` | Static security scanner; `SkillScanBlocked`. |
| `vaf/core/skills_registry.py` | Manifest, scoping, `save_skill_md`, `import_skill_zip`, scan gate. |
| `vaf/tools/use_skill.py` | On-demand delivery tool. |
| `vaf/tools/{list,read,create,update,delete}_skill.py` | Per-user self-service skill-management tools (owner-isolated). |
| `vaf/core/agent.py` | Unified router, `[SKILL SUGGESTION]` injection, `use_skill` pinning, scope injection, self-service skill-tool gating. |
| `vaf/core/web_server.py` | WebSocket handlers and `_broadcast_skills_update`. |
| `web/components/settings/SkillsEditor.tsx` | Create/edit/upload editor. |

---

## Configuration

Skills have no dedicated toggle. Skill auto-suggestion rides on the workflow
router, so `workflows_enabled: false` disables it (the `use_skill` tool remains
available). Storage lives under `~/.vaf/skills/`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Skill never suggested | Its `description` must clearly state when to use it — the router matches on `name` + `description` only. Confirm the skill is `valid` and visible to the user. |
| Skill shows as "broken" | Frontmatter is missing/invalid or lacks `name`/`description`. Open it in the editor; the error is shown. |
| Upload/create rejected as high-risk | The scanner found a high-severity pattern. Review the findings; if the skill is trusted, tick the override and retry. |
| Skill not visible to a user | Check `shared_with`. `[]` is admin-only; add the user's scope id or `"*"`. |
| Agent ignores the suggestion | The hint is advisory. The agent decides based on full context; it may handle the request directly. |

---

## Related Documentation

- [Workflow Selection](WORKFLOW_SELECTION.md) — the first routing tier and the shared router.
- [Tool Router Architecture](TOOL_ROUTER_ARCHITECTURE.md) — per-turn tool scoping.
- [Whare Wananga](../memory/WHARE_WANANGA.md) — per-tool learned know-how (a different layer).
- [User Isolation](../security/USER_ISOLATION.md) — per-user scoping model.
- [Web UI](../web-ui/WEB_UI.md) — settings and the WebSocket API.

---

*Last updated: 2026-06-26*
