"""test_channel_rename.py — renaming a channel in the editor reaches Tunarr on Apply.

Before: Save wrote the new name to channels.json, then Apply's name-match guard compared
Tunarr's channel (still the old name) to the new name and refused. Now the entry carries
`tunarr_name` (every name Tunarr might still have) until a successful Apply renames the
channel.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
import scheduler

REAL_UPDATE = channel_engine.update_channel_in_place
REAL_FIND = channel_engine.find_channel_by_number
from routers import channels_router
from test_live_update_guard import RESOLVED
from test_notices import server  # noqa: F401  (fixture: temp data dir + fake library)


# ── the engine ──────────────────────────────────────────────────────────────────────

def test_a_rename_remembers_the_name_tunarr_has():
    new = channel_engine.note_rename({"name": "Comedy TV"}, {"name": "Laughs"})
    assert new["tunarr_name"] == ["Comedy TV"]


def test_two_renames_before_apply_keep_both_earlier_names():
    """'Laughs' was never confirmed, but an Apply may have set it in Tunarr (a timed-out PUT)."""
    once = channel_engine.note_rename({"name": "Comedy TV"}, {"name": "Laughs"})
    twice = channel_engine.note_rename(once, {"name": "Big Laughs"})
    assert twice["tunarr_name"] == ["Comedy TV", "Laughs"]


def test_renaming_back_drops_that_name_but_keeps_the_unconfirmed_one():
    once = channel_engine.note_rename({"name": "Comedy TV"}, {"name": "Laughs"})
    back = channel_engine.note_rename(once, {"name": "comedy tv", "tunarr_name": "stale"})
    assert back["tunarr_name"] == ["Laughs"]  # the client's own tunarr_name is ignored


def test_an_old_string_tunarr_name_still_reads():
    assert channel_engine.pending_names({"name": "Laughs", "tunarr_name": "Comedy TV"}) == ["Comedy TV"]
    assert channel_engine.guard_names({"name": "Laughs", "tunarr_name": "Comedy TV"}) == ["Comedy TV", "Laughs"]
    again = channel_engine.note_rename({"name": "Laughs", "tunarr_name": "Comedy TV"}, {"name": "Big Laughs"})
    assert again["tunarr_name"] == ["Comedy TV", "Laughs"]


def test_pending_names_are_deduped_ignoring_case():
    entry = {"name": "Z", "tunarr_name": ["X", " x ", "", None, "Y"]}
    assert channel_engine.pending_names(entry) == ["X", "Y"]
    again = channel_engine.note_rename({"name": "y", "tunarr_name": ["X", "Y"]}, {"name": "Z"})
    assert again["tunarr_name"] == ["X", "y"]  # re-added as the most recent, not twice


def test_rename_back_and_forth_keeps_every_name_tunarr_might_have():
    """Confirmed 'A'; renamed A→B, B→A, A→C with no Apply. Tunarr has A (or B, if an
    Apply timed out after landing), so the guard must accept both."""
    entry = {"name": "A"}
    for name in ("B", "A", "C"):
        entry = channel_engine.note_rename(entry, {"name": name})
    guard = channel_engine.guard_names(entry)
    assert channel_engine.name_matches("A", guard)
    assert channel_engine.name_matches("B", guard)
    assert channel_engine.name_matches("C", guard)


def test_a_save_without_a_rename_adds_nothing():
    assert "tunarr_name" not in channel_engine.note_rename({"name": "A"}, {"name": "A"})


@pytest.fixture
def fake_tunarr(monkeypatch):
    rec = type("Rec", (), {"channels": [], "puts": [], "refuse_put": False})()

    def fake_api(tunarr_url, method, path, body=None, timeout=60):
        if method == "GET" and path == "/api/channels":
            return rec.channels
        if method == "GET" and path.startswith("/api/channels/"):
            cid = path.rsplit("/", 1)[1]
            return next(dict(c) for c in rec.channels if c["id"] == cid)
        if method == "PUT" and path.startswith("/api/channels/"):
            rec.puts.append(body)
            return None if rec.refuse_put else body
        raise AssertionError(f"unexpected api call: {method} {path}")

    monkeypatch.setattr(channel_engine, "api", fake_api)
    monkeypatch.setattr(channel_engine, "set_programming", lambda *a, **k: {"ok": True})
    return rec


def test_update_in_place_renames_the_tunarr_channel(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Comedy TV"}]
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV", new_name="Laughs")
    assert [p["name"] for p in fake_tunarr.puts] == ["Laughs"]
    assert fake_tunarr.puts[0]["id"] == "abc"  # same channel, so the DVR mapping stays


def test_a_capitals_only_rename_still_reaches_tunarr(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "comedy tv"}]
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name="comedy tv", new_name="Comedy TV")
    assert [p["name"] for p in fake_tunarr.puts] == ["Comedy TV"]


def test_no_rename_call_when_the_name_already_matches(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Comedy TV"}]
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV", new_name="Comedy TV")
    assert fake_tunarr.puts == []


def test_a_refused_rename_says_so(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Comedy TV"}]
    fake_tunarr.refuse_put = True
    with pytest.raises(channel_engine.ChannelEngineError) as e:
        channel_engine.update_channel_in_place(
            "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV", new_name="Laughs")
    assert "new name" in str(e.value)


# ── Save and Apply ─────────────────────────────────────────────────────────────────

def _fake_update(calls, tunarr_has="Test"):
    """Stands in for update_channel_in_place: records the call and reports the name the
    Tunarr channel ends up with, the way the real one does (renamed unless blank)."""
    def update(*a, **k):
        calls.append(k)
        if k.get("outcome") is not None:
            new = (k.get("new_name") or "").strip()
            k["outcome"]["name"] = new or tunarr_has
    return update


@pytest.fixture
def renamed(server, monkeypatch):  # noqa: F811
    """Channel #1 is 'Test' in Tunarr; the editor saves it as 'Renamed'."""
    calls = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place", _fake_update(calls))
    ch = {"number": 1, "name": "Test", "shuffle": "shuffle", "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    channels_router.update_channel(1, {**ch, "name": "Renamed"})
    return calls


def _saved(tmp_path):
    return json.loads((tmp_path / "channels.json").read_text())["channels"][0]


def test_save_remembers_the_old_name(server, renamed):  # noqa: F811
    assert _saved(server)["name"] == "Renamed"
    assert _saved(server)["tunarr_name"] == ["Test"]


def test_apply_checks_the_old_name_and_sets_the_new_one(server, renamed):  # noqa: F811
    asyncio.run(channels_router.apply_channel(1))
    assert renamed[0]["expected_name"] == ["Test", "Renamed"]
    assert renamed[0]["new_name"] == "Renamed"
    assert "tunarr_name" not in _saved(server)  # Tunarr has the new name now


def test_a_failed_apply_keeps_the_old_name(server, renamed, monkeypatch):  # noqa: F811
    def refuse(*a, **k):
        raise channel_engine.ChannelEngineError("Tunarr said no")
    monkeypatch.setattr(channel_engine, "update_channel_in_place", refuse)
    with pytest.raises(HTTPException):
        asyncio.run(channels_router.apply_channel(1))
    assert _saved(server)["tunarr_name"] == ["Test"]


def test_the_auto_update_still_runs_on_a_renamed_live_channel(server, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda *a, **k: calls.append(k))
    ch = {"number": 1, "name": "Renamed", "tunarr_name": "Test", "live": True,
          "shuffle": "shuffle", "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    summary = scheduler._run_cycle_blocking(apply=True)
    assert not [s for s in summary["skipped"] if "mismatch" in s["reason"]]
    assert calls and channel_engine.name_matches("Test", calls[0]["expected_name"])


# ── a stale tunarr_name can't lock the channel out ─────────────────────────────────

def test_the_guard_accepts_the_new_name_when_tunarr_name_is_stale(fake_tunarr):
    """Renamed in Tunarr's own UI (or a rename PUT that timed out after Tunarr applied it):
    Tunarr already has the new name, so the old tunarr_name no longer matches."""
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Laughs"}]
    entry = {"name": "Laughs", "tunarr_name": "Comedy TV"}
    outcome = {}
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name=channel_engine.guard_names(entry),
        new_name="Laughs", outcome=outcome)
    assert fake_tunarr.puts == []          # already named: no rename call
    assert outcome["name"] == "Laughs"


def test_the_guard_still_accepts_the_old_name(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": " comedy tv "}]
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED,
        expected_name=channel_engine.guard_names({"name": "Laughs", "tunarr_name": "Comedy TV"}),
        new_name="Laughs")
    assert [p["name"] for p in fake_tunarr.puts] == ["Laughs"]


def test_the_guard_refuses_a_third_name(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Westerns"}]
    with pytest.raises(channel_engine.ChannelEngineError) as e:
        channel_engine.update_channel_in_place(
            "http://t", 11, "ordered", RESOLVED,
            expected_name=channel_engine.guard_names({"name": "Laughs", "tunarr_name": "Comedy TV"}),
            new_name="Laughs")
    assert "mismatch" in str(e.value)
    assert fake_tunarr.puts == []


def test_guard_names_are_both_names_once():
    assert channel_engine.guard_names({"name": "Laughs", "tunarr_name": "Comedy TV"}) == ["Comedy TV", "Laughs"]
    assert channel_engine.guard_names({"name": "Laughs", "tunarr_name": "laughs "}) == ["laughs "]
    assert channel_engine.guard_names({"name": "Laughs"}) == ["Laughs"]


def test_the_scheduler_accepts_the_new_name_when_tunarr_name_is_stale(server, monkeypatch):  # noqa: F811
    """The server fixture's Tunarr channel is 'Test'; the record's tunarr_name is stale."""
    calls = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place", _fake_update(calls))
    ch = {"number": 1, "name": "Test", "tunarr_name": "Old Name", "live": True,
          "shuffle": "shuffle", "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    summary = scheduler._run_cycle_blocking(apply=True)
    assert not [s for s in summary["skipped"] if "mismatch" in s["reason"]]
    assert calls and channel_engine.name_matches("Test", calls[0]["expected_name"])


def test_the_scheduler_still_refuses_a_third_name(server, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place", _fake_update(calls))
    ch = {"number": 1, "name": "Laughs", "tunarr_name": "Comedy TV", "live": True,
          "shuffle": "shuffle", "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    summary = scheduler._run_cycle_blocking(apply=True)
    assert [s for s in summary["skipped"] if "mismatch" in s["reason"]]
    assert calls == []


def test_apply_with_a_stale_tunarr_name_clears_it(server, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place", _fake_update(calls))
    ch = {"number": 1, "name": "Test", "tunarr_name": "Old Name", "shuffle": "shuffle",
          "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    asyncio.run(channels_router.apply_channel(1))
    assert channel_engine.name_matches("Test", calls[0]["expected_name"])
    assert channel_engine.name_matches("Old Name", calls[0]["expected_name"])
    assert "tunarr_name" not in _saved(server)


# ── a blank name never reaches the record ─────────────────────────────────────────

@pytest.mark.parametrize("blank", ["", "   ", None])
def test_save_refuses_a_blank_name(server, blank):  # noqa: F811
    ch = {"number": 1, "name": "Test", "shuffle": "shuffle", "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    with pytest.raises(HTTPException) as e:
        channels_router.update_channel(1, {**ch, "name": blank})
    assert e.value.status_code == 400
    assert _saved(server) == ch


def test_a_blank_new_name_is_not_sent_and_reports_tunarrs_name(fake_tunarr):
    fake_tunarr.channels = [{"number": 11, "id": "abc", "name": "Comedy TV"}]
    outcome = {}
    channel_engine.update_channel_in_place(
        "http://t", 11, "ordered", RESOLVED, expected_name="Comedy TV", new_name="  ",
        outcome=outcome)
    assert fake_tunarr.puts == []
    assert outcome["name"] == "Comedy TV"


# ── a Save that lands while Apply runs ─────────────────────────────────────────────

def test_a_rename_saved_during_apply_keeps_the_applied_name(server, monkeypatch):  # noqa: F811
    """Apply sets 'Renamed' in Tunarr; meanwhile the editor saves 'Renamed Again'. The
    record must remember Tunarr has 'Renamed': not drop tunarr_name, and not keep 'Test'."""
    calls = []
    applied = _fake_update(calls)

    def update_while_saving(*a, **k):
        applied(*a, **k)
        cur = json.loads((server / "channels.json").read_text())["channels"][0]
        channels_router.update_channel(1, {**cur, "name": "Renamed Again"})

    monkeypatch.setattr(channel_engine, "update_channel_in_place", update_while_saving)
    ch = {"number": 1, "name": "Renamed", "tunarr_name": "Test", "shuffle": "shuffle",
          "content": ["2 Guns"]}
    (server / "channels.json").write_text(json.dumps({"channels": [ch]}))
    asyncio.run(channels_router.apply_channel(1))
    saved = _saved(server)
    assert saved["name"] == "Renamed Again"
    assert saved["tunarr_name"] == ["Renamed"]


# ── Build Channels (surgical deploy) ──────────────────────────────────────────────

@pytest.fixture
def build(pr, monkeypatch):
    """Three Plex channels, each with a rename saved in the editor but not applied:
    #1 unchanged by the run, #2 an update that fails, #3 an update that succeeds."""
    d = pr._test_data_dir
    (d / "config.json").write_text(json.dumps(
        {"tunarr_url": "http://t", "plex_url": "http://p", "plex_token": "x"}))
    deployed = [
        {"number": 1, "name": "Laughs", "tunarr_name": "Comedy TV", "server": "plex",
         "shuffle": "shuffle", "content": ["A"]},
        {"number": 2, "name": "Big Drama", "tunarr_name": "Drama", "server": "plex",
         "shuffle": "shuffle", "content": ["B"]},
        {"number": 3, "name": "Docs Plus", "tunarr_name": "Docs", "server": "plex",
         "shuffle": "shuffle", "content": ["D"]},
    ]
    (d / "channels.json").write_text(json.dumps({"channels": deployed}))
    (d / "planner_state.json").write_text(json.dumps(
        {"managed_names": ["Laughs", "Big Drama", "Docs Plus"]}))
    draft = [
        {"number": 10, "name": "Laughs", "server": "plex", "shuffle": "shuffle", "content": ["A"]},
        {"number": 11, "name": "Big Drama", "server": "plex", "shuffle": "shuffle", "content": ["new"]},
        {"number": 12, "name": "Docs Plus", "server": "plex", "shuffle": "shuffle", "content": ["new"]},
    ]
    (d / "channels.draft.json").write_text(json.dumps({"server": "plex", "channels": draft}))

    calls = []

    def update(url, n, *a, **k):
        calls.append((n, k))
        if n == 2:
            raise channel_engine.ChannelEngineError("Tunarr said no")
        k["outcome"]["name"] = k["new_name"].strip()

    monkeypatch.setattr(channel_engine, "update_channel_in_place", update)
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids", lambda *a, **k: ({}, {}, None))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    monkeypatch.setattr(channel_engine, "resolve_content", lambda *a, **k: (RESOLVED, []))
    monkeypatch.setattr(scheduler, "record_applied", lambda *a, **k: None)
    pr._update_calls = calls
    return pr


def _run_build(pr):
    resp = asyncio.run(pr.run_surgical_deploy(pr.SurgicalDeployRequest()))

    async def drain():
        async for _ in resp.body_iterator:
            pass
    asyncio.run(drain())
    data = json.loads((pr._test_data_dir / "channels.json").read_text())
    return {c["name"]: c for c in data["channels"]}


def test_build_channels_keeps_tunarr_name_where_it_didnt_rename(build):
    got = _run_build(build)
    # (the old string form is written back as a list)
    assert got["Laughs"]["tunarr_name"] == ["Comedy TV"]  # unchanged: never touched in Tunarr
    assert got["Big Drama"]["tunarr_name"] == ["Drama"]   # update failed: Tunarr kept the old name
    assert "tunarr_name" not in got["Docs Plus"]          # updated and renamed
    by_number = dict(build._update_calls)
    assert sorted(by_number) == [2, 3]
    assert channel_engine.name_matches("Docs", by_number[3]["expected_name"])
    assert channel_engine.name_matches("Docs Plus", by_number[3]["expected_name"])


# ── Save and Apply through the real engine, against a fake Tunarr ────────────────

class _Tunarr:
    """Channel #1 in a fake Tunarr. A rename PUT can be refused (Tunarr keeps the old name)
    or time out after Tunarr applied it (the real `api` returns None for both)."""

    def __init__(self, name):
        self.name = name
        self.refuse_rename = False
        self.rename_times_out = False
        self.renames = []

    def channel(self):
        return {"id": "tid-1", "number": 1, "name": self.name}

    def api(self, fallback):
        def call(url, method, path, body=None, timeout=60):
            if method == "GET" and path == "/api/channels":
                return [self.channel()]
            if method == "GET" and path == "/api/channels/tid-1":
                return self.channel()
            if method == "PUT" and path == "/api/channels/tid-1":
                if body["name"] != self.name:
                    if self.refuse_rename:
                        return None
                    self.name = body["name"]
                    self.renames.append(body["name"])
                    if self.rename_times_out:
                        return None
                return body
            return fallback(url, method, path, body=body, timeout=timeout)
        return call


@pytest.fixture
def real(server, monkeypatch):  # noqa: F811
    """Apply runs the real update_channel_in_place; only Tunarr itself is faked."""
    def make(record, tunarr_has):
        tunarr = _Tunarr(tunarr_has)
        monkeypatch.setattr(channel_engine, "update_channel_in_place", REAL_UPDATE)
        monkeypatch.setattr(channel_engine, "find_channel_by_number", REAL_FIND)
        monkeypatch.setattr(channel_engine, "set_programming", lambda *a, **k: {"ok": True})
        monkeypatch.setattr(channel_engine, "api", tunarr.api(channel_engine.api))
        (server / "channels.json").write_text(json.dumps({"channels": [
            {"number": 1, "shuffle": "shuffle", "content": ["2 Guns"], **record}]}))
        return tunarr
    return make


def _save_name(server, name):  # noqa: F811
    channels_router.update_channel(1, {**_saved(server), "name": name})


def _apply_fails():
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.apply_channel(1))
    assert e.value.status_code == 409
    return str(e.value.detail)


def test_real_apply_renames_and_clears_the_pending_names(server, real):  # noqa: F811
    tunarr = real({"name": "Comedy TV"}, "Comedy TV")
    _save_name(server, "Laughs")
    asyncio.run(channels_router.apply_channel(1))
    assert tunarr.name == "Laughs"
    assert "tunarr_name" not in _saved(server)


def test_a_timed_out_rename_then_a_second_rename_still_applies(server, real):  # noqa: F811
    """The rename PUT timed out after Tunarr applied 'Laughs', so the record never heard
    back. A second rename must not leave the guard without 'Laughs'."""
    tunarr = real({"name": "Comedy TV"}, "Comedy TV")
    _save_name(server, "Laughs")
    tunarr.rename_times_out = True
    assert "new name" in _apply_fails()
    assert tunarr.name == "Laughs"                         # Tunarr did apply it
    assert _saved(server)["tunarr_name"] == ["Comedy TV"]  # but nothing confirmed it

    tunarr.rename_times_out = False
    _save_name(server, "Big Laughs")
    assert _saved(server)["tunarr_name"] == ["Comedy TV", "Laughs"]
    asyncio.run(channels_router.apply_channel(1))
    assert tunarr.name == "Big Laughs"
    assert _saved(server)["name"] == "Big Laughs" and "tunarr_name" not in _saved(server)


def test_renamed_in_tunarrs_own_ui_then_edited_still_applies(server, real):  # noqa: F811
    """Old string form on disk; the person typed the new name into Tunarr's UI too."""
    tunarr = real({"name": "Laughs", "tunarr_name": "Comedy TV"}, "Laughs")
    _save_name(server, "Big Laughs")
    asyncio.run(channels_router.apply_channel(1))
    assert tunarr.name == "Big Laughs"
    assert "tunarr_name" not in _saved(server)


def test_a_name_the_record_never_had_is_still_refused(server, real):  # noqa: F811
    tunarr = real({"name": "Laughs", "tunarr_name": ["Comedy TV"]}, "Westerns")
    _save_name(server, "Big Laughs")
    assert "mismatch" in _apply_fails()
    assert tunarr.name == "Westerns" and tunarr.renames == []
    assert _saved(server)["tunarr_name"] == ["Comedy TV", "Laughs"]


def test_many_failed_renames_never_lose_the_confirmed_name(server, real):  # noqa: F811
    """Tunarr has 'A'; seven renames B..H, each Apply refused. 'A' must stay in the guard,
    and the next Apply (once Tunarr accepts renames) must go through."""
    tunarr = real({"name": "A"}, "A")
    tunarr.refuse_rename = True
    for name in "BCDEFGH":
        _save_name(server, name)
        assert "new name" in _apply_fails()
        assert tunarr.name == "A"
        assert channel_engine.name_matches("A", channel_engine.guard_names(_saved(server)))
    assert len(_saved(server)["tunarr_name"]) > 5

    tunarr.refuse_rename = False
    asyncio.run(channels_router.apply_channel(1))
    assert tunarr.name == "H"
    assert "tunarr_name" not in _saved(server)


def test_a_refused_rename_keeps_the_pending_name(server, real):  # noqa: F811
    tunarr = real({"name": "Test"}, "Test")
    _save_name(server, "Renamed")
    tunarr.refuse_rename = True
    assert "new name" in _apply_fails()
    assert tunarr.name == "Test"
    assert _saved(server)["tunarr_name"] == ["Test"]
