# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""tool_result_is_error (vaf/core/context.py) is THE single source of truth for
"did this tool call fail" - shared by the per-turn summarizer and the tool_end
web event. Incident cyan123670 showed a missed prefix lets a weak model report
a failed write as success; a follow-up adversarial review of that very fix then
found the detector covered only a handful of the failure-string shapes tools
actually return (a repo-wide sweep of vaf/tools/*.py found ~30 more shipping
families). This file pins one representative REAL string per family, plus
success-string guards from the same tool families so broadening the net never
starts flagging green results.

Every failure example below is copied from (or minimally shortened from) an
actual return statement in the named source file, not invented.
"""
from vaf.core.context import tool_result_is_error


def _fails(s):
    assert tool_result_is_error(s), f"should be flagged as FAILURE: {s!r}"


def _ok(s):
    assert not tool_result_is_error(s), f"should be OK, was flagged: {s!r}"


# --- Tier: bracket/tag gate markers (tool did NOT run / was stopped) -----

def test_host_bash_gate_markers():
    _fails("[BLOCKED] Command contains a forbidden pattern: rm -rf")          # host_bash.py
    _fails("[HOST] Command timed out after 60s")                              # host_bash.py
    _fails("[SECURITY] path escapes the workspace")                           # shared gates
    _fails("[CANCELLED] user stopped the run")
    _fails("[CONFIRM REQUIRED] This command needs interactive confirmation")
    _fails("[AWAITING USER] waiting for a decision before continuing")
    _fails("[TOOL BLOCKED] policy denies python_exec here")
    _fails("[PLAN REQUIRED] set your approach first")


def test_librarian_and_warn_markers():
    _fails("[LIBRARIAN_ERROR] source fetch failed")                           # librarian.py
    _fails("[WARN] model output was empty, nothing saved")


def test_python_exec_banner_then_error_marker():
    # python_exec prepends a host-execution banner BEFORE its own [ERROR]
    # marker, defeating a prefix-only anchor.
    _fails(
        "Running on HOST python (unsandboxed).\n"
        "[ERROR] (exit=1)\nTraceback would follow here"
    )


# --- Tier: filesystem path-safety gate + wrappers ------------------------

def test_filesystem_path_safety_family():
    _fails("Access denied: Path is outside allowed directories")              # filesystem.py
    _fails("Invalid path: contains traversal")                                # filesystem.py
    _fails("Source Error: Access denied: outside workspace")                  # move/copy wrapper
    _fails("Dest Error: Invalid path")                                        # move/copy wrapper
    _fails("Edit failed: old_string not found in file")                       # filesystem.py


# --- Tier: refusal / precondition openers ---------------------------------

def test_refusal_openers():
    _fails("Cannot delete: the file is a protected system path")
    _fails("Refused: sending to this channel is not allowed for automations")
    _fails("Blocked: attachment type not permitted")                          # learn_attached_knowledge.py
    _fails("Missing required parameters: 'path' and 'content'")


# --- Tier: messaging/contacts "unavailable" family ------------------------

def test_unavailable_family():
    _fails("Telegram unavailable: connection refused")                        # messaging tools
    _fails("Contacts unavailable: no address book connected")
    _fails("Vision is unavailable (no API backend and no local mmproj)")      # vision.py


def test_send_firewall_and_auth():
    _fails("Message was blocked (contained internal system content). "
           "Send a clean user-facing message without any internal context markers.")
    _fails("Authentication failed for gdrive. Reconnect the account in Settings.")


# --- Tier: "<Verb> failed: reason" family ---------------------------------

def test_verb_failed_colon_family():
    _fails("Screenshot failed: page did not load within 30s")                 # browser tools
    _fails("Download failed: HTTP 404")
    _fails("Git commit failed: nothing staged")


# --- Tier: sandbox/test-runner outcomes ------------------------------------

def test_sandbox_test_runner_family():
    _fails("Tests failed (3 of 14).")                                         # sandbox_test_runner.py
    _fails("Test run timed out after 300s")
    _fails("Docker image missing.\nCannot run tests: sandbox unavailable")


# --- Tier: MCP / integration error idioms ----------------------------------

def test_mcp_and_integration_idioms():
    _fails("MCP Error: server 'files' returned invalid response")             # mcp_client.py
    _fails("HTTP Error: 502 from upstream")                                   # mcp_client.py
    _fails("Not connected. Link your account in Settings first.")             # integrations
    _fails("No calendar account connected.")
    _fails("No GitHub account linked for this user.")
    _fails("No WhatsApp contact found matching 'Bob'")
    _fails("Could not schedule reminder: quiet hours active and no override")


def test_permanently_unimplemented_stubs():
    _fails("Editing events is not yet supported for CalDAV calendars.")
    _fails("This provider is not implemented yet.")


# --- Tier: the two agent.py soft-block nudges (were unmarked freeform) -----

def test_thinking_soft_block_nudges_are_flagged():
    """The thinking read-cap returns its block string AS the tool result; the
    per-turn summarizer must not label that blocked call OK. The two nudge
    strings in agent.py now lead with [BLOCKED] exactly for this detector."""
    from vaf.core.agent import _PROACTIVE_DECIDE_NUDGE
    _fails(_PROACTIVE_DECIDE_NUDGE.format(fn="memory_search"))
    _fails(
        "[BLOCKED] Gathering is disabled right now, you must resolve the open item. Do "
        "NOT call memory_search. Call ask_user(message=..., source_note_id=...) or "
        "delete_automation_note(note_id=...) now."
    )
    # The third (most common) soft-block in the same lane: the ordinary
    # over-cap message, hit in every capped thinking run, not only forced nodes.
    _fails(
        "[BLOCKED] You have already called memory_search 3 times this run. Stop "
        "gathering, you have enough context. ACT on what you already have (handle "
        "the open note/todo, or ask one specific question), or call thinking_done."
    )


# --- False-positive guards: real success strings from the SAME families ----

def test_success_strings_from_the_same_tool_families_stay_ok():
    _ok("Saved: /home/user/report.html (2.1 KB)")                             # filesystem write
    _ok("Copied file.txt -> backup/file.txt")                                 # move/copy success
    _ok("Message sent to the user via Telegram.")                             # messaging success
    _ok("All 14 tests passed.")                                               # test-runner success
    _ok("Committed 3 files on main (a1b2c3d).")                               # git success
    _ok("Screenshot saved to output/page.png")                                # browser success
    _ok("Connected. 2 MCP servers available: files, search.")                 # mcp success
    _ok("Event created: Team sync, tomorrow 10:00.")                          # calendar success
    _ok("Found 3 WhatsApp contacts matching 'An': Ana, Andre, Anne")          # contacts success


def test_prose_mentioning_failure_words_stays_ok():
    # Ordinary successful output that merely TALKS about errors/blocking.
    _ok("No errors found in the document.")
    _ok("### Web Search Results\n1. Why deployments failed in 2024 - a retrospective")
    _ok("The article explains how the request was blocked by the firewall and how to fix it.")
    _ok("Summary: the test suite previously failed nightly; the flaky test was quarantined.")
    _ok("Note: 'cannot' appears 12 times in the transcript.")


def test_prefixes_are_anchored_not_substrings():
    # "cannot " / "blocked:" style openers must only fire at the START.
    _ok("The user said they cannot make it on Friday; rescheduled to Monday.")
    _ok("Report: 3 requests blocked: see the audit log for details. All handled.")


def test_non_string_and_empty_inputs():
    _ok("")
    assert not tool_result_is_error(None)
    assert not tool_result_is_error(42)
    assert not tool_result_is_error({"status": "error"})


# ── Content-carrying results (adversarial review of the first expansion) ────
# read_file / web fetches / chat+mail reads return ARBITRARY text as a
# SUCCESSFUL result. The first version of this expansion scanned the whole
# content with free substrings (" failed:", "unavailable:", "not connected",
# ...) and misclassified 10/10 realistic successful reads as failures - the
# exact mirror image of the incident the detector fixes. Everything below is
# a SUCCESS carrying failure-ish CONTENT and must stay green.

def test_read_file_of_a_log_mentioning_failures_is_ok():
    log = (
        "2026-07-17 09:00:01 db reconnect: connection failed: timeout\n"
        "2026-07-17 09:00:04 retry ok\n"
        "2026-07-17 09:01:12 service unavailable: 503 from upstream\n"
        "2026-07-17 09:01:13 recovered\n"
    ) * 20
    _ok(log)


def test_web_results_quoting_failure_titles_are_ok():
    _ok("### Web Search Results\n1. Why the download failed: a post-mortem\n"
        "2. Kubernetes: what 'not connected' really means\n"
        "3. Fixing 'Authentication failed' in CI pipelines")


def test_read_mail_or_chat_content_mentioning_blocks_is_ok():
    _ok("From: ops@example.com\nSubject: incident review\n\n"
        "The message was blocked by the spam filter yesterday; after we fixed "
        "the SPF record everything went through. MCP Error: codes are attached "
        "in the log excerpt below for reference.\n" + ("log line\n" * 40))


def test_long_document_mentioning_unsupported_features_is_ok():
    # The stub-tool belt ("is not yet supported") is gated on SHORT results;
    # a long document that contains the sentence is content, not an outcome.
    _ok("# Product FAQ\n\nExporting to PDF is not yet supported in the beta.\n"
        + ("More documentation text here. " * 30))


def test_program_output_printing_error_markers_deep_in_content_is_ok():
    # python_exec success whose program PRINTED an error-shaped marker beyond
    # the 200-char head window (e.g. while testing its own error handling).
    _ok("test run summary\n" + ("case passed\n" * 30)
        + "[ERROR] (exit=1) <- expected string asserted by the test itself\n")
