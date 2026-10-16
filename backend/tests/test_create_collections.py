"""test_create_collections.py — the deploy (create.py) accepts collections on Plex OR Jellyfin.

It used to stop with "plex_url and plex_token required ... for collection support" before
asking anything, so a Jellyfin-only setup (beta) could never deploy a collection channel.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import create


def test_a_jellyfin_only_setup_can_use_collections(monkeypatch):
    monkeypatch.setattr(create, "get_plex_sections", lambda *a: pytest.fail("Plex was asked"))
    assert create.collection_lookup_sections(True, "", "", has_jellyfin=True) == []


def test_with_plex_its_sections_are_read(monkeypatch):
    monkeypatch.setattr(create, "get_plex_sections", lambda url, token: [{"key": "1"}])
    assert create.collection_lookup_sections(True, "http://p", "x", has_jellyfin=True) == [{"key": "1"}]


def test_no_server_at_all_stops_with_a_clear_error(capsys):
    with pytest.raises(SystemExit):
        create.collection_lookup_sections(True, "", "", has_jellyfin=False)
    assert "Plex (URL + token) or Jellyfin (URL + API key)" in capsys.readouterr().out


def test_no_collections_needs_no_server(monkeypatch):
    monkeypatch.setattr(create, "get_plex_sections", lambda *a: pytest.fail("Plex was asked"))
    assert create.collection_lookup_sections(False, "", "", has_jellyfin=False) == []
