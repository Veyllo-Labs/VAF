# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The browser agent's vision tiers, its dedicated LLM lane, and the guidance.

The tier decision is the user-facing promise: a vision-capable model SEES
(use_vision auto), a text-only one with a vision_provider gets described
screenshots on demand, and a setup with no vision at all still RUNS - it is
told so instead of failing. These tests pin the decision, the config lane
that feeds it, the guidance per tier, and the wiring that carries all of it
into the browser-use Agent.
"""

from pathlib import Path

import pytest

import vaf.tools.browser_agent as ba


# ── vision tier ───────────────────────────────────────────────────────────

def test_vision_capable_model_sees(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: True)
    assert ba._browser_vision_mode("openai", "gpt-4o") == ("auto", "native")


def test_text_only_model_with_vision_provider_gets_descriptions(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    monkeypatch.setattr(ba, "_vision_available", lambda: True)
    assert ba._browser_vision_mode("deepseek", "deepseek-v4-flash") == (False, "described")


def test_no_vision_anywhere_still_runs_dom_only(monkeypatch):
    """The user's requirement, verbatim: without any vision model or API the
    run must be smart enough to continue - blind is a tier, not an error."""
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    monkeypatch.setattr(ba, "_vision_available", lambda: False)
    use_vision, tier = ba._browser_vision_mode("deepseek", "x")
    assert use_vision is False and tier == "blind"


def test_a_broken_registry_answer_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setattr(ba, "_model_supports_vision",
                        lambda p, m: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ba, "_vision_available", lambda: False)
    assert ba._browser_vision_mode("openai", "gpt-4o") == (False, "blind")


def test_a_seeing_main_model_counts_even_without_an_explicit_vision_provider(monkeypatch):
    """The tier asks the framework resolver, not the raw config key.

    A setup whose MAIN provider accepts images but that never filled in the
    optional vision_provider override used to be told it had no vision at all,
    and its runs were sent out with the blind tier's guidance - the one that
    forbids describe_page_visually. This goes through the real
    vision_infer.vision_available(), so it turns red if the lane goes back to
    reading vision_provider itself."""
    from vaf.core.config import Config
    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, k, d="": {"vision_provider": "", "vision_model": "",
                              "provider": "openai"}.get(k, d)))
    monkeypatch.setattr(Config, "load", classmethod(
        lambda cls: {"provider": "openai", "api_model_openai": "gpt-4o"}))
    assert ba._browser_vision_mode("deepseek", "deepseek-v4-flash") == (False, "described")


# ── guidance ──────────────────────────────────────────────────────────────

def test_guidance_names_the_tools_that_beat_blind_scrolling():
    for tier in ("native", "described", "blind"):
        g = ba._browser_guidance(tier)
        assert "find_text" in g and "pages=10" in g and "collect_page_text" in g


def test_guidance_is_honest_about_what_each_tier_can_see():
    assert "screenshot" in ba._browser_guidance("native")
    assert "describe_page_visually" in ba._browser_guidance("described")
    blind = ba._browser_guidance("blind")
    # The blind tier must forbid the one action that cannot answer there.
    assert "do NOT call" in blind and "describe_page_visually" in blind


def test_every_tier_is_told_the_rule_that_wasted_a_whole_run():
    """The measured failure: a run typed a value into an autocomplete, the
    widget kept its previous entry, and the model spent its step budget
    retyping into the same field. Every tier gets this, because it is a DOM
    fact and not a vision one."""
    for tier in ("native", "described", "blind"):
        g = ba._browser_guidance(tier)
        assert "autocomplete" in g and "suggestion is clicked" in g


def test_the_tiers_that_escalate_say_so():
    """A model told it must ask for a picture, in a setup that hands it one
    unasked, reasons about a lane it is not in."""
    assert "without asking" in ba._browser_guidance("native")
    assert "without asking" in ba._browser_guidance("described")
    assert "without asking" not in ba._browser_guidance("blind")


# ── the dedicated browser LLM lane ────────────────────────────────────────

def test_lane_keys_default_to_the_main_provider(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": d))
    assert ba._resolve_browser_lane() == ("", None)


def test_lane_overrides_provider_and_model(monkeypatch):
    from vaf.core.config import Config
    values = {"browser_agent_provider": "google", "browser_agent_model": "gemini-2.5-pro"}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d="": values.get(k, d)))
    assert ba._resolve_browser_lane() == ("google", "gemini-2.5-pro")


def test_bridge_uses_the_lane(monkeypatch):
    monkeypatch.setattr(ba, "_resolve_browser_lane", lambda: ("google", "gemini-2.5-pro"))
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"provider": "local", "model": "llama3"}))
    bridge = ba._build_vaf_bridge()
    assert bridge.provider == "google" and bridge.model == "gemini-2.5-pro"


def test_bridge_without_lane_keeps_the_main_provider(monkeypatch):
    monkeypatch.setattr(ba, "_resolve_browser_lane", lambda: ("", None))
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"provider": "local", "model": "llama3"}))
    bridge = ba._build_vaf_bridge()
    assert bridge.provider == "local" and bridge.model == "llama3"


def test_lane_keys_are_registered_everywhere():
    """Rule 2: a config key exists in DEFAULTS, in the schema doc, and the
    doc's key-count line matches - or the registry copies have drifted."""
    from vaf.core.config import Config
    assert "browser_agent_provider" in Config.DEFAULTS
    assert "browser_agent_model" in Config.DEFAULTS
    schema = (Path(__file__).resolve().parents[1] / "docs" / "setup"
              / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`browser_agent_provider`" in schema
    assert "`browser_agent_model`" in schema
    assert f"({len(Config.DEFAULTS)} keys)" in schema


# ── wiring into browser-use ───────────────────────────────────────────────

def test_the_agent_gets_the_tier_not_a_hardcoded_false():
    """use_vision=False was the measured root of blind scrolling; the Agent
    must receive the computed mode and the guidance, or every tier above
    'blind' exists only on paper."""
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    body = src.split("async def _run_browser", 1)[1]
    # ignore comment lines: the tier explanation names the literal on purpose
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "use_vision=False" not in code
    assert "use_vision=_use_vision" in body
    assert "extend_system_message=_browser_guidance(_vision_tier)" in body
    # The escalation only exists if the hook is actually handed to run(): the
    # tier alone decides nothing once the model stops asking for pictures.
    assert "on_step_start=_make_look_when_stuck(_vision_tier)" in body


def test_collect_page_text_is_a_registered_action():
    # browser-use carries a python_version >= "3.11" marker in requirements,
    # so on the 3.10 CI runner it simply is not installed - building the
    # controller there is an ImportError, not a finding (shipped once: the
    # only red of an otherwise green 3.10 run). Locally the same shape is
    # pinned by scripts/hostile_env.py hiding browser_use.
    pytest.importorskip("browser_use", exc_type=ImportError)
    controller = ba._build_browser_controller()
    names = list(controller.registry.registry.actions.keys())
    assert "collect_page_text" in names
    assert "describe_page_visually" in names   # the vision lane it complements


# ── the vision call goes through the framework's choke point ──────────────

def _record_vision(monkeypatch, reply="a form with two fields"):
    """Replace the primitive itself. The lane imports it per call, so patching
    the module attribute is what the running code resolves - and a lane that
    stopped calling it would leave `seen` empty, which every test below asserts
    against so that a bare `return None` cannot pass for a working reroute."""
    seen = {"called": 0, "reply": reply}

    def fake(images, prompt, *, max_tokens=1024, temperature=0.2):
        seen["called"] += 1
        seen["images"] = images
        seen["prompt"] = prompt
        seen["max_tokens"] = max_tokens
        seen["temperature"] = temperature
        return seen["reply"]

    import vaf.core.vision_infer as vi
    monkeypatch.setattr(vi, "vision_infer", fake)
    return seen


def test_a_screenshot_is_described_through_vision_infer(monkeypatch):
    """Not a style preference: the primitive is what applies the configured
    backend cascade, the downscale and the error-sentinel guard. A lane that
    calls a provider itself has none of them - measured, it had none."""
    seen = _record_vision(monkeypatch)
    out = ba._call_vision_for_screenshot(b"\xff\xd8\xff-not-really-a-jpeg")
    assert out == "a form with two fields"
    assert seen["images"][0]["data"] == b"\xff\xd8\xff-not-really-a-jpeg"
    assert seen["temperature"] == 0.1 and seen["max_tokens"] == 512


def test_a_failed_vision_call_is_no_description(monkeypatch):
    """The old copy appended every streamed chunk, so a provider's
    '[API Error from ...]' sentinel became the page description. The primitive
    answers None; this lane must pass that through - and must still have ASKED
    it, which is what `called` pins (a lane hardcoded to None would not)."""
    seen = _record_vision(monkeypatch, reply=None)
    assert ba._call_vision_for_screenshot(b"x") is None
    assert ba._call_vision_for_captcha(b"x", "buses") is None
    assert seen["called"] == 2


def test_vision_failure_never_escapes_into_the_run(monkeypatch):
    import vaf.core.vision_infer as vi
    monkeypatch.setattr(vi, "vision_infer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ba._call_vision_for_screenshot(b"x") is None


def test_both_screenshot_shapes_survive_the_real_accessor():
    """The lane holds raw JPEG bytes from take_screenshot, and browser-use hands
    the bridge a data: URL. Both must come out of the framework's own accessor
    correctly typed: declaring image/jpeg for PNG bytes is rejected by a
    provider that validates media_type against the payload."""
    from vaf.core.vision_infer import image_to_b64
    assert image_to_b64(ba._shot_image(b"ABC")) == ("QUJD", "image/jpeg")
    assert image_to_b64(ba._shot_image("data:image/png;base64,QUJD")) == ("QUJD", "image/png")


# ── the bridge must hand over the picture, not a picture of a picture ─────

def test_the_bridge_extracts_the_real_url_from_browser_uses_own_type():
    """browser-use's ImageURL is a pydantic model whose __str__ is a LOG line
    that hides the payload, so str() on it does not fail - it silently replaces
    the screenshot with the words 'Image[image/png, detail=auto]'. Every picture
    the lane ever obtained went through this, which is why a model that could
    see still could not see."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

    url = "data:image/png;base64,iVBORw0KGgo="
    msg = UserMessage(content=[
        ContentPartTextParam(text="what is on this page"),
        ContentPartImageParam(image_url=ImageURL(url=url, media_type="image/png")),
    ])
    bridge = ba.VAFLLMBridge(model="gpt-4o", provider_name="openai")
    out = bridge._to_dicts([msg])
    blocks = out[0]["content"]
    images = [b for b in blocks if b.get("type") == "image_url"]
    assert images and images[0]["image_url"]["url"] == url


def test_the_bridge_drops_an_unusable_image_instead_of_describing_it(monkeypatch):
    """A shape we cannot read is no image. Passing its repr on would send a
    vision model a sentence about a screenshot and bill it as a look."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    seen = _record_vision(monkeypatch)

    class _Part:
        type = "image_url"
        image_url = object()

    class _Msg:
        role = "user"
        content = [_Part()]

    monkeypatch.setattr(ba, "_model_supports_vision", lambda p, m: False)
    bridge = ba.VAFLLMBridge(model="deepseek-chat", provider_name="deepseek")
    out = bridge._to_dicts([_Msg()])
    assert seen["called"] == 0
    assert "Image[" not in out[0]["content"]


# ── a stuck run is shown the page instead of told about it ────────────────

class _FakeDetector:
    def __init__(self, stagnant=0, repeated=0):
        self.consecutive_stagnant_pages = stagnant
        self.max_repetition_count = repeated


class _FakeState:
    def __init__(self, failures=0, stagnant=0, repeated=0, last_result=None, n_steps=1):
        self.consecutive_failures = failures
        self.loop_detector = _FakeDetector(stagnant, repeated)
        self.last_result = last_result
        self.n_steps = n_steps


class _FakeAgent:
    def __init__(self, **kw):
        self.state = _FakeState(**kw)
        self.browser_session = None


def test_the_fakes_match_the_real_browser_use_state():
    """A fake with the wrong attribute names would make every test below vacuous
    while the real run never escalates."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    from browser_use.agent.views import ActionLoopDetector, AgentState
    real_state, real_det = AgentState(), ActionLoopDetector()
    for attr in ("consecutive_failures", "last_result", "n_steps", "loop_detector"):
        assert hasattr(real_state, attr), attr
    for attr in ("consecutive_stagnant_pages", "max_repetition_count"):
        assert hasattr(real_det, attr), attr


def test_progress_is_not_stuck():
    assert ba._stuck_reason(_FakeAgent()) == ""
    assert ba._stuck_reason(_FakeAgent(stagnant=2, repeated=3)) == ""


def test_the_thresholds_are_the_ones_that_beat_the_librarys_own_nudge():
    """Pinned as literals, not read back from the constants: browser-use nudges
    with text at 5, and a run that has repeated an action five times has already
    spent five steps not looking. Raising these past 5 would hand the problem
    back to the text that did not solve it."""
    assert ba._STUCK_STAGNANT_PAGES == 3 and ba._STUCK_REPEATED_ACTIONS == 4
    assert "failed action" in ba._stuck_reason(_FakeAgent(failures=1))
    assert "page has not changed" in ba._stuck_reason(_FakeAgent(stagnant=3))
    assert "repeated" in ba._stuck_reason(_FakeAgent(repeated=4))


def test_a_missing_counter_is_not_stuck():
    """Defensive across browser-use versions: no counter means no escalation,
    never an exception inside a hook whose raise aborts the whole run."""
    assert ba._stuck_reason(object()) == ""


def test_a_blind_run_gets_no_hook():
    assert ba._make_look_when_stuck("blind") is None


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_stuck_native_run_gets_the_picture_it_never_asked_for():
    """use_vision='auto' attaches nothing until the model asks, and a looping
    model does not ask. The escalation uses browser-use's own channel: the
    metadata flag its screenshot action sets."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    agent = _FakeAgent(stagnant=3)
    _run(ba._make_look_when_stuck("native")(agent))
    assert agent.state.last_result, "nothing was injected - the run stays blind"
    note = agent.state.last_result[-1]
    assert note.metadata == {"include_screenshot": True}
    assert "autocomplete" in (note.extracted_content or "")
    assert agent._vaf_forced_looks == 1


def test_the_note_travels_on_the_channel_that_survives_a_lost_model_output():
    """The message manager throws away every action result of a step whose LLM
    call produced no output, and replaces the history item with a bare format
    error - and that step is one of the triggers here. extracted_content with
    include_extracted_content_only_once is read out before that decision, into
    read_state, so the note still reaches the prompt."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    agent = _FakeAgent(failures=1)
    _run(ba._make_look_when_stuck("native")(agent))
    note = agent.state.last_result[-1]
    assert note.include_extracted_content_only_once is True
    assert note.extracted_content and not note.long_term_memory


def test_a_progressing_native_run_pays_for_nothing():
    pytest.importorskip("browser_use", exc_type=ImportError)
    agent = _FakeAgent()
    _run(ba._make_look_when_stuck("native")(agent))
    assert not agent.state.last_result


def test_the_look_does_not_repeat_on_every_step_of_one_stuck_stretch():
    """max_repetition_count is a MAXIMUM over a rolling 20-action window: it
    does not fall back when the run recovers, it decays as the repeated actions
    age out. Without the cooldown one stuck moment would attach a screenshot to
    each of the next ~20 steps - the exact cost use_vision='auto' avoids."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    hook = ba._make_look_when_stuck("native")
    agent = _FakeAgent(repeated=9, n_steps=1)
    _run(hook(agent))
    assert agent._vaf_forced_looks == 1

    for step in range(2, 2 + ba._LOOK_COOLDOWN_STEPS - 1):
        agent.state.n_steps = step
        agent.state.last_result = None
        _run(hook(agent))
        assert agent._vaf_forced_looks == 1, f"looked again at step {step}"

    agent.state.n_steps = 1 + ba._LOOK_COOLDOWN_STEPS
    _run(hook(agent))
    assert agent._vaf_forced_looks == 2, "still stuck after the cooldown: look again"


def test_an_existing_result_is_appended_to_not_replaced():
    """browser-use's _finalize returns early on a falsy last_result; replacing
    the list instead of appending would stall the step counter."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    from browser_use.agent.views import ActionResult
    first = ActionResult(long_term_memory="the step's own result")
    agent = _FakeAgent(failures=1, last_result=[first])
    _run(ba._make_look_when_stuck("native")(agent))
    assert agent.state.last_result[0] is first
    assert len(agent.state.last_result) == 2


def test_a_stuck_described_run_gets_the_description_not_the_flag(monkeypatch):
    """A text-only lane model cannot receive an image at all, and the metadata
    flag is inert while use_vision is False - so that tier is handed the text.
    The screenshot it is handed must be the one just taken from the page."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    took = {}

    class _Session:
        async def take_screenshot(self, **kw):
            took["kw"] = kw
            return b"the-live-page"

    monkeypatch.setattr(ba, "_call_vision_for_screenshot",
                        lambda shot: f"court field showing Gelsenkirchen [{shot!r}]")
    agent = _FakeAgent(failures=2)
    agent.browser_session = _Session()
    _run(ba._make_look_when_stuck("described")(agent))
    note = agent.state.last_result[-1]
    assert note.metadata is None
    assert "Gelsenkirchen" in note.extracted_content
    assert "the-live-page" in note.extracted_content, "described a screenshot nobody took"
    assert took["kw"].get("format") == "jpeg"


def test_a_described_run_without_a_backend_injects_nothing(monkeypatch):
    pytest.importorskip("browser_use", exc_type=ImportError)

    class _Session:
        async def take_screenshot(self, **kw):
            return b"jpegbytes"

    monkeypatch.setattr(ba, "_call_vision_for_screenshot", lambda shot: None)
    agent = _FakeAgent(failures=2)
    agent.browser_session = _Session()
    _run(ba._make_look_when_stuck("described")(agent))
    assert not agent.state.last_result


def test_a_dead_browser_does_not_abort_the_run():
    """on_step_start is awaited inside run()'s try: an exception there kills the
    whole browser task. This drives the real failure - a screenshot on a browser
    that died mid-run - not a shortcut that returns before the risky part."""
    pytest.importorskip("browser_use", exc_type=ImportError)

    class _DeadSession:
        async def take_screenshot(self, **kw):
            raise RuntimeError("CDP connection closed")

    agent = _FakeAgent(failures=1)
    agent.browser_session = _DeadSession()
    _run(ba._make_look_when_stuck("described")(agent))   # must not raise
    assert not agent.state.last_result


def test_a_hook_that_hangs_does_not_hang_the_run():
    """The hook runs OUTSIDE browser-use's per-step timeout, so nothing else
    bounds it: an unanswered CDP screenshot would stall the run forever."""
    pytest.importorskip("browser_use", exc_type=ImportError)
    assert ba._LOOK_BUDGET_SECONDS > 0

    class _HangingSession:
        async def take_screenshot(self, **kw):
            import asyncio as _a
            await _a.sleep(3600)

    import asyncio as _a
    agent = _FakeAgent(failures=1)
    agent.browser_session = _HangingSession()
    hook = ba._make_look_when_stuck("described")

    async def _drive():
        # The real budget is minutes; shrink it for the test, then assert the
        # hook returned rather than waited.
        ba._LOOK_BUDGET_SECONDS = 0.05
        try:
            await _a.wait_for(hook(agent), timeout=5)
        finally:
            ba._LOOK_BUDGET_SECONDS = 45.0

    _run(_drive())
    assert not agent.state.last_result


def test_the_dock_reports_a_forced_look():
    """The window says 'auto' for a run that looks on demand; a run that was
    made to look has looked, and a run that cannot see says so."""
    agent = _FakeAgent()
    agent._vaf_vision_tier = "blind"
    assert ba.BrowserAgentTool._build_browser_state(agent, "t", "u", 5)["vision"] == "aus"
    agent._vaf_vision_tier = "native"
    assert ba.BrowserAgentTool._build_browser_state(agent, "t", "u", 5)["vision"] == "auto"
    agent._vaf_forced_looks = 1
    assert ba.BrowserAgentTool._build_browser_state(agent, "t", "u", 5)["vision"] == "aktiv"


def test_the_tier_is_recorded_on_the_run_it_describes():
    """The dock reader above is only true if something writes the tier. This is
    the sole assignment in the tree, and a source scan is the only seam: the
    Agent is built deep inside _run_browser behind a live browser."""
    src = (Path(__file__).resolve().parents[1] / "vaf" / "tools"
           / "browser_agent.py").read_text(encoding="utf-8")
    body = src.split("async def _run_browser", 1)[1]
    assert "agent._vaf_vision_tier = _vision_tier" in body
