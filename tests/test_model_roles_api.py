# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`POST /api/config/model-roles`: the settings overview's rows, saved or previewed.

The rows are `model_roles.resolve_roles`, the resolver the lanes ask, so the overview
cannot disagree with the backend. An unsaved draft is laid over the saved config, but
only its role keys are taken: a draft must not carry a key, a URL or a secret into the
resolver. Admin-only, and it writes nothing.

MUTATIONS, each turns the named test red:
- drop the draft allowlist filter -> test_keys_outside_the_allowlist_are_ignored;
- drop `require_admin` from the route -> test_a_non_admin_gets_403;
- call `Config.save` in the route -> test_the_route_writes_nothing;
- add `local_api_url` to `ROLE_CONFIG_KEYS` -> test_role_keys_are_safe.
"""
import json

import pytest

from vaf.core import model_roles as mr
from vaf.core.config import Config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    from vaf.core.migrations import CONFIG_FORMAT_VERSION
    # Current format version: an older file is migrated (and written) by Config.load itself.
    path.write_text(json.dumps({"provider": "deepseek", "api_model_deepseek": "deepseek-v4-pro",
                                "config_format_version": CONFIG_FORMAT_VERSION}),
                    encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    import vaf.core.api_keys as api_keys
    monkeypatch.setattr(api_keys, "resolve_api_key",
                        lambda provider, caller_config=None: "sk-x" if provider == "deepseek" else "")
    return path


def _client(role="admin"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from vaf.api import config_routes

    app = FastAPI()

    @app.middleware("http")
    async def _as(request, call_next):
        request.state.user = {"username": "alice", "role": role, "user_scope_id": "ab12cd34"}
        return await call_next(request)

    app.include_router(config_routes.router)
    return TestClient(app)


def _rows(rows):
    return [a.as_dict() for a in rows]


def test_without_a_draft_the_rows_are_the_saved_config(config_file):
    r = _client().post("/api/config/model-roles")
    assert r.status_code == 200
    body = r.json()
    assert body["roles"] == _rows(mr.resolve_roles())
    assert body["keys"] == sorted(mr.ROLE_CONFIG_KEYS)
    vision = next(row for row in body["roles"] if row["role"] == "vision")
    assert (vision["source"], vision["reason"]) == ("unavailable", "main_cannot_see")
    assert _client().post("/api/config/model-roles", json={}).json()["roles"] == body["roles"]


def test_a_draft_previews_unsaved_choices(config_file):
    draft = {"provider": "anthropic", "api_model_anthropic": "claude-sonnet-4-6"}
    body = _client().post("/api/config/model-roles", json={"draft": draft}).json()
    assert body["roles"] == _rows(mr.resolve_roles({**Config.load(), **draft}))
    vision = next(row for row in body["roles"] if row["role"] == "vision")
    assert (vision["provider"], vision["source"]) == ("anthropic", "follows_main")


def test_keys_outside_the_allowlist_are_ignored(config_file, monkeypatch):
    seen = []
    real = mr.resolve_roles

    def spy(config=None, **kw):
        seen.append(dict(config) if config is not None else None)
        return real(config, **kw)
    monkeypatch.setattr(mr, "resolve_roles", spy)
    draft = {"api_key_openai": "sk-stolen", "local_api_url": "http://169.254.169.254/",
             "inference_profile": "api", "vision_provider": "google"}
    _client().post("/api/config/model-roles", json={"draft": draft})
    cfg = seen[-1]
    assert cfg["vision_provider"] == "google"
    assert cfg.get("api_key_openai") != "sk-stolen"
    assert cfg.get("local_api_url", "") != "http://169.254.169.254/"
    assert cfg.get("inference_profile") != "api"
    # Only junk: the saved config, as if no draft was sent.
    _client().post("/api/config/model-roles", json={"draft": {"api_key_openai": "sk-x"}})
    assert seen[-1] is None


def test_a_non_admin_gets_403(config_file):
    assert _client(role="user").post("/api/config/model-roles").status_code == 403


def test_the_route_writes_nothing(config_file, monkeypatch):
    before = config_file.read_bytes()
    saved = []
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, data: saved.append(data)))
    monkeypatch.setattr(Config, "set", classmethod(lambda cls, k, v: saved.append((k, v))))
    _client().post("/api/config/model-roles", json={"draft": {"provider": "openai"}})
    assert saved == []
    assert config_file.read_bytes() == before


def test_role_keys_are_safe():
    """Criterion 6: the draft allowlist is a subset of the configuration and holds no
    key, no endpoint and nothing secret."""
    keys = mr.ROLE_CONFIG_KEYS
    assert keys <= set(Config.DEFAULTS)
    forbidden = {"local_api_url", "inference_profile", "local_engine"}
    for key in keys:
        assert not key.startswith("api_key_"), key
        assert "base_url" not in key and "url" not in key, key
        assert "secret" not in key and "token" not in key and "password" not in key, key
        assert key not in forbidden, key
