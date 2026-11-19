# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The speech container installs a voice only as the bytes pinned in its models.json.

docker/tts-multilang/app.py is imported by path with stand-ins for flask and flask_cors
(neither is a VAF dependency); its verified_download is the byte-identical copy next to
it. The network is a fake, the table a fixture.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTAINER = REPO / "docker" / "tts-multilang"
MODEL, CONFIG = b"onnx voice" * 100, b'{"audio": {}}'


def _stub_flask(monkeypatch):
    class Flask:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
        def run(self, *a, **k): pass

    flask = types.ModuleType("flask")
    flask.Flask = Flask
    flask.request = types.SimpleNamespace(body={}, get_json=lambda: flask.request.body)
    flask.jsonify = lambda payload: payload
    for name in ("Response", "send_file"):
        setattr(flask, name, object())
    cors = types.ModuleType("flask_cors")
    cors.CORS = lambda app: None
    monkeypatch.setitem(sys.modules, "flask", flask)
    monkeypatch.setitem(sys.modules, "flask_cors", cors)


def _load_app(monkeypatch, table: Path | None = None):
    _stub_flask(monkeypatch)
    if table is not None:
        monkeypatch.setenv("TTS_MODELS_TABLE", str(table))
    monkeypatch.syspath_prepend(str(CONTAINER))
    monkeypatch.delitem(sys.modules, "verified_download", raising=False)
    spec = importlib.util.spec_from_file_location("vaf_tts_container_app", CONTAINER / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "vaf_tts_container_app", module)
    return module


@pytest.fixture
def container(monkeypatch, tmp_path):
    table = tmp_path / "models.json"
    table.write_text(json.dumps({
        "source": "https://example.invalid/voices", "revision": "0" * 40,
        "models": {"de": {"name": "German (Deutsch)", "locale": "de_DE", "voice": "thorsten", "quality": "medium",
                          "model": {"path": "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
                                    "sha256": hashlib.sha256(MODEL).hexdigest(), "size": len(MODEL)},
                          "config": {"path": "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json",
                                     "sha256": hashlib.sha256(CONFIG).hexdigest(), "size": len(CONFIG)}}}}),
        encoding="utf-8")
    app = _load_app(monkeypatch, table)
    monkeypatch.setattr(app, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(app, "CONFIG_FILE", tmp_path / "config" / "tts_config.json")
    return app


def _network(monkeypatch, app, payloads: dict, calls: list):
    def fake_get(url, **kwargs):
        calls.append(url)
        payload = payloads[url.rsplit("/", 1)[1].split(".", 1)[1]]

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): pass
            def iter_content(self, chunk_size):
                for i in range(0, len(payload), chunk_size):
                    yield payload[i:i + chunk_size]
        return R()
    monkeypatch.setattr(sys.modules["verified_download"].requests, "get", fake_get)


def test_bytes_that_miss_the_pin_end_in_an_error_and_leave_nothing(container, monkeypatch):
    """MUTATION: drop the digest check in download_model (write the response as before):
    red."""
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL + b"x", "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is False
    status = container.download_progress["de"]
    assert status["status"] == "error" and "did not match its expected checksum" in status["error"], status
    assert sorted(p.name for p in (container.MODELS_DIR / "de").iterdir()) == []


def test_pinned_bytes_complete_from_the_pinned_revision(container, monkeypatch):
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is True
    assert container.download_progress["de"] == {"status": "completed", "progress": 100}
    assert (container.MODELS_DIR / "de" / "de.onnx").read_bytes() == MODEL
    assert (container.MODELS_DIR / "de" / "de.onnx.json").read_bytes() == CONFIG
    assert calls[0] == "https://example.invalid/voices/resolve/" + "0" * 40 + \
        "/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"


def test_the_table_is_read_from_models_json_and_no_url_is_written_into_the_app(monkeypatch):
    """MUTATION: put the literal table with its huggingface.co URLs back: red."""
    src = (CONTAINER / "app.py").read_text(encoding="utf-8")
    assert "huggingface.co" not in src and "iter_content(" not in src
    app = _load_app(monkeypatch)
    table = json.loads((CONTAINER / "models.json").read_bytes().decode("utf-8"))
    assert set(app.AVAILABLE_MODELS) == set(table["models"])
    for lang, row in table["models"].items():
        assert app.AVAILABLE_MODELS[lang]["model_sha256"] == row["model"]["sha256"], lang
        assert f"/resolve/{table['revision']}/" in app.AVAILABLE_MODELS[lang]["model_url"], lang
    assert "COPY verified_download.py /app/" in (CONTAINER / "Dockerfile").read_text(encoding="utf-8")


def test_a_rejected_config_keeps_the_installed_pair(container, monkeypatch):
    """The model verified, the config did not: the pair already installed stays as it was
    and no staged file is left. MUTATION: download straight into the final names and
    remove both on an error (as before): red."""
    d = container.MODELS_DIR / "de"
    d.mkdir(parents=True)
    (d / "de.onnx").write_bytes(b"installed model")
    (d / "de.onnx.json").write_bytes(b"installed config")
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG + b"x"}, calls)
    assert container.download_model("de") is False
    assert sorted(p.name for p in d.iterdir()) == ["de.onnx", "de.onnx.json"]
    assert (d / "de.onnx").read_bytes() == b"installed model"
    assert (d / "de.onnx.json").read_bytes() == b"installed config"


def test_an_unreachable_server_is_said_in_one_line(container, monkeypatch):
    import requests

    def down(url, **kwargs):
        raise requests.ConnectionError("HTTPSConnectionPool(host='x', port=443): Max retries exceeded")
    monkeypatch.setattr(sys.modules["verified_download"].requests, "get", down)
    assert container.download_model("de") is False
    status = container.download_progress["de"]
    assert status["error"] == ("The German (Deutsch) voice could not be downloaded: the download server "
                               "could not be reached."), status
    assert "Max retries" in status["detail"]


def test_a_second_install_while_one_runs_starts_no_second_download(container, monkeypatch):
    """MUTATION: drop the check and a second thread starts."""
    started = []
    monkeypatch.setattr(container.threading, "Thread",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: started.append(1)))
    container.request.body = {"language": "de"}
    first = container.install_language()
    second = container.install_language()
    assert first["status"] == "downloading" and second["status"] == "downloading"
    assert "already being downloaded" in second["message"]
    assert started == [1]


@pytest.mark.parametrize("lang", ["../..", "/app", "xx"])
def test_uninstall_refuses_a_language_outside_the_table(container, monkeypatch, tmp_path, lang):
    """/uninstall removed MODELS_DIR / <language> for any value, "../.." included.
    MUTATION: drop the table check: red (the sentinel folder is gone)."""
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep").write_bytes(b"keep")
    container.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(container, "MODELS_DIR", tmp_path / "models" / "inner")
    container.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    container.request.body = {"language": "../../sentinel" if lang == "../.." else lang}
    out = container.uninstall_language()
    assert isinstance(out, tuple) and out[1] == 400, out
    assert (sentinel / "keep").read_bytes() == b"keep"


def test_the_settings_card_shows_why_an_install_failed():
    """The container's error line (a checksum refusal, no connection) reached the browser
    and was dropped: the language card fell back to the plain Install icon. It is shown on
    the card now, with the full text on hover, and Install stays as the retry.
    MUTATION: remove the error line from the card: red."""
    src = (REPO / "web" / "components" / "settings" / "TTSSettings.tsx").read_text(encoding="utf-8")
    card = src[src.index("{availableLanguages.map(lang => {"):src.index("{/* Auto-detect & Auto-speak */}")]
    assert "lang.download_status?.status === 'error'" in card and "lang.download_status.error : undefined" in card
    assert "{installError}</span>" in card and "title={installError}" in card
    assert 'title="Install"' in card, "the Install button is the retry"
    # The cause comes last in every message: the line wraps (three lines at most) instead
    # of being cut to one, and on a phone the failed card takes the full row.
    span = card[card.index("<span", card.index("title={installError}")):card.index("{installError}</span>")]
    assert "truncate" not in span and "line-clamp-3" in span and "break-words" in span, span
    assert 'installError && "max-md:col-span-2"' in card


def test_a_failed_setup_does_not_leave_the_language_stuck_downloading(container, monkeypatch):
    """/install marks the language as downloading before its thread starts; a folder that
    cannot be made (here a file in its place) used to kill the thread before its error
    handling, and the mark stayed until a restart. MUTATION: make the folder before the
    try again: red (still "downloading", a second install is refused)."""
    container.MODELS_DIR.mkdir(parents=True)
    (container.MODELS_DIR / "de").write_bytes(b"not a folder")
    assert container.download_model("de") is False, "the failed setup is reported, not raised"
    assert container.download_progress["de"]["status"] == "error"
    monkeypatch.setattr(container.threading, "Thread",
                        lambda target, daemon=True: types.SimpleNamespace(start=target))
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    container.request.body = {"language": "de"}
    container.install_language()
    assert container.download_progress["de"]["status"] == "error", container.download_progress
    assert container.install_language()["status"] == "downloading"
    assert "already being downloaded" not in container.install_language().get("message", "")


def test_a_failed_promotion_puts_the_installed_pair_back(container, monkeypatch):
    """Both files verified, then the second rename fails: the new model must not stay next
    to the old config (the language would count as installed and never be repaired).
    MUTATION: promote with two plain renames: red."""
    import os
    d = container.MODELS_DIR / "de"
    d.mkdir(parents=True)
    (d / "de.onnx").write_bytes(b"installed model")
    (d / "de.onnx.json").write_bytes(b"installed config")
    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith("de.onnx.json") and str(src).endswith(".incoming"):
            raise PermissionError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is False
    assert (d / "de.onnx").read_bytes() == b"installed model"
    assert (d / "de.onnx.json").read_bytes() == b"installed config"
    assert sorted(p.name for p in d.iterdir()) == ["de.onnx", "de.onnx.json"]


def test_the_staged_files_are_gone_before_the_error_allows_a_retry(container, monkeypatch):
    """The error status is what lets a retry start; a retry writes the same staged names,
    so this attempt's cleanup must be over by then. MUTATION: report the error before the
    cleanup: red."""
    import pathlib
    seen = []
    real_unlink = pathlib.Path.unlink

    def unlink(self, *a, **k):
        if self.name.endswith(".incoming"):
            seen.append(container.download_progress.get("de", {}).get("status"))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG + b"x"}, calls)
    assert container.download_model("de") is False
    assert seen and all(status == "downloading" for status in seen), seen
    assert container.download_progress["de"]["status"] == "error"


def test_whatever_escapes_the_download_still_ends_the_downloading_mark(container, monkeypatch):
    """The thread's own guard: an exception download_model did not report itself still
    turns "downloading" into an error, so the language can be installed again.
    MUTATION: drop the guard in do_download: red."""
    def broken(lang):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(container, "download_model", broken)
    monkeypatch.setattr(container.threading, "Thread",
                        lambda target, daemon=True: types.SimpleNamespace(start=target))
    container.request.body = {"language": "de"}
    container.install_language()
    status = container.download_progress["de"]
    assert status["status"] == "error" and "could not be installed" in status["error"], status
