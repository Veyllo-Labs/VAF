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
    for name in ("request", "jsonify", "Response", "send_file"):
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
    assert status["status"] == "error" and "did not match its pinned checksum" in status["error"], status
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
