from __future__ import annotations

import shutil

from fastapi.testclient import TestClient

from orb_bot.webui import create_app

AUTH = ("admin", "test-password")


def _client(tmp_path, config_source="config/config.yaml"):
    config_path = tmp_path / "config.yaml"
    shutil.copy(config_source, config_path)
    app = create_app(str(config_path), "admin", "test-password")
    return TestClient(app), config_path


def test_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/api/config")
    assert resp.status_code == 401


def test_rejects_wrong_password(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/api/config", auth=("admin", "wrong"))
    assert resp.status_code == 401


def test_get_config_returns_current_values(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/api/config", auth=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["instrument"]["symbol"] == "MES"
    assert data["opening_range_filter"]["min_width_points"] == 10.0
    assert data["exits"]["tp1_close_pct"] == 0.9


def test_serves_index_page(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/", auth=AUTH)
    assert resp.status_code == 200
    assert "ORB Bot Settings" in resp.text


def test_post_valid_change_persists_and_reloads(tmp_path):
    client, config_path = _client(tmp_path)
    current = client.get("/api/config", auth=AUTH).json()

    current["opening_range_filter"]["min_width_points"] = 12.0
    current["opening_range_filter"]["max_width_points"] = 28.0
    current["exits"]["tp1_close_pct"] = 0.5  # "leave 50% running"

    resp = client.post("/api/config", auth=AUTH, json=current)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    reloaded = client.get("/api/config", auth=AUTH).json()
    assert reloaded["opening_range_filter"]["min_width_points"] == 12.0
    assert reloaded["opening_range_filter"]["max_width_points"] == 28.0
    assert reloaded["exits"]["tp1_close_pct"] == 0.5

    # A backup of the previous version was made before overwriting.
    backups = list((config_path.parent / "backups").glob("config.*.yaml"))
    assert len(backups) == 1


def test_post_invalid_change_is_rejected_and_not_persisted(tmp_path):
    client, config_path = _client(tmp_path)
    current = client.get("/api/config", auth=AUTH).json()
    original_min = current["opening_range_filter"]["min_width_points"]

    # min > max should fail the same validator the bot enforces at startup.
    current["opening_range_filter"]["min_width_points"] = 50.0
    current["opening_range_filter"]["max_width_points"] = 10.0

    resp = client.post("/api/config", auth=AUTH, json=current)
    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert isinstance(errors, list) and len(errors) >= 1

    unchanged = client.get("/api/config", auth=AUTH).json()
    assert unchanged["opening_range_filter"]["min_width_points"] == original_min


def test_post_mismatched_point_value_for_known_symbol_is_rejected(tmp_path):
    client, _ = _client(tmp_path)
    current = client.get("/api/config", auth=AUTH).json()
    current["instrument"]["point_value"] = 999.0  # MES is $5/pt

    resp = client.post("/api/config", auth=AUTH, json=current)
    assert resp.status_code == 422


def test_meta_exposes_known_point_values(tmp_path):
    client, _ = _client(tmp_path)
    resp = client.get("/api/meta", auth=AUTH)
    assert resp.status_code == 200
    assert resp.json()["known_point_values"]["MES"] == 5.0
    assert resp.json()["known_point_values"]["ES"] == 50.0
