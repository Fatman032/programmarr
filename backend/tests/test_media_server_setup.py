"""test_media_server_setup.py — a Jellyfin (beta) setup needs no Plex.

The setup wizard counted you as "configured" only with a Tunarr URL AND a Plex URL + token, so
anyone whose Tunarr is fed by Jellyfin was stuck at the wizard. `media_server` ("plex", the
default, or "jellyfin") says which server feeds Tunarr; Plex is only required for Plex.
Tunarr's own list of media sources is surfaced so the wizard and Dashboard can tell the two apart.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
from routers import config_router, status_router


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A data folder; call .write({...}) to set config.json."""
    monkeypatch.setattr(config_router, "DATA_DIR", tmp_path)
    channel_engine.set_tunarr_auth()
    holder = type("Cfg", (), {})()
    holder.dir = tmp_path
    holder.write = lambda d: (tmp_path / "config.json").write_text(json.dumps(d))
    holder.read = lambda: json.loads((tmp_path / "config.json").read_text())
    return holder


# ── "configured" ────────────────────────────────────────────────────────────────────

def test_a_plex_setup_still_needs_tunarr_and_plex(cfg):
    cfg.write({"tunarr_url": "http://t"})
    assert config_router.config_status()["configured"] is False
    cfg.write({"tunarr_url": "http://t", "plex_url": "http://p", "plex_token": "x"})
    status = config_router.config_status()
    assert status["configured"] is True and status["media_server"] == "plex"


def test_a_jellyfin_setup_needs_only_tunarr(cfg):
    cfg.write({"tunarr_url": "http://t", "media_server": "jellyfin"})
    status = config_router.config_status()
    assert status["configured"] is True and status["media_server"] == "jellyfin"


def test_a_jellyfin_setup_still_needs_a_tunarr_address(cfg):
    cfg.write({"media_server": "jellyfin"})
    assert config_router.config_status()["configured"] is False


def test_an_unknown_media_server_is_treated_as_plex(cfg):
    cfg.write({"tunarr_url": "http://t", "media_server": "emby"})
    status = config_router.config_status()
    assert status["configured"] is False and status["media_server"] == "plex"


def test_no_config_at_all_is_plex_and_not_configured(cfg):
    status = config_router.config_status()
    assert status == {"configured": False, "media_server": "plex", "has_tmdb": False, "has_auth": False}


# ── saving ──────────────────────────────────────────────────────────────────────────

def test_the_choice_is_saved_and_a_jellyfin_save_needs_no_plex_fields(cfg):
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", media_server="jellyfin"))
    saved = cfg.read()
    assert saved["media_server"] == "jellyfin" and "plex_url" not in saved and "plex_token" not in saved
    assert config_router.config_status()["configured"] is True


def test_switching_back_to_plex_clears_the_choice(cfg):
    cfg.write({"tunarr_url": "http://t", "media_server": "jellyfin"})
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", plex_url="http://p",
                                                        plex_token="tok", media_server=""))
    assert "media_server" not in cfg.read()
    assert config_router.config_status()["media_server"] == "plex"


def test_a_settings_save_keeps_a_jellyfin_choice(cfg):
    """Settings sends the choice with every save, so an unrelated edit mustn't lose it."""
    cfg.write({"tunarr_url": "http://t", "media_server": "jellyfin"})
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", tmdb_api_key="k", media_server="jellyfin"))
    assert cfg.read()["media_server"] == "jellyfin"


# ── Tunarr's sources ────────────────────────────────────────────────────────────────

JELLYFIN_SOURCE = {"id": "s1", "type": "jellyfin", "name": "Home Jellyfin", "uri": "http://jf:8096",
                   "userId": "secret-user-id", "username": "someone", "libraries": [{"id": "l"}]}
PLEX_SOURCE = {"id": "s2", "type": "plex", "name": "Plex", "uri": "http://plex:32400", "accessToken": "TOKEN"}


@pytest.fixture
def tunarr(monkeypatch):
    seen = {}
    monkeypatch.setattr(status_router, "probe", lambda url, tunarr=False: {"ok": True})
    monkeypatch.setattr(channel_engine, "get_tunarr_version", lambda url: "2026.10.1")

    def fake_api(url, method, path, body=None, timeout=60):
        seen["path"] = path
        return seen.get("sources")
    monkeypatch.setattr(channel_engine, "api", fake_api)
    return seen


def test_the_connection_test_says_which_sources_tunarr_has(tunarr):
    tunarr["sources"] = [JELLYFIN_SOURCE]
    out = status_router.test_connection(status_router.ConnectionTest(tunarr_url="http://t"))
    assert out["tunarr"]["ok"] is True
    assert out["tunarr"]["sources"] == [{"type": "jellyfin", "name": "Home Jellyfin", "uri": "http://jf:8096"}]
    assert "plex" not in out  # nothing Plex was asked, so nothing Plex is reported


def test_no_user_ids_or_tokens_leave_the_server_in_the_sources(tunarr):
    tunarr["sources"] = [JELLYFIN_SOURCE, PLEX_SOURCE]
    out = status_router.test_connection(status_router.ConnectionTest(tunarr_url="http://t"))
    text = json.dumps(out)
    for secret in ("secret-user-id", "someone", "TOKEN"):
        assert secret not in text
    assert [s["type"] for s in out["tunarr"]["sources"]] == ["jellyfin", "plex"]


def test_a_failed_tunarr_test_reports_no_sources(monkeypatch):
    monkeypatch.setattr(status_router, "probe", lambda url, tunarr=False: {"ok": False, "error": "refused"})
    out = status_router.test_connection(status_router.ConnectionTest(tunarr_url="http://t"))
    assert out["tunarr"]["ok"] is False and "sources" not in out["tunarr"]


def test_tunarr_sources_survives_tunarr_saying_nothing(monkeypatch):
    monkeypatch.setattr(channel_engine, "api", lambda *a, **k: None)
    assert status_router.tunarr_sources("http://t") == []


def test_the_dashboard_status_carries_the_media_server_and_sources(cfg, tunarr, monkeypatch):
    cfg.write({"tunarr_url": "http://t", "media_server": "jellyfin"})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())
    tunarr["sources"] = [JELLYFIN_SOURCE]
    out = status_router.get_status()
    assert out["media_server"] == "jellyfin"
    assert out["tunarr"]["sources"][0]["type"] == "jellyfin"
    assert out["plex"]["ok"] is False  # nothing to check: no Plex configured, and none is needed
