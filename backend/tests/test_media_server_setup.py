"""test_media_server_setup.py — Jellyfin (beta) is set up exactly like Plex.

With Plex the wizard asks for a Plex URL + token, tests them against Plex, and you're set up once
Tunarr and Plex are both filled in. Jellyfin works the same way with its own pair: a Jellyfin URL +
API key, tested against Jellyfin, and set up once Tunarr and Jellyfin are both filled in.
`media_server` ("plex", the default, or "jellyfin") says which pair applies. Nothing changes for Plex.
"""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
from routers import config_router, status_router
from routers.config_router import MASK


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config_router, "DATA_DIR", tmp_path)
    channel_engine.set_tunarr_auth()
    holder = type("Cfg", (), {})()
    holder.write = lambda d: (tmp_path / "config.json").write_text(json.dumps(d))
    holder.read = lambda: json.loads((tmp_path / "config.json").read_text())
    return holder


JF = {"tunarr_url": "http://t", "media_server": "jellyfin", "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "KEY"}


# ── "configured": Tunarr + the chosen server's address and key ──────────────────────

def test_a_plex_setup_still_needs_tunarr_and_plex(cfg):
    cfg.write({"tunarr_url": "http://t"})
    assert config_router.config_status()["configured"] is False
    cfg.write({"tunarr_url": "http://t", "plex_url": "http://p", "plex_token": "x"})
    status = config_router.config_status()
    assert status["configured"] is True and status["media_server"] == "plex"


def test_a_jellyfin_setup_needs_tunarr_and_jellyfin_url_and_key(cfg):
    cfg.write(JF)
    status = config_router.config_status()
    assert status["configured"] is True and status["media_server"] == "jellyfin"


@pytest.mark.parametrize("missing", ["tunarr_url", "jellyfin_url", "jellyfin_api_key"])
def test_a_jellyfin_setup_missing_any_piece_is_not_configured(cfg, missing):
    cfg.write({k: v for k, v in JF.items() if k != missing})
    assert config_router.config_status()["configured"] is False


def test_a_plex_pair_does_not_count_for_a_jellyfin_setup(cfg):
    cfg.write({"tunarr_url": "http://t", "media_server": "jellyfin", "plex_url": "http://p", "plex_token": "x"})
    assert config_router.config_status()["configured"] is False


def test_a_jellyfin_pair_does_not_count_for_a_plex_setup(cfg):
    cfg.write({"tunarr_url": "http://t", "jellyfin_url": "http://jf", "jellyfin_api_key": "k"})
    assert config_router.config_status()["configured"] is False


def test_no_config_at_all_is_plex_and_not_configured(cfg):
    assert config_router.config_status() == {"configured": False, "media_server": "plex", "has_tmdb": False, "has_auth": False}


# ── the key is hidden and kept, like the Plex token ─────────────────────────────────

def test_the_api_key_is_never_sent_back_to_the_screen(cfg):
    cfg.write(JF)
    assert config_router.get_config()["jellyfin_api_key"] == MASK


def test_saving_with_the_masked_key_keeps_the_real_one(cfg):
    cfg.write(JF)
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", media_server="jellyfin",
                                                        jellyfin_url="http://jf:8096", jellyfin_api_key=MASK))
    assert cfg.read()["jellyfin_api_key"] == "KEY"


def test_the_wizard_save_stores_the_jellyfin_pair_and_no_plex_fields(cfg):
    config_router.save_config(config_router.ConfigModel(**JF))
    saved = cfg.read()
    assert saved["media_server"] == "jellyfin" and saved["jellyfin_url"] == "http://jf:8096"
    assert saved["jellyfin_api_key"] == "KEY" and "plex_url" not in saved and "plex_token" not in saved
    assert config_router.config_status()["configured"] is True


def test_switching_back_to_plex_clears_the_choice(cfg):
    cfg.write(JF)
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", plex_url="http://p", plex_token="tok"))
    assert "media_server" not in cfg.read()
    assert config_router.config_status()["media_server"] == "plex"


# ── the Jellyfin connection check ───────────────────────────────────────────────────

class _Resp:
    status = 200
    def read(self):
        return b"{}"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


@pytest.fixture
def jellyfin(monkeypatch):
    """A fake Jellyfin: answers /System/Info only with the right key."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        if seen["headers"].get("x-emby-token") != "GOOD":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        return _Resp()
    monkeypatch.setattr(status_router.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_a_good_key_is_reported_connected(jellyfin):
    r = status_router.check_jellyfin("http://jf:8096/", "GOOD")
    assert r["ok"] is True and r["url"] == "http://jf:8096"
    assert jellyfin["url"] == "http://jf:8096/System/Info"  # needs a real key, unlike /System/Info/Public
    assert jellyfin["headers"]["authorization"] == 'MediaBrowser Token="GOOD"'


def test_a_wrong_key_says_jellyfin_rejected_it(jellyfin):
    r = status_router.check_jellyfin("http://jf:8096", "WRONG")
    assert r["ok"] is False and "Jellyfin rejected the API key" in r["error"]


@pytest.mark.parametrize("url,key,error", [("", "GOOD", "No Jellyfin URL given"), ("http://jf", "", "No Jellyfin API key given")])
def test_missing_pieces_are_named(url, key, error):
    assert status_router.check_jellyfin(url, key) == {"ok": False, "error": error}


def test_the_plex_message_is_unchanged(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(status_router.urllib.request, "urlopen", fake_urlopen)
    assert "Plex rejected the token" in status_router.probe("http://p/x")["error"]


def test_the_connection_test_checks_jellyfin_when_given(cfg, jellyfin, monkeypatch):
    monkeypatch.setattr(status_router, "check_tunarr", lambda url, u="", p="": {"ok": True, "url": url})
    out = status_router.test_connection(status_router.ConnectionTest(
        tunarr_url="http://t", jellyfin_url="http://jf:8096", jellyfin_api_key="GOOD"))
    assert out["tunarr"]["ok"] and out["jellyfin"]["ok"] and "plex" not in out


def test_the_connection_test_uses_the_saved_key_when_the_screen_sends_the_mask(cfg, jellyfin, monkeypatch):
    cfg.write({**JF, "jellyfin_api_key": "GOOD"})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())  # status reads config its own way
    out = status_router.test_connection(status_router.ConnectionTest(jellyfin_url="http://jf:8096", jellyfin_api_key=MASK))
    assert out["jellyfin"]["ok"] is True


def test_the_dashboard_status_checks_jellyfin(cfg, jellyfin, monkeypatch):
    cfg.write({**JF, "jellyfin_api_key": "GOOD"})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())
    monkeypatch.setattr(status_router, "probe", lambda url, tunarr=False, headers=None, who="Plex":
                        {"ok": (headers or {}).get("X-Emby-Token") == "GOOD" or tunarr})
    monkeypatch.setattr(channel_engine, "get_tunarr_version", lambda url: "2026.10.1")
    out = status_router.get_status()
    assert out["media_server"] == "jellyfin" and out["jellyfin"]["ok"] is True
    assert out["plex"] == {"ok": False, "error": "Not configured", "url": ""}


def test_without_jellyfin_settings_the_dashboard_says_not_configured(cfg, monkeypatch):
    cfg.write({"tunarr_url": "", "media_server": "jellyfin"})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())
    assert status_router.get_status()["jellyfin"] == {"ok": False, "error": "Not configured", "url": ""}
