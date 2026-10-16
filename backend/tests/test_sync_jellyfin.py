"""test_sync_jellyfin.py — after a deploy, Jellyfin's Live TV guide is refreshed (BETA).

The Jellyfin side of the Plex sync. You add Tunarr to Jellyfin Live TV once by hand (M3U tuner +
XMLTV guide); after that each deploy has Jellyfin run its Refresh Guide task so new channels show
up. Exit 2 (the deploy screen shows the one-time steps) when the tuner or guide isn't there yet,
or channels are still missing after the refresh; exit 1 when Jellyfin or Tunarr can't be read.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_jellyfin

TUNARR = "http://host.docker.internal:8265"
TUNER = {"Id": "t1", "Type": "m3u", "Url": "http://192.168.1.5:8265/api/channels.m3u"}
GUIDE = {"Id": "g1", "Type": "xmltv", "Path": "http://192.168.1.5:8265/api/xmltv.xml"}


# ── recognising Tunarr in Jellyfin's Live TV setup ──────────────────────────────────

def test_the_m3u_tuner_is_found_even_at_a_different_address():
    assert sync_jellyfin.find_tunarr_tuner({"TunerHosts": [TUNER]}, TUNARR) == TUNER


def test_tunarr_as_an_hdhomerun_tuner_is_found_by_its_port():
    hdhr = {"Type": "hdhomerun", "Url": "http://192.168.1.5:8265"}
    assert sync_jellyfin.find_tunarr_tuner({"TunerHosts": [hdhr]}, TUNARR) == hdhr


def test_other_tuners_are_not_tunarr():
    others = [{"Type": "m3u", "Url": "http://iptv.example/list.m3u"},
              {"Type": "hdhomerun", "Url": "http://192.168.1.9"}]
    assert sync_jellyfin.find_tunarr_tuner({"TunerHosts": others}, TUNARR) is None


def test_the_xmltv_guide_is_found():
    other = {"Type": "schedulesdirect", "Path": ""}
    assert sync_jellyfin.find_tunarr_guide({"ListingProviders": [other, GUIDE]}) == GUIDE


def test_nothing_set_up_finds_nothing():
    assert sync_jellyfin.find_tunarr_tuner({"TunerHosts": None}, TUNARR) is None
    assert sync_jellyfin.find_tunarr_guide({}) is None


# ── a run ───────────────────────────────────────────────────────────────────────────

class FakeJellyfin:
    """Jellyfin's API as sync_jellyfin uses it: Live TV settings, channels, scheduled tasks."""

    def __init__(self, livetv, channels, after_refresh=None, refresh_status="Completed"):
        self.livetv, self.channels = livetv, list(channels)
        self.after_refresh = after_refresh if after_refresh is not None else list(channels)
        self.refresh_status = refresh_status
        self.task = {"Id": "rg", "Key": "RefreshGuide", "State": "Idle",
                     "LastExecutionResult": {"EndTimeUtc": "old", "Status": "Completed"}}
        self.started = 0

    def get(self, url, key, path, timeout=120):
        if path == "/System/Configuration/livetv":
            return self.livetv
        if path.startswith("/LiveTv/Channels"):
            return {"Items": [{"Number": n} for n in self.channels]}
        if path == "/ScheduledTasks":
            return [{"Key": "Other"}, self.task]
        if path == "/ScheduledTasks/rg":
            return self.task
        raise AssertionError(path)

    def post(self, url, key, path, timeout=30):
        assert path == "/ScheduledTasks/Running/rg"
        self.started += 1
        self.task = {**self.task, "LastExecutionResult": {"EndTimeUtc": "new", "Status": self.refresh_status}}
        self.channels = self.after_refresh
        return True


@pytest.fixture
def run(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"tunarr_url": TUNARR, "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "KEY"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sync_jellyfin, "POLL_S", 0)
    monkeypatch.setattr(sync_jellyfin, "get_tunarr_channels",
                        lambda url: {n: f"C{n}.x.tunarr.com" for n in ("1", "2", "3", "4", "5")})

    def go(fake, *argv):
        monkeypatch.setattr(sync_jellyfin.jellyfin_api, "get", fake.get)
        monkeypatch.setattr(sync_jellyfin.jellyfin_api, "post", fake.post)
        monkeypatch.setattr(sys, "argv", ["sync_jellyfin.py", *argv])
        try:
            sync_jellyfin.main()
            return 0
        except SystemExit as e:
            return e.code
    return go


SET_UP = {"TunerHosts": [TUNER], "ListingProviders": [GUIDE]}


def test_a_new_channel_is_added_by_refreshing_the_guide(run, capsys):
    fake = FakeJellyfin(SET_UP, ["1", "2", "3", "4"], after_refresh=["1", "2", "3", "4", "5"])
    assert run(fake) == 0
    assert fake.started == 1
    assert "all 5 channels are in Jellyfin (1 added)" in capsys.readouterr().out


def test_the_guide_is_refreshed_even_when_every_channel_is_there(run):
    """A deploy changes what's on the channels, so the guide is refreshed every time."""
    fake = FakeJellyfin(SET_UP, ["1", "2", "3", "4", "5"])
    assert run(fake) == 0 and fake.started == 1


def test_no_tunarr_tuner_yet_shows_the_one_time_steps(run, capsys):
    fake = FakeJellyfin({"TunerHosts": [], "ListingProviders": [GUIDE]}, [])
    assert run(fake) == 2
    out = capsys.readouterr().out
    assert "doesn't have a Tunarr tuner yet" in out
    assert f"{TUNARR}/api/channels.m3u" in out and fake.started == 0


def test_no_guide_yet_shows_the_one_time_steps(run):
    fake = FakeJellyfin({"TunerHosts": [TUNER], "ListingProviders": []}, [])
    assert run(fake) == 2 and fake.started == 0


def test_channels_still_missing_after_the_refresh_show_the_steps(run, capsys):
    fake = FakeJellyfin(SET_UP, ["1", "2"], after_refresh=["1", "2"])
    assert run(fake) == 2
    assert "still doesn't have 3 channels: #3, #4, #5" in capsys.readouterr().out


def test_a_failed_refresh_is_an_error(run):
    fake = FakeJellyfin(SET_UP, ["1", "2", "3", "4", "5"], refresh_status="Failed")
    assert run(fake) == 1


def test_jellyfin_unreadable_is_an_error(run):
    fake = FakeJellyfin(None, [])
    assert run(fake) == 1


def test_probe_changes_nothing(run, capsys):
    fake = FakeJellyfin(SET_UP, ["1", "2", "3", "4"])
    assert run(fake, "--probe") == 0
    assert fake.started == 0 and "#5" in capsys.readouterr().out
