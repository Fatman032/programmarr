"""test_media_server_setup.py — set up Plex, Jellyfin (beta), or both.

Plex is a URL + token, tested against Plex; Jellyfin is a URL + API key, tested against Jellyfin.
Fill in either pair, or both: you're set up once Tunarr and at least one server are filled in, and
everything that talks to a media server talks to whichever ones are set up. (This replaced an
earlier one-or-the-other switch, `media_server`, which is now ignored and cleaned out on save.)
Nothing changes for a Plex-only setup.
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


PLEX = {"plex_url": "http://p", "plex_token": "x"}
JFP = {"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "KEY"}
JF = {"tunarr_url": "http://t", **JFP}


# ── "configured": Tunarr + at least one server's address and key ────────────────────

def test_plex_only_is_configured(cfg):
    cfg.write({"tunarr_url": "http://t", **PLEX})
    status = config_router.config_status()
    assert status["configured"] is True and status["servers"] == ["plex"]


def test_jellyfin_only_is_configured(cfg):
    cfg.write(JF)
    status = config_router.config_status()
    assert status["configured"] is True and status["servers"] == ["jellyfin"]


def test_both_are_configured_together(cfg):
    cfg.write({**JF, **PLEX})
    status = config_router.config_status()
    assert status["configured"] is True and status["servers"] == ["plex", "jellyfin"]


def test_tunarr_alone_is_not_configured(cfg):
    cfg.write({"tunarr_url": "http://t"})
    assert config_router.config_status()["configured"] is False


@pytest.mark.parametrize("missing", ["jellyfin_url", "jellyfin_api_key"])
def test_half_a_jellyfin_pair_does_not_count(cfg, missing):
    cfg.write({k: v for k, v in JF.items() if k != missing})
    status = config_router.config_status()
    assert status["configured"] is False and status["servers"] == []


def test_a_server_without_tunarr_is_not_configured(cfg):
    cfg.write(JFP)
    assert config_router.config_status()["configured"] is False


def test_the_old_switch_is_ignored(cfg):
    """A config saved by the earlier one-or-the-other version still works by its filled-in pairs."""
    cfg.write({**JF, **PLEX, "media_server": "jellyfin"})
    assert config_router.config_status()["servers"] == ["plex", "jellyfin"]


def test_no_config_at_all_is_not_configured(cfg):
    assert config_router.config_status() == {"configured": False, "servers": [], "has_tmdb": False, "has_auth": False}


# ── the key is hidden and kept, like the Plex token ─────────────────────────────────

def test_the_api_key_is_never_sent_back_to_the_screen(cfg):
    cfg.write(JF)
    assert config_router.get_config()["jellyfin_api_key"] == MASK


def test_saving_with_the_masked_key_keeps_the_real_one(cfg):
    cfg.write(JF)
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", jellyfin_url="http://jf:8096",
                                                        jellyfin_api_key=MASK))
    assert cfg.read()["jellyfin_api_key"] == "KEY"


def test_saving_both_keeps_both_pairs(cfg):
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", **PLEX, **JFP))
    saved = cfg.read()
    assert saved["plex_url"] == "http://p" and saved["jellyfin_url"] == "http://jf:8096"
    assert config_router.config_status()["servers"] == ["plex", "jellyfin"]


def test_clearing_one_pair_leaves_the_other(cfg):
    cfg.write({**JF, **PLEX})
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", **PLEX))  # Jellyfin fields left blank
    saved = cfg.read()
    assert "jellyfin_url" not in saved and saved["plex_url"] == "http://p"
    assert config_router.config_status()["servers"] == ["plex"]


def test_a_save_cleans_out_the_old_switch(cfg):
    cfg.write({**JF, "media_server": "jellyfin"})
    config_router.save_config(config_router.ConfigModel(tunarr_url="http://t", **JFP))
    assert "media_server" not in cfg.read()


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
    assert out["servers"] == ["jellyfin"] and out["jellyfin"]["ok"] is True
    assert out["plex"] == {"ok": False, "error": "Not configured", "url": ""}


def test_without_jellyfin_settings_the_dashboard_says_not_configured(cfg, monkeypatch):
    cfg.write({"tunarr_url": ""})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())
    out = status_router.get_status()
    assert out["jellyfin"] == {"ok": False, "error": "Not configured", "url": ""} and out["servers"] == []


def test_the_dashboard_checks_both_when_both_are_set_up(cfg, jellyfin, monkeypatch):
    cfg.write({**JF, **PLEX, "jellyfin_api_key": "GOOD"})
    monkeypatch.setattr(status_router, "load_config", lambda: cfg.read())
    monkeypatch.setattr(status_router, "probe", lambda url, tunarr=False, headers=None, who="Plex":
                        {"ok": tunarr or "X-Plex-Token=x" in url or (headers or {}).get("X-Emby-Token") == "GOOD"})
    monkeypatch.setattr(channel_engine, "get_tunarr_version", lambda url: "2026.10.1")
    out = status_router.get_status()
    assert out["servers"] == ["plex", "jellyfin"] and out["plex"]["ok"] and out["jellyfin"]["ok"]


def test_the_connection_test_checks_both_when_both_are_given(cfg, jellyfin, monkeypatch):
    monkeypatch.setattr(status_router, "check_tunarr", lambda url, u="", p="": {"ok": True, "url": url})
    monkeypatch.setattr(status_router, "check_plex", lambda url, token: {"ok": True, "url": url})
    out = status_router.test_connection(status_router.ConnectionTest(
        tunarr_url="http://t", plex_url="http://p", plex_token="x", jellyfin_url="http://jf:8096", jellyfin_api_key="GOOD"))
    assert out["tunarr"]["ok"] and out["plex"]["ok"] and out["jellyfin"]["ok"]
