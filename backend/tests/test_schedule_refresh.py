"""test_schedule_refresh.py — a live channel is rebuilt when it needs to be, not on every check.

The problem: Tunarr plays a channel as ONE fixed list of about 31 days, so a big channel's list
holds only the subset of its titles that fit (a 605-movie channel holds ~365). The auto-update
decided "did anything change?" by comparing everything the channel resolves to against what
Tunarr's channel holds — never equal for a big channel — so every check "added" ~240 movies
and rebuilt it with a new random pick, restarting what was playing. At a 6-hour interval that
is four rebuilds a day.

Now it compares against what it applied LAST time, and also rebuilds a channel near the end of
its list when some of its titles didn't fit (so they get a turn). A small channel whose titles
all fit is left alone: it simply loops.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
import scheduler
from routers import channels_router

DAY = 24 * 60 * 60 * 1000
NOW = 1_800_000_000_000
BIG = {f"p{i}" for i in range(100)}          # everything the channel resolves to
FITTED = {f"p{i}" for i in range(60)}        # the part of it Tunarr's 31-day list holds
SMALL = {f"p{i}" for i in range(10)}


def _tch(days_left, name="Test"):
    """The Tunarr channel summary: its list runs for 31 days from startTime."""
    duration = 31 * DAY
    return {"id": "tid", "number": 5, "name": name,
            "startTime": NOW - (duration - int(days_left * DAY)), "duration": duration}


# ── the decision itself ─────────────────────────────────────────────────────────────

def test_an_unchanged_big_channel_is_left_alone():
    """The bug: fresh (100) never equals what Tunarr holds (60), yet nothing changed."""
    assert scheduler._needs_update(BIG, FITTED, BIG, _tch(20), NOW) == (False, "")


def test_new_content_is_a_change():
    assert scheduler._needs_update(BIG | {"new"}, FITTED, BIG, _tch(20), NOW) == (True, "content changed")


def test_removed_content_is_a_change():
    assert scheduler._needs_update(BIG - {"p1"}, FITTED, BIG, _tch(20), NOW) == (True, "content changed")


def test_without_a_baseline_it_compares_with_what_tunarr_holds_as_before():
    assert scheduler._needs_update(SMALL, SMALL, None, _tch(20), NOW) == (False, "")
    assert scheduler._needs_update(BIG, FITTED, None, _tch(20), NOW) == (True, "content changed")


def test_a_big_channel_near_the_end_of_its_list_gets_a_fresh_pick():
    needs, why = scheduler._needs_update(BIG, FITTED, BIG, _tch(2), NOW)
    assert needs and "nearly used up" in why


def test_a_big_channel_with_plenty_left_is_not_rebuilt():
    assert scheduler._needs_update(BIG, FITTED, BIG, _tch(4), NOW)[0] is False


def test_a_small_channel_is_never_rebuilt_just_for_running_out():
    """Everything already fits, so there is nothing to rotate in — it loops."""
    assert scheduler._needs_update(SMALL, SMALL, SMALL, _tch(1), NOW) == (False, "")
    assert scheduler._needs_update(SMALL, SMALL, SMALL, _tch(-5), NOW) == (False, "")  # already looping


def test_a_big_channel_already_past_its_end_is_rebuilt():
    assert scheduler._needs_update(BIG, FITTED, BIG, _tch(-3), NOW)[0] is True


def test_a_channel_that_does_not_say_when_it_started_is_not_rebuilt_for_that():
    assert scheduler._needs_update(BIG, FITTED, BIG, {"id": "tid"}, NOW)[0] is False


def test_an_empty_channel_in_tunarr_is_refilled():
    assert scheduler._needs_update(BIG, set(), BIG, _tch(20), NOW) == (True, "the channel is empty in Tunarr")
    assert scheduler._needs_update(set(), set(), None, _tch(20), NOW) == (False, "")


# ── the whole cycle ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cycle(tmp_path, monkeypatch):
    """A live channel (#5 "Test") and a fake Tunarr. Set .fresh (what it resolves to), .cur (what
    Tunarr's channel holds) and .days_left; read .pushed (what was re-posted)."""
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(scheduler, "_now_ms", lambda: NOW)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 5, "name": "Test", "shuffle": "shuffle", "live": True, "content": ["x"]}]}))
    s = type("Cycle", (), {})()
    s.dir, s.fresh, s.cur, s.days_left, s.pushed = tmp_path, set(BIG), set(FITTED), 20, []

    def resolved(*a, **k):
        return [{"type": "Movie", "title": "M", "programs": [{"id": i} for i in sorted(s.fresh)]}], []

    monkeypatch.setattr(channel_engine, "build_library_index_with_ids", lambda url: ({}, {}, {}))
    monkeypatch.setattr(channel_engine, "resolve_content", resolved)
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: _tch(s.days_left))
    monkeypatch.setattr(channel_engine, "read_channel_programming", lambda url, cid: set(s.cur))
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda url, number, shuffle, res, **kw: s.pushed.append(kw))
    s.run = lambda apply=True: scheduler._run_cycle_blocking(apply=apply)
    s.applied = lambda: json.loads((tmp_path / "recipe_applied.json").read_text()) \
        if (tmp_path / "recipe_applied.json").exists() else {}
    return s


def test_a_big_channel_is_rebuilt_once_not_on_every_check(cycle):
    """First check after this change: no baseline yet, so it compares the old way (a rebuild),
    then remembers what it applied. Every later check leaves it alone."""
    cycle.run()
    assert len(cycle.pushed) == 1 and cycle.applied()["5"] == sorted(BIG)
    cycle.run()
    cycle.run()
    assert len(cycle.pushed) == 1


def test_a_rebuild_starts_the_new_schedule_from_now(cycle):
    cycle.run()
    assert cycle.pushed[0]["restart"] is True


def test_new_titles_still_rebuild_it(cycle):
    cycle.run()
    cycle.fresh = BIG | {"p-new"}
    summary = cycle.run()
    assert len(cycle.pushed) == 2 and summary["changes"][0]["added_count"] == 1
    assert "p-new" in cycle.applied()["5"]


def test_the_summary_counts_against_what_was_applied_not_against_tunarrs_smaller_list(cycle):
    cycle.run()
    cycle.fresh = BIG | {"p-new"}
    (change,) = cycle.run()["changes"]
    assert change["added_count"] == 1 and change["removed_count"] == 0  # not +41


def test_a_channel_already_in_step_is_remembered_not_rebuilt(cycle):
    cycle.fresh = cycle.cur = set(SMALL)
    cycle.run()
    assert cycle.pushed == [] and cycle.applied()["5"] == sorted(SMALL)


def test_a_big_channel_near_its_end_gets_a_fresh_pick_and_says_why(cycle):
    cycle.run()                                  # baseline
    cycle.days_left = 2
    summary = cycle.run()
    assert len(cycle.pushed) == 2
    (change,) = summary["changes"]
    assert "nearly used up" in change["note"] and change["added_count"] == change["removed_count"] == 0
    assert "nearly used up" in (cycle.dir / "logs" / "recipes.log").read_text()
    assert "nearly used up" in json.loads((cycle.dir / "recipe_state.json").read_text())["5"]["change_summary"]


def test_a_small_channel_is_left_to_loop(cycle):
    cycle.fresh = cycle.cur = set(SMALL)
    cycle.run()
    cycle.days_left = -4
    cycle.run()
    assert cycle.pushed == []


def test_an_emptied_channel_is_refilled(cycle):
    cycle.run()
    cycle.cur = set()
    cycle.run()
    assert len(cycle.pushed) == 2


def test_a_dry_run_changes_and_remembers_nothing(cycle):
    summary = cycle.run(apply=False)
    assert summary["changed"] == 1 and summary["changes"][0]["applied"] is False
    assert cycle.pushed == [] and cycle.applied() == {}


def test_a_failed_rebuild_is_not_remembered_as_applied(cycle, monkeypatch):
    def refuse(*a, **k):
        raise channel_engine.ChannelEngineError("Tunarr said no")
    monkeypatch.setattr(channel_engine, "update_channel_in_place", refuse)
    summary = cycle.run()
    assert summary["skipped"][0]["reason"] == "Tunarr said no" and cycle.applied() == {}


# ── remembering what was applied ────────────────────────────────────────────────────

def test_recording_one_channel_keeps_the_others(tmp_path):
    scheduler.record_applied(tmp_path, 5, {"b", "a"})
    scheduler.record_applied(tmp_path, 6, {"c"})
    assert scheduler._load_applied(tmp_path) == {"5": ["a", "b"], "6": ["c"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_broken_baseline_file_is_ignored_not_fatal(tmp_path):
    (tmp_path / scheduler.APPLIED_FILE).write_text("{not json")
    assert scheduler._load_applied(tmp_path) == {}
    scheduler.record_applied(tmp_path, 5, {"a"})
    assert scheduler._load_applied(tmp_path) == {"5": ["a"]}


def test_an_unwritable_folder_never_raises(tmp_path):
    scheduler.record_applied(tmp_path / "no" / "such" / "folder", 5, {"a"})


# ── restarting the schedule in Tunarr ───────────────────────────────────────────────

@pytest.fixture
def tunarr(monkeypatch):
    rec = type("Rec", (), {})()
    rec.channel = {"id": "tid", "number": 5, "name": "Test", "startTime": 1000, "duration": 31 * DAY,
                   "icon": {"path": "x"}, "fillerCollections": []}
    rec.calls, rec.get_ok, rec.put_ok = [], True, True

    def fake_api(url, method, path, body=None, timeout=60):
        rec.calls.append((method, path, body))
        if method == "GET":
            return dict(rec.channel) if rec.get_ok else None
        return {} if rec.put_ok else None
    monkeypatch.setattr(channel_engine, "api", fake_api)
    return rec


def test_restart_moves_only_the_start_time(tunarr):
    assert channel_engine.restart_channel_schedule("http://t", "tid", now_ms=NOW) == NOW
    (_m, path, body), = [c for c in tunarr.calls if c[0] == "PUT"]
    assert path == "/api/channels/tid" and body["startTime"] == NOW
    assert body["duration"] == 31 * DAY and body["icon"] == {"path": "x"} and body["name"] == "Test"


def test_restart_errors_are_plain(tunarr):
    tunarr.get_ok = False
    with pytest.raises(channel_engine.ChannelEngineError):
        channel_engine.restart_channel_schedule("http://t", "tid")
    tunarr.get_ok, tunarr.put_ok = True, False
    with pytest.raises(channel_engine.ChannelEngineError):
        channel_engine.restart_channel_schedule("http://t", "tid")


@pytest.fixture
def in_place(monkeypatch, tunarr):
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "tid", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "set_programming", lambda url, cid, payload: tunarr.calls.append(("POST", "programming", None)) or {"ok": True})
    return tunarr


RESOLVED = [{"type": "Movie", "programs": [{"id": "p1"}]}]


def test_updating_with_restart_posts_the_programming_then_resets_the_start(in_place):
    channel_engine.update_channel_in_place("http://t", 5, "shuffle", RESOLVED, expected_name="Test", restart=True)
    kinds = [c[0] for c in in_place.calls]
    assert kinds.index("POST") < kinds.index("PUT")


def test_updating_without_restart_never_touches_the_start_time(in_place):
    channel_engine.update_channel_in_place("http://t", 5, "shuffle", RESOLVED, expected_name="Test")
    assert [c[0] for c in in_place.calls] == ["POST"]


def test_a_failed_restart_says_the_schedule_was_still_updated(in_place):
    in_place.put_ok = False
    with pytest.raises(channel_engine.ChannelEngineError) as e:
        channel_engine.update_channel_in_place("http://t", 5, "shuffle", RESOLVED, expected_name="Test", restart=True)
    assert "schedule was updated" in str(e.value) and "start time" in str(e.value)


# ── manual Apply ────────────────────────────────────────────────────────────────────

def test_a_manual_apply_restarts_the_schedule_and_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [
        {"number": 5, "name": "Test", "shuffle": "shuffle", "content": ["x"]}]}))
    items = [{"type": "Movie", "title": "M", "programs": [{"id": "p1"}, {"id": "p2"}]}]
    pushed = []
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids", lambda url: ({}, {}, {}))
    monkeypatch.setattr(channel_engine, "resolve_content", lambda *a, **k: (items, []))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda d: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number", lambda url, n: {"id": "tid", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "update_channel_in_place", lambda *a, **k: pushed.append(k))
    asyncio.run(channels_router.apply_channel(5))
    assert pushed[0]["restart"] is True
    assert scheduler._load_applied(tmp_path) == {"5": ["p1", "p2"]}
