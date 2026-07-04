"""Tests for {{APP_NAME}}.

Uses Flask's test client - no network, no running server needed. If Flask is not
installed these tests are skipped (not failed). Add assertions for your own routes as
you add them to server.py.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("flask_cors")

from server import app


def test_health_endpoint():
    client = app.test_client()
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "healthy"


def test_home_endpoint():
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "status" in resp.get_json()
