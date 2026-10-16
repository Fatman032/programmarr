"""test_sync_plex.py — the Plex sync says when the one-time Plex steps are needed.

The deploy screen shows its "One more step — add your channels to Plex" box only when the sync
fails. The sync used to print the manual steps but still exit 0, so the box never appeared.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_plex

CHANNELS = {"1": "C1.1.tunarr.com", "2": "C2.2.tunarr.com"}


def _device(*nums):
    return {"key": "d", "ChannelMapping": [{"deviceIdentifier": n} for n in nums]}


@pytest.fixture
def run(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"tunarr_url": "http://t", "plex_url": "http://p", "plex_token": "x"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sync_plex, "get_tunarr_channels", lambda url: dict(CHANNELS))

    def go(dvr_states, accepted=True, *argv):
        states = iter(dvr_states)
        monkeypatch.setattr(sync_plex, "get_plex_dvr", lambda url, token: next(states))
        monkeypatch.setattr(sync_plex, "attempt_soft_update", lambda *a: accepted)
        monkeypatch.setattr(sys, "argv", ["sync_plex.py", *argv])
        try:
            sync_plex.main()
            return 0
        except SystemExit as e:
            return e.code
    return go


def test_no_dvr_yet_needs_the_manual_steps(run, capsys):
    assert run([(None, None)]) == 2
    assert "http://t/api/xmltv.xml" in capsys.readouterr().out


def test_every_channel_already_mapped_is_success(run):
    assert run([({"key": "1"}, _device("1", "2"))]) == 0


def test_new_channels_mapped_automatically_is_success(run):
    assert run([({"key": "1"}, _device("1")), ({"key": "1"}, _device("1", "2"))]) == 0


def test_plex_refusing_the_new_channels_needs_the_manual_steps(run):
    assert run([({"key": "1"}, _device("1"))], False) == 2


def test_plex_accepting_but_not_adding_needs_the_manual_steps(run):
    assert run([({"key": "1"}, _device("1")), ({"key": "1"}, _device("1"))]) == 2


def test_probe_is_never_a_failure(run):
    assert run([({"key": "1"}, _device("1"))], True, "--probe") == 0
