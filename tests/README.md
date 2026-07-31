# Tests

This directory contains the testing suite for VAF, including unit tests, integration tests, and system-level checks.

## Structure

The suite contains dozens of `test_*.py` files following the `test_*.py` naming convention. Rather than list each one, the tests are grouped by the area they cover. The list below is representative, not exhaustive:

- **LLM providers and failover**: provider integrations and the cross-provider/backend failover and routing paths (e.g. `test_anthropic_provider.py`, `test_provider_failover.py`, `test_router_provider_agnostic.py`).
- **Thinking and proactive runs**: the proactive/"thinking" loop, its delivery channels, dedup, and guards (e.g. `test_thinking_proactive.py`, `test_thinking_channel_delivery.py`).
- **Memory and RAG**: the memory store, working memory, and retrieval/embedding paths (e.g. `test_memory_store_tool.py`, `test_working_memory.py`).
- **Skills, automation, and workflows**: skill learning/tools, automation delivery, and workflow selection (e.g. `test_skills.py`, `test_automation_delivery.py`, `test_workflow_selector_extract.py`).
- **Tools and MCP**: the tool contract/registry, input repair, and MCP registration (e.g. `test_tool_contract.py`, `test_mcp_registry.py`).
- **Credentials and security**: secure/credential storage, auth middleware, and WebSocket auth (e.g. `test_secure_store.py`, `test_credential_store.py`, `test_ws_auth_failclosed.py`).
- **Core agent and integrity**: core agent capabilities and overall system health (e.g. `test_agent_features.py`, `test_integrity.py`).

Note: `list_tools_cli.py` is a helper script (it prints the CLI tool menu), not a test, so `pytest` does not collect it.

## Running Tests

VAF uses `pytest` as its primary testing framework.

```bash
# Run all tests
pytest

# Run tests with output
pytest -s

# Run specific test file
pytest tests/test_agent_features.py
```

## Writing New Tests

When adding features or fixing bugs:
- Create corresponding test files in this directory.
- Mock external API calls and heavy LLM operations where possible to keep tests fast.
- Ensure all new tests pass before submitting a pull request.
- Follow the naming convention `test_*.py` for test discovery.

## What makes a test admissible here

**A test that does not go red when you revert the fix proves nothing.** That is the house
rule, and it is the only one on this page that is not negotiable. A green suite is evidence
only about the tests that would have gone red.

This is not a local invention. The practice has a name - mutation testing - and the
literature is blunt about why coverage is not a substitute: coverage says a line was
executed, a mutation score says the execution was backed by an assertion that would have
noticed. It is the metric that correlates with real fault detection; coverage on its own
does not.

So a new test arrives with the mutation that proves it:

1. Make the fix.
2. Break it again, deliberately - remove the guard, drop the argument, flip the polarity.
3. Check that exactly the tests meant to protect it turn red.
4. If a mutation stays green, the property is unguarded. The number of tests around it is
   irrelevant.

**Before saying "green locally", run the full suite with the home directory redirected -
and the variable for that is not called the same thing everywhere.** Several code paths
write into real stores as a side effect, and a run against your own home mixes synthetic
rows into data you will later measure. On Linux and macOS: `HOME=$(mktemp -d) pytest -q`.
On Windows the equivalent is `USERPROFILE`, because `Path.home()` goes through
`os.path.expanduser`, which consults USERPROFILE and then HOMEDRIVE+HOMEPATH there and
never looks at HOME at all - so the Linux spelling of this rule silently does nothing on
Windows. The remaining store axes (the XDG names, and `%APPDATA%`/`%LOCALAPPDATA%`) are
redirected by `conftest.py` for the whole session; `tests/test_suite_writes_nowhere_real.py`
holds which mechanism governs which directory on which platform, and fails if one of them
is governed by something nothing redirects.

The failure modes below have actually happened in this repository. They are worth knowing
before writing the test, not after. (The list is deliberately not numbered in this sentence:
a count that has to be edited whenever the list grows is the same stale anchor as a number
inside a test name.)

**Stage tested, wiring not.** The commonest gap by a wide margin. The pipeline stage is
correct and fully covered, and the caller never passes it what it checks. Every test stays
green because every test drives the stage directly. Whenever a test proves that a component
behaves correctly, a second one has to prove that its caller actually reaches it that way.

**A guard that reads text instead of code.** Assertions over `inspect.getsource` or a file
grep break when code moves and pass when it is renamed. `test_markers_sync` once stayed
green while the constant it guards had moved away, because the prompt text further down the
file happened to contain the same words. A grep for `.run(**` reported zero raw dispatches
while five of the form `.run(path=...)` sat in the same file. If a test must look at source,
ask the AST, not the string - and never choose a pattern that matches only one of several
call shapes. Strip docstrings first: a comment explaining the old mechanism should not
satisfy or break a guard about the new one.

**A test that uses a precondition instead of establishing it.** It then measures whether
that precondition happens to hold. When it fails, its error text accuses the code - most
convincingly where the text is precise. A test drove a helper with a hardcoded `/tmp`; on
Windows the helper bailed out before reaching the code under test, and the failure read
"the fast path still runs invisibly", which is exactly the defect the file was written to
catch. Six other jobs were green, so it looked like a platform-specific hole in the fix
rather than a broken assumption in the test.

All three failures that CI caught in one round were this shape: a config key taken from a
real `~/.vaf`, a lock tool taken against whatever pip the runner image carried, a path taken
from the OS. `tmp_path` and an explicit pin would have established all three. Ask of every
fixture: did this test CREATE the state it depends on, or find it?

**A probe that measures nothing and reads as an acquittal.** A leak test whose fake session
started as `{}` found no leak, because the code replaces a falsy container and the probe
then inspected the wrong object. "No leak found" and "nothing was measured" look identical
from the outside. Assert on the artefact the code really writes - the file on disk, the
store, the object the caller keeps - not on the return string, which can say "denied" while
the write already happened.

**A gate with no assertion on the refusing side.** A loosening is invisible; a tightening is
noticed at once. Under a gate that is too wide everything still works, so every assertion of
the form "X gets through" stays green. Only an assertion that something is REFUSED can catch
a loosening, and nobody writes that one until the case has been named out loud.
`_ws_session_owner_ok` answered "the file exists but cannot be read" the permissive way;
eight tests covered that helper and all eight stayed green, because each of them asked
whether a legitimate caller got through. Every gate therefore needs at least one assertion
on the refusal side, and the counter-proof to run against it is always "make the doubtful
case permissive" - never the other direction, which fails on its own.

**A boundary as wide as the surface somebody enumerated, described as if it covered the
module.** The fix is correct and the sentence around it is not: "this module is contained"
is a claim about the SURFACE, while the work was done on the line that was being looked at.
Three times, same shape, three different neighbours. `write_file` was jailed and `edit_file`
- the other half of the same write surface, in the same tool list - was not. `document_viewer`
refused a path on the checked read and opened it raw again eight lines later. `cloud_storage`
got its guard on `_action_save`, the door the author was already standing in, while
`_action_retrieve` one function below took the same model-chosen path, let pathlib swallow
the base on an absolute value, and copied the result into a directory the API serves. Each
time the fix was real and the description was one size too big. So before writing that
something is contained, enumerate the doors mechanically - every entry point that accepts a
path, not the one that prompted the change - and let the count decide the sentence.

**A counter-proof built on the same mechanism as the defect.** It reproduces the failure
instead of catching it, and it does so while looking like independent confirmation. Three
times in one round: a guard whose collector parsed `name = TOOL_NAME` with
`ast.literal_eval`, silently dropped the class, and reported 31 tools where there were 32 -
then the cross-check written to catch that attributed each class to the file it was FOUND in
rather than the one that defines it, so it cleared the very tool it existed for, because the
re-exporting module happened to contain the check. And an isolation counter-proof that
cleared the child's environment from the same tuple the mutation shrinks: breaking the
isolation also stopped the environment being cleared, so the child inherited the real
setting, wrote into the real store, and left the scratch directory spotless. A green
counter-proof while the defect is actively happening is perfect camouflage. So the probe has
to come from an INDEPENDENT source: `__module__` rather than "where I found it", an explicit
list rather than the constant under test, the imported object rather than the parsed text.
Ask of every counter-proof: if the defect were present right now, which line of THIS test
would notice - and does that line share a mechanism with the thing that broke?

**Nothing measures the thing that finds the things.** Every assertion in a frozen guard is
about its members; the collector that decides who the members ARE is usually unguarded, and
it fails in the reassuring direction - a tool it cannot parse is simply absent, and absence
reads as a clean bill of health. A set is only as honest as its collector, so a guard that
enumerates needs a second, independently derived enumeration asserted against it.

**A measured number that is read as an answer to a question it did not count.** The count is
correct, the conclusion drawn from it is not, and the number's authority is what carries the
conclusion past review. "24 of 3178 sessions carry a username" was counted right, twice, and
read as "so the nameless fallback resolves to the machine owner". It counted how many sessions
have a NAME. It said nothing about who the other 3154 are - and when that was finally counted,
3208 of 3238 turned out to carry a non-owner SCOPE and none carried the owner's, so the
nameless caller was overwhelmingly a tenant. A fix built on the first reading handed those
tenants the owner's GitHub token and cloud accounts. The same shape produced "49 path literals"
as an argument about whether they get touched, which is not what a grep for literals counts. So
before a number justifies a change, say out loud what it counted and what the change assumes,
and check they are the same sentence; where they differ, the missing count is the one to run.

**A label that asserts something no test holds.** "Legacy", "deprecated", "read-only",
"historical" are all claims about the PRESENT: they say the thing is no longer produced. That
half is what nobody measures. A frozen set in `test_credential_key_baseline.py` carried the
`email:<provider>:admin:<id>` key shape as a legacy form, read but never written - and the
dispatcher's nameless-caller fallback was still writing exactly it, on every installation
whose owner had not registered under that name. The set was correct about the read path and
silently wrong about the world, and the label is what stopped anyone looking: it had already
answered the question. Same shape as the comment claiming a tool "can never scan outside the
project" while it did. So when a frozen set carries an entry as historical, "does anything
still produce this today?" is part of the assurance and belongs in an assertion - not in the
prose above it, which is where a label goes to avoid being checked.

## When to delete a test

Removing tests is a maintenance practice, not an admission of defeat, and the same rule
decides it: a test whose mutation stays green is not protection - it is runtime and false
confidence. Delete it or repair it.

Two further groups are worth removing on sight:

- **Tests that restate the implementation.** If a test has to be edited every time the
  production code is edited, it is a copy of the code rather than a statement about its
  behaviour, and it will be updated to match whatever the code now does - including a bug.
- **Scaffolding.** Tests written to drive a migration, pinned against a state that no longer
  exists. When the migration lands, the scaffolding comes down.

What is NOT a reason to delete: the suite being large. Measured against the size of `vaf/`,
this suite is small. Size is not the metric; whether each test would notice a break is.

**A standing decision, so that nobody starts a cull unprompted.** The suite has been left
as it is on purpose. Its runtime is measured and negligible, and the rule above turns every
test in it into either a guard or a candidate - decidable in a minute by one mutation, which
is cheaper than auditing thousands of them up front.

What a large suite really costs is churn: the portion of it pinned to the SHAPE of the code
rather than to its behaviour has to be edited every time the code moves. That portion is
small and identifiable - the assertions over source text - and it is the only group a sweep
would be worth aiming at.

The trigger for doing that sweep is measurable rather than aspirational: **when a refactor
costs more in updating tests than in changing the code itself.** Until that happens, apply
the rule to what you touch and leave the rest alone.

## Frozen measurements

Several tests here pin a MEASURED set rather than a written one - the tools that receive an
identity, the tools that gain one in workflows, the arguments a public class promises. They
are generated, never typed, because the one attempt to type such a list invented nine
entries and dropped five.

Two rules for them. They may only shrink without a deliberate diff: a name joining the set
is a security-relevant change and belongs in a review, not in silence. And the failure
message says what to do - which file to edit, and why the change might be correct - because
a frozen set is read by someone who did not write it.

## Dependencies

- `pytest`: Core testing framework.
- See `requirements.txt` for additional test-related dependencies.
