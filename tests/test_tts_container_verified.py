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
import os
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
                                     "sha256": hashlib.sha256(CONFIG).hexdigest(), "size": len(CONFIG)}}},
        "retired": ["ja", "tr", "de", "../x", "J"]}),
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
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == [], "no voice, no staging folder"


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
    and no staging folder is left. MUTATION: download into the installed folder and remove
    both files on an error (as before): red."""
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
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]


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
    # of being cut to one, and below lg the failed card takes the full row.
    span = card[card.index("<span", card.index("title={installError}")):card.index("{installError}</span>")]
    assert "truncate" not in span and "line-clamp-3" in span and "break-words" in span, span
    assert 'installError && "max-lg:col-span-2"' in card, "below lg (tablets, narrow windows) it takes the full row"


def test_a_failed_setup_does_not_leave_the_language_stuck_downloading(container, monkeypatch):
    """/install marks the language as downloading before its thread starts; a folder that
    cannot be made (here a file where the models folder belongs) used to kill the thread
    before its error handling, and the mark stayed until a restart. MUTATION: make the
    folder before the try again: red (still "downloading", a second install is refused)."""
    container.MODELS_DIR.parent.mkdir(parents=True, exist_ok=True)
    container.MODELS_DIR.write_bytes(b"not a folder")
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


def _installed(container, model=b"installed model", config=b"installed config"):
    d = container.MODELS_DIR / "de"
    d.mkdir(parents=True)
    (d / "de.onnx").write_bytes(model)
    (d / "de.onnx.json").write_bytes(config)
    return d


def _failing_renames(monkeypatch, *failing):
    """os.rename raising for the given (source suffix, destination suffix) pairs."""
    import os
    real_rename = os.rename

    def rename(src, dst):
        for a, b in failing:
            if str(src).endswith(a) and str(dst).endswith(b):
                raise PermissionError(13, "Permission denied")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    return real_rename


def test_a_failed_swap_puts_the_installed_voice_back(container, monkeypatch):
    """Both files verified, then the staged folder cannot take the installed one's place:
    the installed folder is renamed back, whole. MUTATION: drop the rename back: red."""
    d = _installed(container)
    _failing_renames(monkeypatch, (".de.incoming", "/de"))
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is False
    assert (d / "de.onnx").read_bytes() == b"installed model"
    assert (d / "de.onnx.json").read_bytes() == b"installed config"
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]


def test_a_failed_rename_back_keeps_the_old_voice_aside_and_the_next_install_restores_it(container, monkeypatch):
    """The double fault: the swap fails and the rename back fails too. No pair is mixed:
    the language shows as not installed, the old voice stays whole in .de.previous, the
    error says that it could not be put back (both errors in the detail). The next
    attempt puts it back before anything else: even when that attempt fails its own
    download, the old voice is installed again and was never overwritten.
    MUTATIONS: report the double fault as a plain rename failure (red: the line), or let
    the next attempt start without recovering (red: the old voice is lost)."""
    _installed(container)
    real_rename = _failing_renames(monkeypatch, (".de.incoming", "/de"), (".de.previous", "/de"))
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is False
    status = container.download_progress["de"]
    assert "or the old one restored; install it again" in status["error"], status
    assert len(status["error"]) <= 115, status["error"]
    assert "could not be put in place" in status["detail"], status
    assert "putting the installed one back failed" in status["detail"], status
    assert not container.is_language_installed("de")
    kept = container.MODELS_DIR / ".de.previous"
    assert (kept / "de.onnx").read_bytes() == b"installed model"
    assert (kept / "de.onnx.json").read_bytes() == b"installed config"
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == [".de.previous"]

    import os
    monkeypatch.setattr(os, "rename", real_rename)
    _network(monkeypatch, container, {"onnx": MODEL + b"x", "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is False
    d = container.MODELS_DIR / "de"
    assert (d / "de.onnx").read_bytes() == b"installed model", "the kept voice is back, not overwritten"
    assert (d / "de.onnx.json").read_bytes() == b"installed config"
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]


def test_a_voice_kept_aside_by_a_killed_install_is_back_at_start(container):
    """A container killed between the two renames: no installed folder, the old voice in
    .de.previous. The start puts it back before the first request. MUTATION: do not
    recover at start: red."""
    kept = container.MODELS_DIR / ".de.previous"
    kept.mkdir(parents=True)
    (kept / "de.onnx").write_bytes(MODEL)
    (kept / "de.onnx.json").write_bytes(CONFIG)
    container.recover_interrupted_installs()
    assert container.is_language_installed("de")
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]
    import inspect
    src = (CONTAINER / "app.py").read_text(encoding="utf-8")
    assert "prepare_voices()" in src[src.index('if __name__ == "__main__":'):]
    start = inspect.getsource(container.prepare_voices)
    order = [start.index(step) for step in ("recover_interrupted_installs()", "remove_retired_languages()",
                                             "replace_unpinned_installs()", "download_model(DEFAULT_LANG)")]
    assert order == sorted(order), "recover, then retired languages, then unpinned voices, then the default"


def test_a_stale_kept_voice_next_to_an_installed_one_is_removed(container, monkeypatch):
    """.de.previous next to an installed folder: the swap finished, only the removal of
    the old voice failed. It is stale, removed, and the install goes on."""
    _installed(container)
    kept = container.MODELS_DIR / ".de.previous"
    kept.mkdir()
    (kept / "de.onnx").write_bytes(b"older model")
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.download_model("de") is True
    assert (container.MODELS_DIR / "de" / "de.onnx").read_bytes() == MODEL
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]


def test_uninstall_removes_what_an_interrupted_install_left(container, monkeypatch):
    """Otherwise a kept voice would come back at the next start after it was removed."""
    _installed(container)
    for name in (".de.previous", ".de.incoming"):
        (container.MODELS_DIR / name).mkdir()
    monkeypatch.setattr(container, "load_config",
                        lambda: {"installed_languages": ["de"], "language_priority": ["de"]})
    monkeypatch.setattr(container, "save_config", lambda c: None)
    container.request.body = {"language": "de"}
    assert container.uninstall_language()["status"] == "uninstalled"
    assert list(container.MODELS_DIR.iterdir()) == []


class _Killed(BaseException):
    """The process ending at this point (not an error any handler would catch)."""


def test_an_interrupted_uninstall_never_brings_an_older_voice_back(container, monkeypatch):
    """Uninstall removes what an interrupted install kept aside first and the installed
    folder last. Removed the other way round and cut short after the first removal, the
    kept older voice would be all that is left, and the next start would install it.
    MUTATION: remove the installed folder first again: red."""
    _installed(container, model=b"current model", config=b"current config")
    kept = container.MODELS_DIR / ".de.previous"
    kept.mkdir()
    (kept / "de.onnx").write_bytes(b"older model")
    (kept / "de.onnx.json").write_bytes(b"older config")
    real_remove = container.remove_path

    def remove_then_die(path):
        real_remove(path)
        raise _Killed()

    monkeypatch.setattr(container, "remove_path", remove_then_die)
    container.request.body = {"language": "de"}
    with pytest.raises(_Killed):
        container.uninstall_language()
    monkeypatch.setattr(container, "remove_path", real_remove)
    container.recover_interrupted_installs()            # the next start
    model = container.MODELS_DIR / "de" / "de.onnx"
    assert not model.exists() or model.read_bytes() == b"current model", model.read_bytes()
    assert not kept.exists()


def test_the_staging_folder_is_gone_before_the_error_allows_a_retry(container, monkeypatch):
    """The error status is what lets a retry start; a retry stages into the same folder,
    so this attempt's cleanup must be over by then. MUTATION: report the error before the
    cleanup: red."""
    seen = []
    real_remove = container.remove_path

    def remove(path):
        if path.name == ".de.incoming":
            seen.append(container.download_progress.get("de", {}).get("status"))
        return real_remove(path)

    monkeypatch.setattr(container, "remove_path", remove)
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG + b"x"}, calls)
    assert container.download_model("de") is False
    assert len(seen) == 2 and all(status == "downloading" for status in seen), seen
    assert container.download_progress["de"]["status"] == "error"
    assert not (container.MODELS_DIR / ".de.incoming").exists()


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


# ── voices this release no longer offers, and voices of another build ─────────────

def _config(container) -> dict:
    return json.loads(container.CONFIG_FILE.read_bytes())


def test_only_plain_retired_codes_outside_the_table_are_used(container):
    """A retired code names a folder below MODELS_DIR: "../x" or a language still offered
    would remove something it must not. MUTATION: drop the code check: red."""
    assert container.RETIRED_LANGUAGES == ["ja", "tr"]


def test_a_retired_language_is_removed_at_start(container):
    """Japanese left the table: its installed voice, what an interrupted install of it
    left, and its place in the settings go at start; a default language that was Japanese
    becomes the container's default. MUTATIONS: skip remove_retired_languages (red), leave
    default_language (red)."""
    for name in ("ja", ".ja.previous", ".ja.incoming"):
        (container.MODELS_DIR / name).mkdir(parents=True)
        (container.MODELS_DIR / name / "ja.onnx").write_bytes(b"retired voice")
    d = _installed(container, model=MODEL, config=CONFIG)
    container.save_config({"installed_languages": ["de", "ja"], "language_priority": ["ja", "de"],
                           "auto_detect": True, "default_language": "ja"})
    container.remove_retired_languages()
    assert sorted(p.name for p in container.MODELS_DIR.iterdir()) == ["de"]
    assert (d / "de.onnx").read_bytes() == MODEL
    config = _config(container)
    assert config["installed_languages"] == ["de"] and config["language_priority"] == ["de"]
    assert config["default_language"] == container.DEFAULT_LANG


def _immediate_threads(monkeypatch, container):
    started = []

    def thread(target, daemon=True):
        started.append(target)
        return types.SimpleNamespace(start=target)
    monkeypatch.setattr(container.threading, "Thread", thread)
    return started


def test_an_installed_voice_of_another_build_is_not_installed_and_is_replaced(container, monkeypatch):
    """An earlier VAF installed another build of the German voice: same name, other bytes.
    It is not installed (the settings card offers it again and synthesis refuses it before
    Piper loads anything), and the start installs the pinned one in one background thread.
    MUTATIONS: is_language_installed back to "both files exist" (red), start without
    replace_unpinned_installs (red: nothing replaced)."""
    import sys as _sys
    _installed(container, model=MODEL, config=b'{"audio": {"older": 1}}')
    loaded = []
    piper = types.ModuleType("piper")
    piper.PiperVoice = types.SimpleNamespace(load=lambda *a: loaded.append(a))
    monkeypatch.setitem(_sys.modules, "piper", piper)
    assert not container.is_language_installed("de")
    assert container.synthesize_speech("Hallo", "de") is None
    assert loaded == []
    assert [row["installed"] for row in container.list_languages()["languages"]] == [False]

    started = _immediate_threads(monkeypatch, container)
    calls = []
    _network(monkeypatch, container, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    assert container.replace_unpinned_installs() == ["de"]
    assert len(started) == 1 and len(calls) == 2
    assert container.is_language_installed("de")
    assert container.download_progress["de"] == {"status": "completed", "progress": 100}


def test_a_model_of_another_size_is_not_installed(container):
    _installed(container, model=MODEL + b"x", config=CONFIG)
    assert not container.is_language_installed("de")


def test_while_a_voice_is_replaced_a_second_install_starts_nothing(container, monkeypatch):
    """The replaced language is marked as downloading before its thread runs, so the card
    shows it and a click on Install does not start a second download of the same files."""
    _installed(container, model=MODEL, config=b"other")
    monkeypatch.setattr(container.threading, "Thread",
                        lambda target, daemon=True: types.SimpleNamespace(start=lambda: None))
    assert container.replace_unpinned_installs() == ["de"]
    container.request.body = {"language": "de"}
    assert "already being downloaded" in container.install_language()["message"]


def test_a_pinned_install_downloads_nothing_at_start(container, monkeypatch):
    _installed(container, model=MODEL, config=CONFIG)
    started = _immediate_threads(monkeypatch, container)
    monkeypatch.setattr(sys.modules["verified_download"].requests, "get",
                        lambda *a, **k: pytest.fail("a download started"))
    assert container.replace_unpinned_installs() == []
    assert started == []
    assert container.is_language_installed("de")


def _calls_to_download_model(monkeypatch, container) -> list:
    calls = []
    monkeypatch.setattr(container, "download_model", lambda lang, mark=None: calls.append(lang) or True)
    return calls


def test_a_default_language_being_replaced_is_not_installed_a_second_time(container, monkeypatch):
    """Nothing in the settings counts as installed, and the default language's folder holds
    a voice of another build: the background replaces it, so the start must not install
    it as well (two downloads into one staging folder). MUTATION: drop the
    "DEFAULT_LANG not in replacing" guard: red."""
    _installed(container, model=MODEL, config=b"other build")
    started = []
    monkeypatch.setattr(container.threading, "Thread",
                        lambda target, daemon=True: types.SimpleNamespace(start=lambda: started.append(target)))
    calls = _calls_to_download_model(monkeypatch, container)
    container.prepare_voices()
    assert calls == [], "only the background thread installs it"
    assert len(started) == 1
    started[0]()
    assert calls == ["de"]


def test_with_nothing_installed_the_default_language_is_installed_at_start(container, monkeypatch):
    calls = _calls_to_download_model(monkeypatch, container)
    container.prepare_voices()
    assert calls == [container.DEFAULT_LANG]


@pytest.fixture
def two_languages(monkeypatch, tmp_path):
    """The container with German and English, both pinned to the same fixture voice."""
    row = {"locale": "de_DE", "voice": "thorsten", "quality": "medium",
           "model": {"path": "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
                     "sha256": hashlib.sha256(MODEL).hexdigest(), "size": len(MODEL)},
           "config": {"path": "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json",
                      "sha256": hashlib.sha256(CONFIG).hexdigest(), "size": len(CONFIG)}}
    table = tmp_path / "models.json"
    table.write_text(json.dumps({"source": "https://example.invalid/voices", "revision": "0" * 40,
                                 "models": {"de": dict(row, name="German"), "en": dict(row, name="English")}}),
                     encoding="utf-8")
    app = _load_app(monkeypatch, table)
    monkeypatch.setattr(app, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(app, "CONFIG_FILE", tmp_path / "config" / "tts_config.json")
    for lang in ("de", "en"):
        d = app.MODELS_DIR / lang
        d.mkdir(parents=True)
        (d / f"{lang}.onnx").write_bytes(MODEL)
        (d / f"{lang}.onnx.json").write_bytes(b"other build")
    return app


def test_the_default_language_is_replaced_first(two_languages, monkeypatch):
    """English is the default: its voice comes back first, not after German's 63 MB.
    MUTATION: queue in table order: red."""
    app = two_languages
    app.save_config({"installed_languages": ["de", "en"], "language_priority": ["de", "en"],
                     "auto_detect": True, "default_language": "en"})
    monkeypatch.setattr(app.threading, "Thread", lambda target, daemon=True: types.SimpleNamespace(start=lambda: None))
    assert app.replace_unpinned_installs() == ["en", "de"]


def _queue_two(app, monkeypatch) -> tuple:
    """German and English queued for replacement, the worker not yet run; the fake network
    records every fetch."""
    started = []
    monkeypatch.setattr(app.threading, "Thread",
                        lambda target, daemon=True: types.SimpleNamespace(start=lambda: started.append(target)))
    calls = []
    _network(monkeypatch, app, {"onnx": MODEL, "onnx.json": CONFIG}, calls)
    app.save_config({"installed_languages": ["de", "en"], "language_priority": ["de", "en"],
                     "auto_detect": True, "default_language": "de"})
    assert app.replace_unpinned_installs() == ["de", "en"]
    return started[0], calls


def _removed_for_good(app, lang):
    assert not os.path.lexists(app.MODELS_DIR / lang)
    config = app.load_config()
    assert lang not in config["installed_languages"] and lang not in config["language_priority"]


def test_a_queued_language_that_was_uninstalled_is_not_installed_again(two_languages, monkeypatch):
    """German is being replaced and English waits in the queue; the person removes English.
    Its turn must not bring it back. MUTATION: drop the mark check in download_model: red."""
    app = two_languages
    work, calls = _queue_two(app, monkeypatch)
    app.request.body = {"language": "en"}
    assert app.uninstall_language()["status"] == "uninstalled"
    work()
    assert len(calls) == 2, "only German's two files"
    assert app.is_language_installed("de")
    _removed_for_good(app, "en")


def test_an_uninstall_just_before_the_download_starts_is_kept(two_languages, monkeypatch):
    """The narrowest ordering: English's turn has come, and the person removes it after the
    worker picked it up but before its download marks itself as started. The mark is
    checked under the lock that starts the download, so nothing is fetched and English stays
    removed. MUTATION: check the mark in the worker before calling download_model, not in
    download_model (red: English is downloaded again)."""
    app = two_languages
    work, calls = _queue_two(app, monkeypatch)
    real = app._download_guarded

    def remove_first(lang, mark=None):
        if lang == "en":
            app.request.body = {"language": "en"}
            assert app.uninstall_language()["status"] == "uninstalled"
        return real(lang, mark)
    monkeypatch.setattr(app, "_download_guarded", remove_first)
    work()
    assert len(calls) == 2, "only German's two files"
    _removed_for_good(app, "en")
    assert "en" not in app.download_progress


@pytest.mark.parametrize("config", [
    {"installed_languages": ["de"], "default_language": ["en"], "language_priority": ["de"]},
    {"installed_languages": ["de"], "default_language": "de", "language_priority": [{"en": 1}, "en"]},
    {"installed_languages": ["de"], "default_language": "de", "language_priority": 5},
    {"installed_languages": ["de"], "default_language": {"x": 1}, "language_priority": None},
], ids=["default-a-list", "priority-holds-an-object", "priority-a-number", "default-an-object"])
def test_a_damaged_settings_file_does_not_stop_the_start(two_languages, monkeypatch, config):
    """The settings file is the container's own, but a hand edit or a damaged volume can put
    a list or an object where a language belongs; the start must still come up and replace
    the voices. MUTATIONS: let _replacement_order take any entry (red), drop the list check
    in remove_retired_languages (red: priority-a-number)."""
    app = two_languages
    app.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    app.CONFIG_FILE.write_text(json.dumps(dict(config, auto_detect=True)), encoding="utf-8")
    monkeypatch.setattr(app.threading, "Thread", lambda target, daemon=True: types.SimpleNamespace(start=lambda: None))
    calls = _calls_to_download_model(monkeypatch, app)
    app.prepare_voices()
    assert sorted(app._replacement_order()) == ["de", "en"]
    assert calls == []
