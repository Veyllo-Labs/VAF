"""Reply-outcome classification: when the user answers a proactive question, the main agent only
captures the exchange (status 'replied'); the NEXT thinking run classifies the outcome from the triple
{question, user reply, the main agent's own reply} via an LLM call — replacing the old `_is_refusal`
keyword guess. ACCEPTED -> done, DECLINED -> declined (+ declined-log), UNCLEAR -> reopen for a soft
reconfirm. Storage is isolated per test to a tmp vaf_dir/data_dir; the LLM call is stubbed."""
import vaf.core.thinking_requests as tr
import vaf.core.thinking_mode as tm
from vaf.core.platform import Platform


class _FakeAgent:
    """Stands in for the run's Agent: its _generate_for_classification returns a canned string."""
    def __init__(self, reply):
        self._reply = reply

    def _generate_for_classification(self, prompt):
        return self._reply


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "vaf_dir", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path))


def _replied(scope, question="Automate tests?", user="hm", main="ok", action="create automation: X"):
    e = tr.add_request(scope, question, run_seq=1, proposed_action=action)
    tr.record_reply(scope, e["id"], user_reply=user)
    tr.record_reply(scope, e["id"], main_reply=main)
    return e["id"]


# ── parse layer ───────────────────────────────────────────────────────────────

def test_classify_outcome_parses_keywords():
    a = _FakeAgent("ACCEPTED")
    assert tm._classify_reply_outcome(a, "q", "x", "ja", "done") == "ACCEPTED"
    assert tm._classify_reply_outcome(_FakeAgent("DECLINED"), "q", "x", "nee", "ok") == "DECLINED"


def test_classify_outcome_lenient_first_token_wins():
    # A reasoning model may emit <think> first, or wrap the verdict in a sentence.
    a = _FakeAgent("<think>the user said nee</think> DECLINED")
    assert tm._classify_reply_outcome(a, "q", "x", "nee", "ok") == "DECLINED"
    b = _FakeAgent("The outcome here is ACCEPTED, clearly.")
    assert tm._classify_reply_outcome(b, "q", "x", "ja", "erledigt") == "ACCEPTED"


def test_classify_outcome_empty_or_garbage_defaults_unclear():
    assert tm._classify_reply_outcome(_FakeAgent(""), "q", "x", "hm", "...") == "UNCLEAR"
    assert tm._classify_reply_outcome(_FakeAgent("banana"), "q", "x", "hm", "...") == "UNCLEAR"


# ── status mapping ────────────────────────────────────────────────────────────

def test_decline_maps_to_declined_and_writes_log(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "c-dec"
    rid = _replied(scope, user="bin noch am umbau aber danke", main="Alles gut, kommt wieder.")
    tm._classify_replied_requests(_FakeAgent("DECLINED"), scope)
    assert tr.get_request(scope, rid)["status"] == "declined"
    # declined -> not an open follow-up anymore
    assert tr.get_open_proactive_request(scope, current_run_seq=1) is None
    # the declined-questions dedup log was written (drives the next run's "do not ask" prompt)
    prompt = tm._get_declined_questions_prompt(scope)
    assert "Automate tests?" in (prompt or "")


def test_accept_maps_to_done_and_marks_source_handled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import vaf.core.automation_planner as ap
    scope = "c-acc"
    note = ap.add_note(scope, "run tests by hand every night", title="tests")
    e = tr.add_request(scope, "Automate tests?", run_seq=1, proposed_action="create automation",
                       source_note_id=note["id"])
    tr.record_reply(scope, e["id"], user_reply="ja mach das")
    tr.record_reply(scope, e["id"], main_reply="Erledigt, ich richte das ein.")
    tm._classify_replied_requests(_FakeAgent("ACCEPTED"), scope)
    assert tr.get_request(scope, e["id"])["status"] == "done"
    assert ap.list_notes(scope) == []  # source note marked handled -> hidden


def test_unclear_reopens_for_reconfirm_and_becomes_open_followup(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "c-unc"
    rid = _replied(scope, user="warum fragst du?", main="Naja, du machst das oft manuell.")
    tm._classify_replied_requests(_FakeAgent("UNCLEAR"), scope)
    e = tr.get_request(scope, rid)
    assert e["status"] == "asked" and e["needs_reconfirm"] is True
    openq = tr.get_open_proactive_request(scope, current_run_seq=1)
    assert openq and openq["id"] == rid   # the reopened request is this run's follow-up


def test_reconfirm_fires_at_most_once(monkeypatch, tmp_path):
    """First UNCLEAR -> reconfirm (reopen). A second UNCLEAR on the now-reconfirmed request must NOT loop
    -> it resolves to declined (we never pester forever, never auto-act on an unconfirmed proposal)."""
    _isolate(monkeypatch, tmp_path)
    scope = "c-once"
    rid = _replied(scope, user="hm", main="...")
    tm._classify_replied_requests(_FakeAgent("UNCLEAR"), scope)
    e = tr.get_request(scope, rid)
    assert e["status"] == "asked" and e["needs_reconfirm"] is True and e["reconfirmed"] is True
    # The user answers the reconfirm ambiguously again -> 'replied' -> classified once more.
    tr.record_reply(scope, rid, user_reply="weiß nicht", main_reply="ok")
    tm._classify_replied_requests(_FakeAgent("UNCLEAR"), scope)
    assert tr.get_request(scope, rid)["status"] == "declined"   # closed safely, no second reconfirm


def test_request_without_user_reply_is_left_alone(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    scope = "c-none"
    e = tr.add_request(scope, "Automate tests?", run_seq=1)  # never answered -> stays 'asked'
    tm._classify_replied_requests(_FakeAgent("DECLINED"), scope)
    assert tr.get_request(scope, e["id"])["status"] == "asked"


def test_classifier_call_failure_leaves_status_replied(monkeypatch, tmp_path):
    """If the LLM call itself RAISES (transient outage), the request stays 'replied' and is retried on a
    later run — we do NOT prompt the user a reconfirm over a provider hiccup."""
    _isolate(monkeypatch, tmp_path)
    scope = "c-err"
    rid = _replied(scope)

    class _Boom:
        def _generate_for_classification(self, prompt):
            raise RuntimeError("provider down")

    tm._classify_replied_requests(_Boom(), scope)
    assert tr.get_request(scope, rid)["status"] == "replied"


# ── presence-ack: re-ask instead of mis-recording a nudge reply ───────────────

def test_presence_ack_detects_bare_acknowledgements():
    for s in ["ja", "Ja!", "yes", "yes.", "da", "bin wieder da", "Hier", "👋", "yep", "  Yo ",
              "zurück", "anwesend", "i'm back"]:
        assert tm._is_presence_ack(s), s


def test_presence_ack_rejects_real_content():
    for s in ["ja mach das", "nein!", "für was? nein!", "yes please set it up", "nö", "warum?",
              "ja aber später", "", "klar mach mal", "richtig ein langer satz hier drin"]:
        assert not tm._is_presence_ack(s), s


# ── reconfirm prompt framing ──────────────────────────────────────────────────

def test_followup_prompt_reconfirm_is_a_soft_recap():
    normal = tm._build_followup_prompt("Soll ich X automatisieren?", reconfirm=False)
    recap = tm._build_followup_prompt("Soll ich X automatisieren?", reconfirm=True)
    assert "NOT replied yet" in normal and "yes/no" in normal
    assert "DID reply" in recap and "RECAP" in recap
    assert normal != recap
