import sqlite3
import pytest
from fastapi.testclient import TestClient

from backend import database as db
from backend.main import app

client = TestClient(app)


def test_healthz_ok():
    response = client.get("/healthz")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["db"] == "connected"
    assert isinstance(data["groq_configured"], bool)
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0


def test_healthz_unauthenticated():
    # Verify no Authorization header is required
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_groq_configuration_flag(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_mock_key_123")
    res1 = client.get("/healthz")
    assert res1.status_code == 200
    assert res1.json()["groq_configured"] is True

    monkeypatch.setenv("GROQ_API_KEY", "")
    res2 = client.get("/healthz")
    assert res2.status_code == 200
    assert res2.json()["groq_configured"] is False


def test_healthz_db_failure_returns_503(monkeypatch):
    def broken_conn():
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(db, "get_conn", broken_conn)

    response = client.get("/healthz")
    assert response.status_code == 503

    data = response.json()
    assert data["status"] == "error"
    assert "disconnected" in data["db"]
    assert "groq_configured" in data
    assert isinstance(data["uptime_seconds"], (int, float))
