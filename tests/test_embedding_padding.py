# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Bucket padding in the ONNX embedding model: fast, deterministic, and honest.

The tokenizer used to pad EVERY text to a fixed 512 tokens, so a twelve-token query ran
a full 512-wide forward pass through all six MiniLM layers - measured ~160 ms per fresh
embedding, and that cost sat inside every RAG query, every memory search, and the voice
reflex policy's live turns. Padding to the next multiple of 32 brings it to single-digit
milliseconds.

Two properties guard the change, because the obvious cheaper variants each break one:

- The vector must stay a PURE FUNCTION OF THE TEXT. Plain `longest` padding makes it
  depend on the longest neighbor in the batch: the int8-quantized model derives its
  activation scale from the whole tensor, padding rows included, so the same text
  embeds differently alone vs in a group. Buckets close that door.
- Old and new vectors must stay COMPARABLE. The switch from 512 to buckets shifts
  vectors slightly (same int8 mechanism; measured cos >= 0.989 per text, pairwise
  similarity drift <= 0.016 against a related/unrelated signal gap of 0.09 to 0.59),
  which is why stored vectors are NOT re-indexed and no thresholds moved. The mask
  test pins the mechanism that keeps the drift this small.

Model-dependent tests skip when the model is not cached locally - the suite must not
download models (CI incident precedent: a voice test needed a model only this machine
had cached).
"""
import numpy as np
import pytest


def _model_or_skip():
    try:
        from vaf.memory.embeddings import get_model
        return get_model()
    except Exception as e:  # pragma: no cover - offline/hostile env
        pytest.skip(f"embedding model unavailable: {e}")


# ── T3: count the work, not the wall clock ───────────────────────────────────

def test_a_short_query_no_longer_pays_the_full_width(monkeypatch):
    """Deterministic latency test: assert the sequence length fed to the model, not a
    timing. A 12-token query must run in the smallest bucket, not at 512."""
    m = _model_or_skip()
    seen = {}
    real_run = m.session.run

    def spy(out, inputs, *a, **kw):
        seen["seq"] = inputs["input_ids"].shape[1]
        return real_run(out, inputs, *a, **kw)

    monkeypatch.setattr(m.session, "run", spy)
    m.encode("Was steht heute in meinem Kalender?")
    assert seen["seq"] <= 64, (
        f"a short query ran at seq={seen['seq']} - the fixed-512 padding is back, and "
        f"with it the ~160 ms forward pass on every RAG/memory/voice-policy lookup"
    )


def test_a_long_text_is_still_capped_at_the_models_ceiling(monkeypatch):
    m = _model_or_skip()
    seen = {}
    real_run = m.session.run

    def spy(out, inputs, *a, **kw):
        seen["seq"] = inputs["input_ids"].shape[1]
        return real_run(out, inputs, *a, **kw)

    monkeypatch.setattr(m.session, "run", spy)
    m.encode("wort " * 2000)
    assert seen["seq"] == 512, "truncation at the model ceiling must survive the padding change"


# ── T2: the vector is a pure function of the text ────────────────────────────

def test_the_same_text_embeds_identically_alone_and_in_a_batch():
    """The trap `longest` padding would open: next to a much longer neighbor the text
    would be padded further, and the int8 activation scale would shift its vector.
    Buckets make both runs land in the same bucket only when lengths are close - so
    the pair here is chosen to land in DIFFERENT longest-buckets on purpose."""
    m = _model_or_skip()
    text = "kurzer eindeutiger satz"
    alone = np.asarray(m.encode(text))
    batched = np.asarray(m.encode([text, "ein sehr viel laengerer nachbar " * 40])[0])
    assert np.array_equal(alone, batched), (
        "the same text embedded differently alone vs in a batch - padding depends on "
        "the neighbor again (longest-style), the vector is no longer a function of the text"
    )


# ── T1: the mask mechanism that keeps old and new comparable ─────────────────

def test_padding_changes_the_vector_only_marginally():
    """The guarantee the no-re-index decision rests on: forcing extra padding onto the
    same text must move its vector only within the measured drift band. The mutation
    that turns this red is feeding the model an all-ones attention mask (padding rows
    then attend like real tokens and the cosine collapses) - verified. Note the
    pooling DENOMINATOR is not separately testable this way: dividing by seq instead
    of mask.sum() is a constant per text and the L2 normalization cancels it."""
    m = _model_or_skip()
    text = "kubernetes ingress controller tls termination"

    v_bucket = np.asarray(m.encode(text), dtype=np.float64)

    # Reproduce the OLD fixed-512 path by hand: same tokenizer, hard 512 padding.
    enc = m.tokenizer.encode(text)
    ids = list(enc.ids) + [0] * (512 - len(enc.ids))
    mask = list(enc.attention_mask) + [0] * (512 - len(enc.attention_mask))
    types = list(enc.type_ids) + [0] * (512 - len(enc.type_ids))
    inputs = {
        "input_ids": np.array([ids], dtype=np.int64),
        "attention_mask": np.array([mask], dtype=np.int64),
    }
    if "token_type_ids" in m.input_names:
        inputs["token_type_ids"] = np.array([types], dtype=np.int64)
    out = m.session.run(None, inputs, m.run_options)
    pooled = m.mean_pooling(out[0], inputs["attention_mask"])
    v_512 = (pooled / np.linalg.norm(pooled, axis=1, keepdims=True))[0].astype(np.float64)

    cos = float(np.dot(v_bucket, v_512))
    assert cos >= 0.985, (
        f"cos(bucket, fixed-512) = {cos:.4f} - the padding drift exploded past the "
        f"measured band. Stored vectors are no longer comparable; this is now a "
        f"re-index question, not a padding tweak."
    )
