# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""scripts/pin_llama_cpp.py pins a build only when llama.cpp's own workflow attested it.

The script takes the asset digests from the release API and cross-checks them against
the SLSA provenance statement GitHub stores under /attestations/<digest>. Two things
broke that for the builds after llama.cpp split its release into a build and a publish
workflow (measured on b12359):

- the statement names `.github/workflows/release-publish.yml`, which the old
  `endswith("release.yml")` check refused;
- the lookup used the digest of the first selected asset, a `cudart-llama-bin-*` archive
  that is byte-identical across builds: its digest returns the statements of many builds,
  and the first of them belonged to another build.

No network here: the script's `_get` answers from synthetic release and attestation
payloads shaped like the API's.
"""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TAG = "b12359"
REPO_URL = "https://github.com/ggml-org/llama.cpp"
PUBLISH = ".github/workflows/release-publish.yml"


def _load_script():
    spec = importlib.util.spec_from_file_location("pin_llama_cpp_under_test", REPO / "scripts" / "pin_llama_cpp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# The shared CUDA runtime comes FIRST, as in the real release listing.
CUDART = "cudart-llama-bin-win-cuda-12.4-x64.zip"
SELECTED = {
    CUDART: _sha("cudart 12.4, the same bytes in every build"),
    f"llama-{TAG}-bin-ubuntu-x64.tar.gz": _sha(f"{TAG} ubuntu x64"),
    f"llama-{TAG}-bin-ubuntu-vulkan-x64.tar.gz": _sha(f"{TAG} ubuntu vulkan x64"),
    f"llama-{TAG}-bin-win-cuda-13.5-x64.zip": _sha(f"{TAG} win cuda 13.5"),
    "cudart-llama-bin-win-cuda-13.5-x64.zip": _sha("cudart 13.5"),
}
NOT_SELECTED = {f"llama-{TAG}-bin-ubuntu-cuda-13.5-x64.tar.gz": _sha(f"{TAG} ubuntu cuda")}


def _release() -> dict:
    assets = {**SELECTED, **NOT_SELECTED}
    return {"tag_name": TAG, "assets": [
        {"name": n, "digest": f"sha256:{d}", "size": 1000 + i,
         "browser_download_url": f"{REPO_URL}/releases/download/{TAG}/{n}"}
        for i, (n, d) in enumerate(assets.items())]}


def _attestation(subjects: dict, *, path: str = PUBLISH, repo: str = REPO_URL, repo_id: int = 612354784,
                 ref: str = "refs/heads/master") -> dict:
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": n, "digest": {"sha256": d}} for n, d in subjects.items()],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {"buildDefinition": {"externalParameters": {
            "workflow": {"ref": ref, "repository": repo, "path": path}}}},
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return {"repository_id": repo_id,
            "bundle": {"dsseEnvelope": {"payload": payload, "payloadType": "application/vnd.in-toto+json"}}}


def _other_build() -> dict:
    """A statement of another build: it shares the cudart archive and nothing else."""
    return _attestation({CUDART: SELECTED[CUDART], "llama-b12110-bin-ubuntu-x64.tar.gz": _sha("b12110 ubuntu x64")})


@pytest.fixture
def script(monkeypatch, tmp_path):
    mod = _load_script()
    monkeypatch.setattr(mod, "PIN_PATH", tmp_path / "llama_server_pin.json")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    mod.urls = []
    mod.attestations = []

    def fake_get(url):
        mod.urls.append(url)
        if url.endswith(f"/releases/tags/{TAG}"):
            return _release()
        if "/attestations/sha256:" in url:
            return {"attestations": mod.attestations}
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(mod, "_get", fake_get)
    return mod


def _written(mod) -> dict:
    return json.loads(mod.PIN_PATH.read_bytes().decode("utf-8"))


def test_the_statement_that_attests_every_asset_is_taken_wherever_it_sits(script):
    """MUTATION: take attestations[0] again and this goes red."""
    script.attestations = [_other_build(), _attestation({**SELECTED, **NOT_SELECTED})]
    assert script.main(["pin_llama_cpp.py", TAG]) == 0
    pin = _written(script)
    assert pin["tag"] == TAG
    assert {n: a["sha256"] for n, a in pin["assets"].items()} == SELECTED
    assert f"{REPO_URL} {PUBLISH} (repository id 612354784)" in pin["provenance"]


def test_the_lookup_uses_a_digest_unique_to_the_build(script):
    """MUTATION: look up by the first selected asset (the shared cudart archive) and this
    goes red."""
    script.attestations = [_attestation(SELECTED)]
    assert script.main(["pin_llama_cpp.py", TAG]) == 0
    looked_up = [u for u in script.urls if "/attestations/" in u]
    assert len(looked_up) == 1
    assert SELECTED[CUDART] not in looked_up[0]
    assert any(d in looked_up[0] for n, d in SELECTED.items() if n.startswith(f"llama-{TAG}-")), looked_up
    assert "per_page=100" in looked_up[0]


@pytest.mark.parametrize("path", [".github/workflows/release.yml", PUBLISH])
def test_both_release_workflows_are_accepted(script, path):
    """MUTATION: compare with endswith("release.yml") again and release-publish.yml goes red."""
    script.attestations = [_attestation(SELECTED, path=path)]
    assert script.main(["pin_llama_cpp.py", TAG]) == 0
    assert path in _written(script)["provenance"]


@pytest.mark.parametrize("kwargs", [
    {"path": ".github/workflows/x-release.yml"},
    {"path": ".github/workflows/release.yml.bak"},
    {"path": "release.yml"},
    {"repo": "https://github.com/someone/llama.cpp"},
    {"repo_id": 1},
], ids=["suffix-lookalike", "longer-name", "no-folder", "foreign-repo", "foreign-repo-id"])
def test_any_other_builder_is_refused(script, kwargs):
    """MUTATION: compare with endswith("release.yml") and the suffix lookalike goes red."""
    script.attestations = [_attestation(SELECTED, **kwargs)]
    with pytest.raises(SystemExit) as exc:
        script.main(["pin_llama_cpp.py", TAG])
    assert exc.value.code not in (0, None)
    assert not script.PIN_PATH.exists()


@pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/b12359", "", None])
def test_a_statement_from_another_branch_is_refused(script, ref):
    """release.yml can be started by hand from any branch; such a run is not a release.
    MUTATION: drop the ref check and this goes red."""
    script.attestations = [_attestation(SELECTED, path=".github/workflows/release.yml", ref=ref)]
    with pytest.raises(SystemExit) as exc:
        script.main(["pin_llama_cpp.py", TAG])
    assert "refs/heads/master" in str(exc.value.code)
    assert not script.PIN_PATH.exists()


def test_no_statement_covering_every_asset_is_refused(script):
    """MUTATION: accept the statement without comparing its subjects and this goes red."""
    partial = dict(SELECTED)
    partial.pop(f"llama-{TAG}-bin-win-cuda-13.5-x64.zip")
    script.attestations = [_other_build(), _attestation(partial)]
    with pytest.raises(SystemExit) as exc:
        script.main(["pin_llama_cpp.py", TAG])
    assert f"llama-{TAG}-bin-win-cuda-13.5-x64.zip" in str(exc.value.code)
    assert not script.PIN_PATH.exists()


def test_no_attestation_at_all_is_refused(script):
    script.attestations = []
    with pytest.raises(SystemExit):
        script.main(["pin_llama_cpp.py", TAG])
    assert not script.PIN_PATH.exists()


# ── the repository id is compared exactly ──────────────────────────────────────────

LLAMA_REPO_ID = 612354784

ODD_IDS = ["abc", [1], None, True, 612354784.0, 612354784.9, "６１２３５４７８４",
           " 612354784\n", "612_354_784", "612354784", {"id": 612354784}]


def _with_id(value) -> dict:
    att = _attestation(SELECTED)
    att["repository_id"] = value
    return att


@pytest.mark.parametrize("value", ODD_IDS, ids=["text", "list", "none", "bool", "float", "float-frac", "fullwidth",
                                                "padded", "underscored", "digits-as-text", "dict"])
def test_an_odd_repository_id_is_refused_and_a_later_valid_statement_still_pins(script, value):
    """int(att.get("repository_id") or 0) raised a bare ValueError or TypeError on some shapes
    (no reason, a traceback) and coerced others (fullwidth digits, padding, underscores, a
    float, True) into a number. MUTATION: put int(... or 0) back (red: a traceback, or the
    odd entry accepted on its own)."""
    script.attestations = [_with_id(value), _attestation(SELECTED)]
    assert script.main(["pin_llama_cpp.py", TAG]) == 0, "the valid statement after it is taken"
    assert f"(repository id {LLAMA_REPO_ID})" in _written(script)["provenance"]


@pytest.mark.parametrize("value", ODD_IDS, ids=["text", "list", "none", "bool", "float", "float-frac", "fullwidth",
                                                "padded", "underscored", "digits-as-text", "dict"])
def test_only_an_odd_repository_id_exits_with_its_repr(script, value):
    """MUTATION: coerce with int() again (red: ValueError/TypeError, or a pin written)."""
    script.attestations = [_with_id(value)]
    with pytest.raises(SystemExit) as exc:
        script.main(["pin_llama_cpp.py", TAG])
    assert repr(value) in str(exc.value.code), exc.value.code
    assert not script.PIN_PATH.exists()


def _odd_value(rng):
    """One value for repository_id: near the id, the id in another type or spelling, or junk."""
    digits = str(LLAMA_REPO_ID)
    other_digits = ("０", "٠", "۰")             # fullwidth, Arabic-Indic, Persian zero
    choice = rng.randrange(12)
    if choice == 0:
        return LLAMA_REPO_ID + rng.choice((-1, 0, 1))
    if choice == 1:
        return rng.choice((0, -LLAMA_REPO_ID, -1, 2 ** 63, rng.randint(-10 ** 12, 10 ** 12)))
    if choice == 2:
        return rng.choice((True, False))
    if choice == 3:
        return float(LLAMA_REPO_ID) + rng.choice((0.0, 0.5, -0.1))
    if choice == 4:
        return rng.choice((" ", "\t", "\n", "")) + digits + rng.choice((" ", "\n", "", "\r"))
    if choice == 5:
        cut = rng.randint(1, len(digits) - 1)
        return digits[:cut] + "_" + digits[cut:]
    if choice == 6:
        return rng.choice(("+", "-", "")) + digits
    if choice == 7:
        zero = rng.choice(other_digits)
        return "".join(chr(ord(zero) + int(c)) for c in digits)
    if choice == 8:
        cut = rng.randint(0, len(digits))
        return digits[:cut] + "\u200b" + digits[cut:]
    if choice == 9:
        return rng.choice((None, [], [LLAMA_REPO_ID], {}, {"id": LLAMA_REPO_ID}))
    if choice == 10:
        return "".join(rng.choice("0123456789abc _-é") for _ in range(rng.randint(0, 12)))
    return digits


def test_the_repository_id_is_compared_exactly_on_random_values(script):
    """Seeded fuzz of the repository id check against an independent predicate: a statement
    is taken exactly when its id is an int equal to llama.cpp's, or when a valid statement
    follows it, and nothing but SystemExit ever escapes. VAF_FUZZ_DRAWS raises the draw
    count outside the suite. MUTATION: int(... or 0) back (red)."""
    import os
    import random

    rng = random.Random(4051)
    draws = int(os.environ.get("VAF_FUZZ_DRAWS", "300"))
    for draw in range(draws):
        value = _odd_value(rng)
        for follow in (False, True):
            if script.PIN_PATH.exists():
                script.PIN_PATH.unlink()
            script.attestations = [_with_id(value)] + ([_attestation(SELECTED)] if follow else [])
            expect = (type(value) is int and value == LLAMA_REPO_ID) or follow
            try:
                pinned = script.main(["pin_llama_cpp.py", TAG]) == 0
            except SystemExit as e:
                pinned = False
                assert repr(value) in str(e.code), (draw, value, e.code)
            assert pinned == expect, (draw, value, follow)
            assert script.PIN_PATH.exists() == expect, (draw, value, follow)
