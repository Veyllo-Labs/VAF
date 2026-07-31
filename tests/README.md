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
