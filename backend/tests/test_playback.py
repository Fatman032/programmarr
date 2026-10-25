"""Playback structure — interleaved random-slot weighting + timeline manual lineups."""

import channel_engine


def _movie_item(title, pid, release_ms=None, duration=5400000):
    return {"type": "Movie", "title": title, "programs": [
        {"id": pid, "program": {"title": title, "releaseDate": release_ms,
                                "duration": duration, "year": 1990}}]}


def _show_item(title, show_id, episodes):
    """episodes: list of (pid, season, ep, release_ms)
    Uses real Tunarr field names: season={index: N} and episodeNumber.
    """
    return {"type": "TV", "title": title, "showId": show_id, "programs": [
        {"id": pid, "program": {"title": f"{title} s{s}e{e}", "duration": 1800000,
                                "season": {"index": s}, "episodeNumber": e,
                                "releaseDate": rms}}
        for pid, s, e, rms in episodes]}


def _slots(schedule):
    return schedule["schedule"]["slots"]


# ── interleaved ───────────────────────────────────────────────────────────────

def test_interleaved_weights_movies_vs_episode_blocks():
    items = [
        _movie_item("Movie A", "m1", 100),
        _movie_item("Movie B", "m2", 200),
        _show_item("Show X", "sx", [("e1", 1, 1, 50), ("e2", 1, 2, 60)]),
        _show_item("Show Y", "sy", [("e3", 1, 1, 70)]),
    ]
    sched = channel_engine.build_schedule(
        "ordered", items, playback={"structure": "interleaved", "episodes_per_block": 4})
    assert sched["type"] == "random"
    movie_slots = [s for s in _slots(sched) if s["type"] == "movie"]
    show_slots = [s for s in _slots(sched) if s["type"] == "show"]
    assert len(movie_slots) == 1 and len(show_slots) == 2
    assert movie_slots[0]["order"] == "chronological"
    assert movie_slots[0]["weight"] == 2          # = number of shows
    assert all(s["order"] == "next" for s in show_slots)
    assert all(s["weight"] == 4 for s in show_slots)  # = episodes_per_block


def test_interleaved_default_block_size_is_4():
    items = [_movie_item("Movie A", "m1", 100),
             _show_item("Show X", "sx", [("e1", 1, 1, 50)])]
    sched = channel_engine.build_schedule("ordered", items,
                                          playback={"structure": "interleaved"})
    show_slots = [s for s in _slots(sched) if s["type"] == "show"]
    assert show_slots[0]["weight"] == 4
    movie_slots = [s for s in _slots(sched) if s["type"] == "movie"]
    assert movie_slots[0]["weight"] == 1          # one show


def test_interleaved_movies_only_degrades_gracefully():
    items = [_movie_item("Movie A", "m1", 100)]
    sched = channel_engine.build_schedule("ordered", items,
                                          playback={"structure": "interleaved"})
    movie_slots = [s for s in _slots(sched) if s["type"] == "movie"]
    assert movie_slots[0]["order"] == "chronological"
    assert movie_slots[0]["weight"] == 1


def test_no_playback_is_byte_identical_to_today():
    items = [_movie_item("Movie A", "m1", 100),
             _show_item("Show X", "sx", [("e1", 1, 1, 50)])]
    a = channel_engine.build_schedule("ordered", items)
    b = channel_engine.build_schedule("ordered", items, playback=None)
    # uuids differ per call; compare everything except slot ids
    def strip(s):
        return {**s, "schedule": {**s["schedule"],
                "slots": [{k: v for k, v in sl.items() if k != "id"}
                          for sl in s["schedule"]["slots"]]}}
    assert strip(a) == strip(b)
    sl = strip(a)["schedule"]["slots"]
    assert all(x["weight"] == 1 for x in sl)      # today's weights untouched


# ── timeline ──────────────────────────────────────────────────────────────────

def test_timeline_builds_manual_lineup_in_premiere_order():
    items = [
        _movie_item("Late Movie", "m2", 900),
        _show_item("Mid Show", "sx", [("e2", 1, 2, 510), ("e1", 1, 1, 500)]),
        _movie_item("Early Movie", "m1", 100),
    ]
    sched = channel_engine.build_schedule("ordered", items,
                                          playback={"structure": "timeline"})
    assert sched["type"] == "manual"
    assert sched["append"] is False
    ids = [li["id"] for li in sched["lineup"]]
    # Early Movie (100) → Mid Show premiere (500; episodes in s/e order) → Late Movie (900)
    assert ids == ["m1", "e1", "e2", "m2"]
    assert all(li["type"] == "content" for li in sched["lineup"])
    assert all(isinstance(li["duration"], int) and li["duration"] > 0
               for li in sched["lineup"])


def test_timeline_show_without_releasedate_sorts_by_year_then_end():
    items = [
        _movie_item("Movie 1990", "m1", 700000000000),  # ~1992 in ms — fine, just ordered
        {"type": "TV", "title": "Undated Show", "showId": "sz", "programs": [
            {"id": "z1", "program": {"title": "Undated s1e1", "duration": 1800000,
                                     "season": {"index": 1}, "episodeNumber": 1,
                                     "releaseDate": None, "year": None}}]},
    ]
    sched = channel_engine.build_schedule("ordered", items,
                                          playback={"structure": "timeline"})
    ids = [li["id"] for li in sched["lineup"]]
    assert ids == ["m1", "z1"]  # undated content sorts to the end, never crashes


def test_timeline_empty_items_returns_none():
    assert channel_engine.build_schedule(
        "ordered", [], playback={"structure": "timeline"}) is None


# ── plumbing ──────────────────────────────────────────────────────────────────

def test_update_channel_in_place_passes_playback(monkeypatch):
    captured = {}
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid", "name": "X"})
    monkeypatch.setattr(channel_engine, "set_programming",
                        lambda url, cid, payload: captured.update(payload=payload) or {})
    items = [_movie_item("Movie A", "m1", 100),
             _show_item("Show X", "sx", [("e1", 1, 1, 50)])]
    channel_engine.update_channel_in_place(
        "http://t", 5, "ordered", items,
        playback={"structure": "timeline"}, expected_name="X")
    assert captured["payload"]["type"] == "manual"


# ── list order ────────────────────────────────────────────────────────────────

def _ids(sched):
    return [li["id"] for li in sched["lineup"]]


def test_list_order_plays_items_in_the_order_listed():
    # The Toon Legends shape: a show, its movie, the next show, its movie.
    items = [
        _show_item("Simpsons", "s", [("s2", 1, 2, 20), ("s1", 1, 1, 10), ("s3", 2, 1, 30)]),
        _movie_item("Simpsons Movie", "sm", 25),
        _show_item("Bobs", "b", [("b1", 1, 1, 5)]),
        _movie_item("Bobs Movie", "bm", 1),
    ]
    sched = channel_engine.build_schedule("shuffle", items, playback={"structure": "list"})
    assert sched["type"] == "manual" and sched["append"] is False
    # Listed order wins over dates; each show's run is in season/episode order.
    assert _ids(sched) == ["s1", "s2", "s3", "sm", "b1", "bm"]
    assert all(li["duration"] > 0 for li in sched["lineup"])


def test_list_order_puts_specials_after_the_run_and_plays_a_repeat_once():
    show = _show_item("Show", "x", [("sp", 0, 1, 99), ("e1", 1, 1, 10), ("e2", 1, 2, 20)])
    items = [show, _movie_item("Movie", "m", 5), show]      # listed twice (e.g. by hand + rule)
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "list"})
    assert _ids(sched) == ["e1", "e2", "sp", "m"]


def test_list_order_empty_returns_none():
    assert channel_engine.build_schedule("ordered", [], playback={"structure": "list"}) is None


# ── air-date order ────────────────────────────────────────────────────────────

def test_airdate_puts_the_movie_between_the_episodes_around_its_release():
    # Simpsons S1E1 1989 … S18 ends May 2007; the movie July 2007; S19 from Sep 2007.
    items = [
        _show_item("Simpsons", "s", [("s1", 1, 1, 1989), ("s18", 18, 22, 2007_05),
                                     ("s19", 19, 1, 2007_09)]),
        _movie_item("Simpsons Movie", "sm", 2007_07),
    ]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert sched["type"] == "manual"
    assert _ids(sched) == ["s1", "s18", "sm", "s19"]


def test_airdate_mixes_shows_by_date():
    items = [
        _show_item("A", "a", [("a1", 1, 1, 10), ("a2", 1, 2, 30)]),
        _show_item("B", "b", [("b1", 1, 1, 20), ("b2", 1, 2, 40)]),
    ]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["a1", "b1", "a2", "b2"]


def test_airdate_undated_episode_follows_the_one_before_it():
    # e2 has no date (Plex never matched it): it plays right after e1, before the other show's 15.
    items = [
        _show_item("A", "a", [("e1", 1, 1, 10), ("e2", 1, 2, None), ("e3", 1, 3, 30)]),
        _show_item("B", "b", [("b1", 1, 1, 15)]),
    ]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["e1", "e2", "b1", "e3"]


def test_airdate_leading_undated_episodes_sit_just_before_the_first_dated_one():
    items = [
        _show_item("B", "b", [("b1", 1, 1, 5)]),
        _show_item("A", "a", [("e1", 1, 1, None), ("e2", 1, 2, 10)]),
    ]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["b1", "e1", "e2"]


def test_airdate_never_reorders_a_shows_episodes():
    # e2's date is earlier than e1's (bad metadata): episode order still wins.
    items = [_show_item("A", "a", [("e1", 1, 1, 50), ("e2", 1, 2, 40), ("e3", 1, 3, 60)]),
             _movie_item("M", "m", 55)]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    ids = _ids(sched)
    assert ids.index("e1") < ids.index("e2") < ids.index("e3")
    assert ids == ["e1", "e2", "m", "e3"]


def test_airdate_specials_go_by_their_own_date_and_never_drag_the_show():
    # A season-0 special dated 2010 must not push the 1989 run to 2010.
    items = [
        _show_item("A", "a", [("sp", 0, 1, 2010), ("e1", 1, 1, 1989), ("e2", 1, 2, 2020)]),
        _movie_item("M", "m", 2000),
    ]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["e1", "m", "sp", "e2"]


def test_airdate_undated_movie_uses_its_year_then_the_end():
    from datetime import datetime, timezone
    y1995 = datetime(1995, 6, 1, tzinfo=timezone.utc).timestamp() * 1000
    y2000 = datetime(2000, 6, 1, tzinfo=timezone.utc).timestamp() * 1000
    undated = {"type": "Movie", "title": "Undated", "programs": [
        {"id": "u", "program": {"title": "Undated", "releaseDate": None, "duration": 100, "year": None}}]}
    items = [undated,
             _movie_item("Year Only", "y", None),                 # year 1990 → Jan 1 1990
             _show_item("A", "a", [("e1", 1, 1, y1995), ("e2", 1, 2, y2000)])]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["y", "e1", "e2", "u"]


def test_airdate_same_day_keeps_list_then_episode_order():
    items = [_movie_item("M", "m", 10),
             _show_item("A", "a", [("e2", 1, 2, 10), ("e1", 1, 1, 10)])]
    sched = channel_engine.build_schedule("ordered", items, playback={"structure": "airdate"})
    assert _ids(sched) == ["m", "e1", "e2"]


def test_update_channel_in_place_passes_list_and_airdate(monkeypatch):
    captured = []
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid", "name": "X"})
    monkeypatch.setattr(channel_engine, "set_programming",
                        lambda url, cid, payload: captured.append(payload) or {})
    items = [_movie_item("Movie A", "m1", 100),
             _show_item("Show X", "sx", [("e1", 1, 1, 50)])]
    for structure in ("list", "airdate"):
        channel_engine.update_channel_in_place(
            "http://t", 5, "ordered", items, playback={"structure": structure}, expected_name="X")
    assert [p["type"] for p in captured] == ["manual", "manual"]
    assert [li["id"] for li in captured[0]["lineup"]] == ["m1", "e1"]   # list order
    assert [li["id"] for li in captured[1]["lineup"]] == ["e1", "m1"]   # air date
