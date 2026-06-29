# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Main-loop runaway guards.

A slow verify/retry loop (the create_automation "zombie": the agent kept calling
list_automations/read_automation after the work was already done, grinding toward the 75-turn cap on a
slow reasoning provider) must terminate quickly. The existing guards missed it because the calls were
slow (not <5s), not bookkeeping tools, and had slightly different args. These tests pin the new
no-progress guard (force-stops a read/verify-only loop within a bounded number of turns), the
create_automation workflow-gen skip for simple prompts, and the loop-protection WIRING in chat_step.
"""
from vaf.core.agent import Agent, _is_nonprogress_tool
from vaf.tools.automation import AutomationTool


def _bare() -> Agent:
    a = Agent.__new__(Agent)
    a._nonprogress_streak = 0
    return a


# --- _is_nonprogress_tool: read/verify vs progress ------------------------------------------------

def test_is_nonprogress_tool_classification():
    for t in ("list_automations", "read_automation", "list_calendar_events", "mail_inbox",
              "get_weather", "read_file", "list_foo"):
        assert _is_nonprogress_tool(t) is True, t
    # web_search / memory_search are genuine gathering, NOT verification — they must reset the streak,
    # so a legitimately search-heavy turn is never throttled (the wall-clock still backstops a search spin).
    for t in ("web_search", "memory_search", "create_automation", "update_automation",
              "delete_automation", "write_file", "coding_agent", "send_telegram",
              "update_working_memory", "execute_workflow"):
        assert _is_nonprogress_tool(t) is False, t


# --- the runaway-stop proof: a verify-only loop is force-stopped within a bounded number of turns ---

def test_nonprogress_guard_force_stops_verify_loop():
    a = _bare()
    nudged_at = None
    forced_at = None
    for i in range(1, 30):
        msg, force = a._nonprogress_step("list_automations")
        if msg and not force and nudged_at is None:
            nudged_at = i
        if force:
            forced_at = i
            break
    # default nonprogress_max_turns=6: nudge at 6, hard force (tools off) at 8 — the loop can NEVER
    # grind forever; it is stopped within a small bounded number of read/verify turns.
    assert nudged_at == 6
    assert forced_at == 8


def test_nonprogress_guard_resets_on_progress_tool():
    a = _bare()
    for _ in range(5):
        a._nonprogress_step("list_automations")
    assert a._nonprogress_streak == 5
    a._nonprogress_step("create_automation")   # a real action resets the streak
    assert a._nonprogress_streak == 0
    # legit varied work (list -> create -> read -> ...) therefore never trips the guard
    msg, force = a._nonprogress_step("read_automation")
    assert msg is None and force is False and a._nonprogress_streak == 1


def test_nonprogress_guard_off_in_thinking_mode(monkeypatch):
    monkeypatch.setenv("VAF_THINKING_MODE", "1")
    a = _bare()
    for _ in range(12):
        msg, force = a._nonprogress_step("list_automations")
        assert msg is None and force is False
    assert a._nonprogress_streak == 0


def test_nonprogress_threshold_configurable(monkeypatch):
    from vaf.core.config import Config
    orig = Config.get  # bound classmethod; still callable after the patch
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: 3 if key == "nonprogress_max_turns" else orig(key, default)
    ))
    a = _bare()
    res = [a._nonprogress_step("read_automation") for _ in range(6)]
    nudges = [m for m, _ in res]
    forces = [f for _, f in res]
    assert nudges[2] is not None        # nudge at the 3rd consecutive read/verify call
    assert forces[4] is True            # forced (tools off) at 3+2 = the 5th


def test_nonprogress_disabled_via_config(monkeypatch):
    from vaf.core.config import Config
    orig = Config.get  # bound classmethod; still callable after the patch
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: False if key == "anti_spin_enabled" else orig(key, default)
    ))
    a = _bare()
    for _ in range(12):
        msg, force = a._nonprogress_step("list_automations")
        assert msg is None and force is False


# --- create_automation: skip the slow LLM workflow-gen for simple prompts ----------------------------

def test_prompt_needs_workflow_gen():
    f = AutomationTool._prompt_needs_workflow_gen
    # simple "tell me X / remind me daily" -> NO heavyweight generation (runs prompt-based)
    assert f("Sag mir jeden Tag den Wetterbericht für Berlin") is False
    assert f("Tell me the current price every day") is False
    assert f("Erinnere mich täglich ans Wasser trinken") is False
    assert f("") is False
    # multi-tool / file / explicit numbered steps -> needs deterministic generation
    assert f("Suche per web_search und erstelle eine HTML-Datei und speichere sie") is True
    assert f("1) web_search 2) coding_agent 3) write_file") is True
    assert f("Generate a report and open it in the document_viewer") is True


# --- wiring regression guard: the loop-protection hooks must stay wired into chat_step --------------

def test_loop_protection_wiring_present():
    import inspect
    import vaf.core.agent as _ag
    src = inspect.getsource(_ag)
    # wall-clock backstop is initialised + enforced in the main loop
    assert "_turn_deadline = time.monotonic()" in src
    assert "Wall-clock stop" in src
    # no-progress guard is actually called at the tool-execution site
    assert "_nonprogress_step(function_name)" in src


def test_create_automation_terminal_signal_present():
    import inspect
    import vaf.tools.automation as _auto
    src = inspect.getsource(_auto.AutomationTool.run)
    # the success result tells the agent to stop verifying (the create_automation "zombie" fix)
    assert "No further action needed" in src


def test_create_automation_no_false_creation_claim():
    """The 'similar automation at a different time' path used to say it was creating ('wird erstellt')
    but returned WITHOUT creating — the agent then told the user a second automation was made. The fix
    returns a truthful 'nothing created, ask the user' prompt and only creates on confirm_duplicate."""
    import inspect
    import vaf.tools.automation as _auto
    src = inspect.getsource(_auto.AutomationTool.run)
    # the lying messages are gone
    assert "wird die neue Automatisierung erstellt" not in src
    assert "wird fortgesetzt" not in src
    # truthful decision prompt + explicit-confirmation path present
    assert "Noch NICHTS erstellt" in src
    assert "confirm_duplicate" in src


def test_create_automation_confirm_duplicate_in_schema():
    from vaf.tools.automation import AutomationTool
    props = AutomationTool.parameters["properties"]
    assert "confirm_duplicate" in props
    assert props["confirm_duplicate"]["type"] == "boolean"
    assert "confirm_duplicate" not in AutomationTool.parameters.get("required", [])


# --- automation output filename: never "File name too long" (Errno 36) -----------------------------

def test_safe_filename_stem_bounds_long_names():
    from vaf.core.automation import _safe_filename_stem
    # an automation 'name' is often the whole prompt -> must be slugified + bounded
    long = ("Sage mir per WebUI den aktuellen Preis der SpaceX-Aktie (Hinweis: SpaceX ist nicht "
            "börsennotiert – falls kein Preis verfügbar, melde das und gib stattdessen ...) ") * 3
    stem = _safe_filename_stem(long)
    assert len(stem) <= 50
    for bad in ("/", " ", "(", ")", "–", ":"):
        assert bad not in stem
    # a real filename built from it stays well under the OS path-component limit (255 bytes)
    assert len(f"{stem}_2026-06-29.html".encode("utf-8")) < 200
    # empty / whitespace -> safe fallback
    assert _safe_filename_stem("") == "automation"
    assert _safe_filename_stem("   ") == "automation"
    # normal names keep their words (umlauts preserved), spaces -> underscores
    assert _safe_filename_stem("Wetter Bericht") == "Wetter_Bericht"
