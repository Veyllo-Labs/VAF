# Code Audit

Code Audit reviews a code change and reports the real problems in it: bugs, security holes,
risky changes. Every finding is proven against the code before it is reported, and each one
comes with a prompt a coding agent can act on. It runs in three places, all on one engine
([vaf/core/code_audit.py](../../vaf/core/code_audit.py), `vaf.code_audit` on the facade):

| Where | Who calls it | What happens with the findings |
|---|---|---|
| Inside the coding agent's loop | the coder itself, after it committed | they become one more task of the same run; the run commits and reviews again |
| The `code_audit` tool | the main agent, when the person asks for a review | shown to the person with the question whether the coder should fix them; nothing changes before a yes |
| `vaf audit run` | a person or a script, on any git repository | printed (text, JSON or a prompt), exit code 0 / 1 / 2 |

## Why it exists

A hosted reviewer works on its own schedule and behind its own rate limit; the coding agent
needs the answer inside its loop, after every commit, and a person needs it for any repository
on the machine. The pipeline follows what makes a hosted reviewer useful: findings that are
verified before anyone sees them, a fixed schema, a fix prompt per finding, and a completion
contract a failed run cannot pass.

## The pipeline

1. **Scope from git.** `changes` (the default) is the base against the working tree, plus new
   files git does not track yet; `committed` is base to HEAD and reads the files as HEAD holds
   them; `uncommitted` is HEAD to the working tree; `files` reviews whole files. The base
   defaults to the merge base with the upstream, else the previous commit, else git's empty
   tree. A given base is resolved to the commit it names before it reaches `git diff`, and
   one that names no commit fails the run: the tool takes `base` from the model, and an
   option-shaped value such as `--output=<file>` would make git write over that file.
   Generated and vendored paths (`node_modules`, `dist`, `.next`, lockfiles, minified
   files), binaries and files over 2 MB are skipped and listed with the reason, never
   silently. A large source file is reviewed by its diff: only numbered windows around the
   changed lines reach the model.
2. **Deterministic evidence first.** The secret rules of the skill scanner run on every
   changed file (a hit is critical, security, and never quoted), and ruff runs on the changed
   Python files with a bug-oriented rule set (`F,B,S,BLE,E9` minus the rules that are noise in
   review, and `F811`, which is how pytest fixtures are imported), reported only on changed
   lines; in test files the bandit rules are skipped, because binding to 0.0.0.0 or hashing a
   literal is test data there. ruff reads the reviewed text from a scratch copy, `--isolated`,
   so the repository's own configuration is never loaded. Both are findings of
   their own and context for the model. The pyflakes rules (`F`, `E9`) are facts and count
   as proven; the heuristic ones (bandit's `S`, bugbear's `B` and `BLE`) flag a pattern, so
   with a model present they go through the verifier like the model's own findings (measured:
   three of eleven "major" findings were `S608` on a query built from `?` placeholders and
   `S324` on hashes used as ids and cache keys).
3. **Context on a budget.** Per file: the diff, numbered windows around the changes, and where
   the names the change defines are used elsewhere (`git grep`). Per batch: the guideline files
   that govern the changed paths (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `GEMINI.md`,
   `.github/copilot-instructions.md`, nearest first) and the repository's path instructions.
   Everything is redacted with VAF's credential patterns before it reaches the provider.
4. **One review call per batch** (about 40,000 characters). A file whose change does not fit
   is reviewed in parts of whole hunks, each with its own numbered windows, instead of being
   cut: a cut view hides the later hunks while the run reads as complete. The model answers JSON in a fixed
   schema: `type` (issue, refactor, nitpick), `severity` (critical, major, minor, trivial),
   `category` (correctness, security, data_integrity, performance, stability,
   maintainability), `effort`, and the `evidence`: a quote of the code the finding is about.
   An answer that is not readable JSON for several files (or whose `findings` is not a list)
   is asked again in halves, and one file again in smaller parts, until a part that cannot be
   split; what still gets no readable answer is listed as not reviewed. Measured on a live run
   over 131 files: the two largest changes (329 and 109 changed lines) got no readable answer
   as a whole.
5. **Verification before anything is reported.** The quote must be in the current file: a
   wrong line is moved to where the quote is, an invented quote drops the finding. Then a
   second call per four findings answers CONFIRMED or REJECTED for each; an unreadable answer
   is asked again in halves, down to one finding. Next to the code around the quote, the
   verifier gets the definitions of the functions the quote calls (`git grep`, up to four), and
   is told that a claim about a call - it can raise, return nothing, skip a step - must agree
   with that definition. A claim about `[redacted]`, the placeholder the redaction put into the
   code the model saw, is dropped before verification. Rejected findings are dropped (and
   counted), findings nobody could confirm are listed apart as unverified, without a fix
   prompt.

   **The deep check.** What the first check confirms is checked once more, one finding at a
   time, by a verifier that may search the repository and read files before it decides
   (`verify_steps`, default 6, `vaf audit run --verify-steps N`; 0 keeps the first check
   only). Why: on a live run 23 of 61 confirmed findings were false when checked by hand,
   and nearly all of them rested on a fact outside the excerpt - the called function never
   raises, a `finally` cleans up, only one caller exists, the default is a documented
   decision. The tools are a text protocol inside `ask`, one JSON object per turn
   (`{"action": "search", "pattern", "path"}`, `{"action": "read", "file", "start", "end"}`
   or the verdict), so a local model without tool calling runs it too. The search is
   `git grep --untracked -E -e <pattern>`: code and documentation alike, never an ignored
   file; a read goes through the same filters as the review (a file git does not track is
   never read), and everything that comes back is redacted. The verifier is told which
   Markdown files name the finding's file, and that a behaviour a design document or a
   `Deliberate:` comment states as intended, with its reason, is not a defect - while the
   code, not the document, decides what actually happens. Its verdict carries a confidence
   (below 70 counts as rejected); a critical or major claim must name the path that reaches
   it, else it is confirmed as minor, and the check may lower a severity but never raise
   it. A finding it never decides on is unverified, and the run is incomplete. The first
   check stays in front because it is cheap (four findings per call) and drops about half
   of what the review proposes. A model that writes its tool calls in its own markup instead
   of the JSON (a DeepSeek-served model sends `<｜｜DSML｜｜invoke ...>` blocks, several per
   turn) is read through `vaf.core.tool_call_recovery`, each call counting as one step.
   Measured on those 61 findings, each already judged by hand: the deep check kept 26 (13
   real, 10 partly right, 3 false), rejected 20 of the 23 false ones and 2 of the 15 real
   ones (two judgment calls on test style and a file mode), and left 2 undecided; all six
   false major findings were rejected, the one real major security finding kept. It cost
   about 1.4 EUR for the 61 (1.35 million tokens, 291 calls, 5 minutes at four in parallel).
6. **Deduplication and the profile.** Overlapping findings of one source and category in one
   file merge, and the same title in several places becomes one finding with its locations.
   `chill` (the default) reports bugs, security and what matters; `assertive` adds nitpicks
   and trivial findings.
7. **Memory per repository**, under the git directory (`.git/vaf-audit/`, never committed):
   which findings were open last time (named as addressed when they are gone) and which a
   person dismissed with a reason (not reported again). Ids are a hash of file, category and
   the quoted line, so the same problem in the same code keeps its id across runs. Not the
   title: a model words one finding differently every time (a live loop of four rounds never
   produced the same title twice), and an id built on it let a dismissed finding come back.
   VAF's own bookkeeping in a project (`.vaf/`: the coder's task list, codex and memory) is
   never reviewed: on a live loop it put the coder's task text into the review, and the
   reviewer then argued against the fixes it had asked for.
8. **Checks in plain language** from the repository's configuration, each `passed`, `failed`
   or `inconclusive`; a failed check in `error` mode fails the run like a finding.

### The completion contract

`status` is `complete` only when everything in scope was reviewed. `incomplete` means some
files were not (over the file budget, no readable answer, no model at all) and names them;
`failed` means nothing was reviewed or the audit was stopped. `exit_code(fail_on)` returns 0
only for a complete run without findings at or above `fail_on`, 1 for findings (or a failed
error check), 2 for an incomplete or failed run - also with `--fail-on none`. An audit that
could not look is never reported as an audit that found nothing.

### The fix prompt

Every verified finding renders as a prompt for a coding agent, behind a preamble that keeps
the finding data rather than instructions: verify each finding against the current code and
fix only what is still valid. `AuditReport.fix_prompt()` groups them by file, most severe
first; `max_chars` bounds it by whole findings and names the ones left out. The main agent
hands exactly this to `coding_agent` after the person said yes; the coder's own loop puts it
into the fix task's context.

## In the coding agent's loop

Once every task of a run is done, the coder commits (`VAF Coder: <task> ... Code audit round
k`), reviews the run's change from its start commit to HEAD with its own model and endpoint,
and turns every verified problem of type `issue` at `minor` or above, plus every failed error
check, into one more task: "Fix the code audit findings (round k)", with the fix prompt as its
context. That task runs through the same gates as every other (verify-before-done, the linter,
stuck detection). When it is done the run commits and reviews again.

**Each finding gets one fix attempt.** A fix task carries only findings no earlier fix task
of the run was pointed at; one that is still there afterwards is not handed over again.
"Pointed at" means the same id, or the same file and the lines an attempted finding sat on
(two lines of slack): a model reports one problem with another title, another quoted line or
another category from round to round, and on a live run three unchanged bugs read as new for
four rounds while only ids were compared. A round that ends the loop also ends it for the
next all-done exit point the same run end passes through. The fix task
also says that what the original task explicitly asked for stays in force: a finding that asks
for something the task ruled out is left, with the reason. Measured on a live run before this
rule: the task said to commit a given file unchanged, the first round's fixes changed it, a
later round flagged exactly that, the coder reverted, and the next round found the original
bugs again - fix, revert, fix, until it was stopped by hand. Every fix writes new code and the
next review usually finds something in it, so "the same findings as last round" never matched.

It stops when:

| Outcome | Condition | Summary line |
|---|---|---|
| clean | a complete review with nothing to fix | "Code audit: clean after k round(s)" |
| incomplete | nothing found, but the review did not cover everything | the reason; never read as clean |
| no progress | every finding left was already given to a fix task | the findings still open |
| limit | `coder_audit_max_rounds` rounds (50) ran | "Code audit stopped: the audit limit ... is reached" |

Proven findings are fixed even when the review around them could not cover every file. The
model may also call `code_audit` itself during a task (the run's work so far, committed or
not); those calls count toward the same limit. The final retry for failed tasks comes first,
the audit second, at all six places where a run can end with every task done
(`_next_round()` in [vaf/tools/coder.py](../../vaf/tools/coder.py)). In the SubAgent window
the stepper reads plan, build, audit, document, commit; build and audit alternate while
findings are fixed, and each round leaves a row in the Guards tab. A run whose review is off
or content-only keeps the four-step stepper.

Every round reviews the whole run's change, not only the last fix. Deliberate: an
incremental review of the fix commit alone would let a finding the fix did not touch
disappear unseen, and "clean" would then be wrong.

Local mode: one review call is sized to the window (`n_ctx` times 1.2 characters, at least
6,000), and calls go one at a time - the local server runs one inference.

Configuration (both admin-only, every round is model calls on the instance's account):
`coder_audit_enabled` (default `True`) and `coder_audit_max_rounds` (default `50`), see
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md).

## For the main agent: the `code_audit` tool

[vaf/tools/code_audit.py](../../vaf/tools/code_audit.py). Read-only (`permission_level`
read, `file_access` read): the project path is resolved like the coder's project tools
(this chat's project when none is given), refused for VAF's own source and the standard
folders, and asked against the account's file jail before anything is read. Parameters:
`project_path`, `scope`, `base`, `paths`, `profile`. At most 40 files per call, four model
calls at once for an API provider. Stop in the chat ends the review before its next model
call (`session_id` is in its `identity_kwargs`).

The result is compact: status, walkthrough, one line per finding. When there is something to
fix, it ends with the instruction to show the findings, ASK whether the coding agent should fix
them, change nothing before a yes, and on a yes call `coding_agent` with the project path and
the fix prompt, unchanged. It is returned whole (`result_is_deliverable`): the chat cuts a
tool result to 2,000 characters by removing its middle, which would remove exactly the
question and the prompt. In return it bounds itself: the fix prompt carries findings up to
20,000 characters, most severe first, and names the rest (`AuditReport.fix_prompt(max_chars=)`;
the coder's own fix task is bounded the same way, and its next round reports them again). The router hints the tool
for phrases that name code ("code review", "prüf den Code", "check my code"), not for
"review" or "audit" alone, which sit inside "preview" and "audit log". The `code_review` workflow, which
rewrites a file, now answers only to "improve" and "optimize".

## On the command line: `vaf audit`

```
vaf audit run [PATH] [--base REF] [--committed | --uncommitted | --files] [--path P ...]
                     [--untracked/--no-untracked] [--profile chill|assertive]
                     [--format text|json|prompt] [--fail-on critical|major|minor|none]
                     [--max-files N] [--parallel N] [--no-llm] [--provider P] [--model M]
vaf audit show [PATH]                    # the last audit of this repository
vaf audit dismiss ID --reason "..." [--repo PATH]
```

The configured model is used unless `--provider`/`--model` say otherwise. Progress lines go to
stderr; stdout carries only the report, so `--format json` can be piped.

## Repository configuration: `.vaf/code-audit.json`

```json
{
  "profile": "assertive",
  "path_filters": ["src/**", "!src/generated/**"],
  "path_instructions": [
    {"path": "src/payments/*", "instructions": "Amounts are integers in cents, never floats."}
  ],
  "checks": [
    {"name": "Tests for new endpoints", "mode": "error",
     "instructions": "Every new HTTP route has a test."}
  ]
}
```

JSON, not YAML: PyYAML is not a declared dependency. The file is read only from inside the
repository.

## Budgets and measurements

Output budgets are 32,000 tokens for a review call and for a verification, 8,000 for the
checks. A reasoning model spends most of them thinking before the JSON: measured on one
43,000-character batch with the Veyllo model, 4,000 and 16,000 tokens both ended inside the
reasoning with no answer, and 32,000 answered after 130 seconds with 113,000 characters of
reasoning before it. Verification was first eight findings per call with 16,000 tokens: on a
live run over 125 files it left 81 of 101 findings unanswered, which is why it is four per call
now, with the same budget as a review and the same split on an unreadable answer. A provider
that refuses a figure this large is retried by the API backend with its safe cap. Calls time out after 600 seconds. Four calls run at once for an API
provider (`parallel_for()`), one for the local server.

## How often it is right (measured)

Every one of the 55 findings of the live run over 131 files was checked by hand against the
code: 13 real (two of them fixed in between), 21 partly true (a real observation, overstated
or with a scenario that cannot happen as described), 21 not true. CodeRabbit, over the same
range, reported 9, of which 8 were real; the two lists shared one finding and three related
ones. The 21 false findings had four causes: the verifier saw only the code around the quote
and not the function the claim was about (most of them), heuristic ruff rules that skipped
verification (3), a claim about the redaction placeholder (1), and facts that need a search of
the whole repository (that a message key exists in every catalog, that a config value is
clamped elsewhere). With the callee definitions and the placeholder rule, the verifier run
again over the same 55 kept 12 of the 13 real findings and dropped 12 of the 21 false ones.
What remains needs a verifier that can search the repository itself, which is a named step,
not built: it costs a tool loop per finding.

## Named boundaries

- **No JavaScript tooling** (eslint, tsc): their configuration files are programs, and a
  repository's own config would run code on the host. A hosted reviewer was taken over
  exactly that way, through a repository's linter configuration. ruff's configuration is data,
  and is not loaded anyway (`--isolated`).
- **No code graph and no embeddings index**: references come from `git grep` on the names a
  change defines.
- **No sequence diagrams**: the walkthrough is text (summary, one line per file, effort 1-5).
- **Reading stays inside the repository**, because what is read goes to a model provider: a
  symlink out of it is skipped and listed, a file the model names outside the change is read
  only when git tracks it, and a guideline file git ignores is someone's private notes and
  stays out.
- **git runs with the repository's fsmonitor hook, external diff driver and textconv filters
  switched off.** Clean filters a repository's config defines still run on a working-tree
  diff: they cannot be switched off without knowing their names, and that config is local to
  the clone (git never transfers it), so it is config the same account already runs git under
  in that directory.
- **Stop in the chat** ends the review before its next model call; a call already in flight
  finishes in the background (an in-process tool cannot be killed), and its answer is
  discarded.
