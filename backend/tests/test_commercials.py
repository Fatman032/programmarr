"""test_commercials.py — a channel can use several filler lists, and Apply attaches them.

Two problems this pins down:
  1. The commercials setting held ONE filler list id, though Tunarr lets a channel pull its
     breaks from several lists. `filler_list_ids` now holds them all; a channels.json with
     only the old single `filler_list_id` keeps working.
  2. A filler list was attached to the Tunarr channel only when the channel was CREATED.
     Choosing one later in the editor was saved in channels.json but never sent to Tunarr,
     so the flex gaps opened after each show had nothing to play. Apply (and the live
     auto-update) now attach the lists, and leave Tunarr alone when it already matches.
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
import create
import scheduler
from routers import channels_router

RESOLVED = [{"type": "Movie", "programs": [{"id": "p1"}]}]


# ── reading the setting ─────────────────────────────────────────────────────────────

def test_old_single_list_setting_still_counts():
    assert channel_engine.commercial_list_ids({"filler_list_id": "a", "pad_minutes": 5}) == ["a"]


def test_several_lists_keep_their_order():
    assert channel_engine.commercial_list_ids({"filler_list_ids": ["b", "a", "c"]}) == ["b", "a", "c"]


def test_the_list_of_ids_wins_over_the_old_single_id():
    comm = {"filler_list_id": "old", "filler_list_ids": ["x", "y"]}
    assert channel_engine.commercial_list_ids(comm) == ["x", "y"]


def test_repeats_blanks_and_junk_are_dropped():
    comm = {"filler_list_ids": ["a", "", None, 7, "a", "b"]}
    assert channel_engine.commercial_list_ids(comm) == ["a", "b"]


@pytest.mark.parametrize("comm", [None, {}, {"pad_minutes": 30}, {"filler_list_ids": []},
                                  {"filler_list_id": ""}, {"filler_list_ids": "a"}])
def test_no_lists_means_commercials_are_off(comm):
    assert channel_engine.commercial_list_ids(comm) == []
    assert channel_engine.commercial_settings(comm) == (0, [])


def test_settings_give_the_gap_and_the_lists():
    assert channel_engine.commercial_settings({"filler_list_ids": ["a", "b"]}) == (5 * 60000, ["a", "b"])
    assert channel_engine.commercial_settings({"filler_list_id": "a", "pad_minutes": 30}) == (30 * 60000, ["a"])


# ── attaching the lists in Tunarr ───────────────────────────────────────────────────

@pytest.fixture
def tunarr(monkeypatch):
    """A stand-in Tunarr: one channel whose attached lists you can set, plus a call log."""
    rec = type("Rec", (), {})()
    rec.channel = {"id": "tid", "number": 3, "name": "Test", "icon": {"path": "x"},
                   "fillerCollections": [], "fillerRepeatCooldown": 30000}
    rec.calls = []
    rec.get_result = "channel"
    rec.put_result = {}

    def fake_api(tunarr_url, method, path, body=None, timeout=60):
        rec.calls.append((method, path, body))
        if method == "GET" and path == "/api/channels/tid":
            return rec.channel if rec.get_result == "channel" else None
        if method == "PUT" and path == "/api/channels/tid":
            return rec.put_result
        raise AssertionError(f"unexpected api call: {method} {path}")

    monkeypatch.setattr(channel_engine, "api", fake_api)
    return rec


def _puts(rec):
    return [c for c in rec.calls if c[0] == "PUT"]


def _attached(rec):
    (_m, _p, body), = _puts(rec)
    return body["fillerCollections"]


def test_lists_chosen_later_are_attached(tunarr):
    assert channel_engine.sync_channel_fillers("http://t", "tid", ["a", "b"]) is True
    assert [c["id"] for c in _attached(tunarr)] == ["a", "b"]
    assert all(c["weight"] == channel_engine.FILLER_WEIGHT for c in _attached(tunarr))  # an even mix


def test_the_rest_of_the_channel_is_sent_back_unchanged(tunarr):
    channel_engine.sync_channel_fillers("http://t", "tid", ["a"])
    (_m, _p, body), = _puts(tunarr)
    assert body["icon"] == {"path": "x"} and body["fillerRepeatCooldown"] == 30000
    assert body["name"] == "Test" and body["number"] == 3


def test_nothing_is_sent_when_tunarr_already_has_them(tunarr):
    tunarr.channel["fillerCollections"] = [{"id": "b", "weight": 100, "cooldownSeconds": 30},
                                           {"id": "a", "weight": 100, "cooldownSeconds": 30}]
    assert channel_engine.sync_channel_fillers("http://t", "tid", ["a", "b"]) is False
    assert _puts(tunarr) == []


def test_a_list_that_stays_keeps_its_tuned_weight(tunarr):
    tunarr.channel["fillerCollections"] = [{"id": "a", "weight": 300, "cooldownSeconds": 90}]
    channel_engine.sync_channel_fillers("http://t", "tid", ["a", "b"])
    kept, added = _attached(tunarr)
    assert kept == {"id": "a", "weight": 300, "cooldownSeconds": 90}
    assert added["id"] == "b" and added["weight"] == channel_engine.FILLER_WEIGHT


def test_a_list_taken_off_the_selection_is_detached(tunarr):
    tunarr.channel["fillerCollections"] = [{"id": "a", "weight": 100, "cooldownSeconds": 30},
                                           {"id": "b", "weight": 100, "cooldownSeconds": 30}]
    channel_engine.sync_channel_fillers("http://t", "tid", ["b"])
    assert [c["id"] for c in _attached(tunarr)] == ["b"]


def test_no_lists_means_hands_off(tunarr):
    """Commercials switched off never strips a list someone attached by hand in Tunarr."""
    tunarr.channel["fillerCollections"] = [{"id": "a", "weight": 100, "cooldownSeconds": 30}]
    assert channel_engine.sync_channel_fillers("http://t", "tid", []) is False
    assert tunarr.calls == []


def test_a_channel_tunarr_wont_show_is_an_error_not_a_silent_skip(tunarr):
    tunarr.get_result = "nothing"
    with pytest.raises(channel_engine.ChannelEngineError):
        channel_engine.sync_channel_fillers("http://t", "tid", ["a"])
    assert _puts(tunarr) == []


def test_a_change_tunarr_refuses_is_an_error(tunarr):
    tunarr.put_result = None
    with pytest.raises(channel_engine.ChannelEngineError):
        channel_engine.sync_channel_fillers("http://t", "tid", ["a"])


# ── Apply and the auto-update do it ─────────────────────────────────────────────────

@pytest.fixture
def in_place(monkeypatch, tunarr):
    """update_channel_in_place with its schedule step stubbed, on top of the stand-in Tunarr."""
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "set_programming", lambda url, cid, payload: {"ok": True})
    return tunarr


def test_updating_a_channel_attaches_its_lists(in_place):
    channel_engine.update_channel_in_place("http://t", 3, "ordered", RESOLVED, pad_ms=300000,
                                           expected_name="Test", filler_list_ids=["a", "b"])
    assert [c["id"] for c in _attached(in_place)] == ["a", "b"]


def test_updating_without_lists_never_touches_the_channel_record(in_place):
    channel_engine.update_channel_in_place("http://t", 3, "ordered", RESOLVED, expected_name="Test")
    assert in_place.calls == []


def test_a_failed_attach_says_the_schedule_was_still_updated(in_place):
    in_place.put_result = None
    with pytest.raises(channel_engine.ChannelEngineError) as e:
        channel_engine.update_channel_in_place("http://t", 3, "ordered", RESOLVED, pad_ms=300000,
                                               expected_name="Test", filler_list_ids=["a"])
    assert "schedule was updated" in str(e.value) and "commercial lists" in str(e.value)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    monkeypatch.setattr(channel_engine, "build_library_index", lambda url: ({}, {}))
    monkeypatch.setattr(channel_engine, "resolve_content", lambda *a, **k: (RESOLVED, []))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "read_channel_programming", lambda url, cid: set())
    seen = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda *a, **k: seen.append(k))
    return type("Srv", (), {"dir": tmp_path, "seen": seen})()


def _write_channel(srv, commercials, live=False):
    ch = {"number": 3, "name": "Test", "shuffle": "shuffle", "content": ["Anything"]}
    if commercials is not None:
        ch["commercials"] = commercials
    if live:
        ch["live"] = True
    (srv.dir / "channels.json").write_text(json.dumps({"channels": [ch]}))


def test_apply_passes_every_chosen_list_and_the_gap(server):
    _write_channel(server, {"filler_list_id": "a", "filler_list_ids": ["a", "b"], "pad_minutes": 30})
    assert asyncio.run(channels_router.apply_channel(3))["ok"] is True
    (kw,) = server.seen
    assert kw["filler_list_ids"] == ["a", "b"] and kw["pad_ms"] == 30 * 60000


def test_apply_still_understands_a_channel_saved_with_one_list(server):
    _write_channel(server, {"filler_list_id": "a", "pad_minutes": 5})
    asyncio.run(channels_router.apply_channel(3))
    (kw,) = server.seen
    assert kw["filler_list_ids"] == ["a"] and kw["pad_ms"] == 5 * 60000


def test_apply_without_commercials_sends_no_lists_and_no_gap(server):
    _write_channel(server, None)
    asyncio.run(channels_router.apply_channel(3))
    (kw,) = server.seen
    assert kw["filler_list_ids"] == [] and kw["pad_ms"] == 0


def test_the_auto_update_keeps_the_lists_attached_too(server):
    _write_channel(server, {"filler_list_ids": ["a", "b"], "pad_minutes": 5}, live=True)
    summary = scheduler._run_cycle_blocking(apply=True)
    assert summary["error"] is None
    (kw,) = server.seen
    assert kw["filler_list_ids"] == ["a", "b"]


# ── creating a channel ──────────────────────────────────────────────────────────────

def test_a_new_channel_is_created_with_all_its_lists(monkeypatch):
    sent = {}

    def fake_api(url, method, path, body=None, timeout=60):
        sent["body"] = body
        return {"id": "new"}

    monkeypatch.setattr(create, "api", fake_api)
    create.create_channel("http://t", 9, "New", "cfg", filler_list_ids=["a", "b"])
    assert [c["id"] for c in sent["body"]["channel"]["fillerCollections"]] == ["a", "b"]


def test_a_new_channel_without_commercials_has_no_lists(monkeypatch):
    sent = {}
    monkeypatch.setattr(create, "api", lambda url, method, path, body=None, timeout=60:
                        sent.update(body=body) or {"id": "new"})
    create.create_channel("http://t", 9, "New", "cfg")
    assert sent["body"]["channel"]["fillerCollections"] == []
