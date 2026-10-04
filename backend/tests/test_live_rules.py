"""test_live_rules.py — {"rule": "genre"|"studio"|"director"|"actor"|"decade"} entries.

The problem (upstream issue #41): picking a genre/studio/director/actor/decade in the
Planner froze today's matches into a plain list of titles and forgot the rule, so a channel
marked Live never gained a movie added to Plex later. A rule entry is saved with the channel
and asked of Plex again on every refresh; each match is then found in Tunarr by its Plex id.

What must hold:
  - the rule finds exactly what Plex says matches (and nothing else);
  - something Plex has but Tunarr hasn't scanned in yet is REPORTED, not dropped for good;
  - if Plex can't be read the rule is "not checked" — and the channel is left alone, because
    patching it then would drop everything the rule brought.
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
from routers import channels_router
from test_id_index import _episode, _index, _library, _movie

SECTIONS = [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"}]


def _pm(title, year, rk, genres=(), studio="", directors=(), actors=()):
    """One movie as Plex lists it."""
    return {"title": title, "year": year, "ratingKey": str(rk), "studio": studio,
            "Genre": [{"tag": g} for g in genres], "Director": [{"tag": d} for d in directors],
            "Role": [{"tag": a} for a in actors]}


PLEX_MOVIES = [
    _pm("Shrek", 2001, 101, genres=["Animation", "Comedy"], studio="DreamWorks Animation", actors=["Mike Myers"]),
    _pm("Toy Story", 1995, 102, genres=["Animation", "Family"], studio="Pixar", directors=["John Lasseter"],
        actors=["Tom Hanks", "Tim Allen", "Don Rickles", "Jim Varney"]),
    _pm("Die Hard", 1988, 103, genres=["Action"], studio="20th Century Studios", actors=["Bruce Willis"]),
    _pm("Big", 1988, 104, genres=["Comedy", "Fantasy"], studio="20th Century Studios", actors=["Tom Hanks"]),
    _pm("Wonder Woman", 2017, 105, genres=["Action"], studio="Warner Bros."),
]
PLEX_SHOWS = [
    {"title": "Wonder Woman", "year": 1975, "ratingKey": "300", "studio": "ABC",
     "Genre": [{"tag": "Action"}]},
    {"title": "Cheers", "year": 1982, "ratingKey": "301", "studio": "NBC", "Genre": [{"tag": "Comedy"}]},
]


@pytest.fixture
def plex(monkeypatch):
    """Fake Plex with one movie library and one show library. `plex.calls` counts requests."""
    state = type("Plex", (), {})()
    state.calls = []
    state.movies = list(PLEX_MOVIES)
    state.shows = list(PLEX_SHOWS)
    state.down = False

    def fake_plex_get(base_url, token, path, timeout=60):
        state.calls.append(path)
        if state.down:
            return None
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": SECTIONS}}
        if path == "/library/sections/1/all?type=1":
            return {"MediaContainer": {"Metadata": state.movies}}
        if path == "/library/sections/2/all?type=2":
            return {"MediaContainer": {"Metadata": state.shows}}
        return None

    monkeypatch.setattr(channel_engine, "plex_get", fake_plex_get)
    return state


@pytest.fixture
def lib(monkeypatch):
    """Tunarr already holds everything Plex has, with the same Plex ids."""
    return _index(
        monkeypatch, [("mv", "movies"), ("tv", "shows")],
        {"mv": [_movie("Shrek", 2001, tmdb="808", plex="101", tunarr="m-shrek"),
                _movie("Toy Story", 1995, tmdb="862", plex="102", tunarr="m-toy"),
                _movie("Die Hard", 1988, tmdb="562", plex="103", tunarr="m-dh"),
                _movie("Big", 1988, tmdb="2280", plex="104", tunarr="m-big"),
                _movie("Wonder Woman", 2017, tmdb="297762", plex="105", tunarr="m-ww")],
         "tv": [_episode("Wonder Woman", "ww-show", 1975, tmdb="1924", plex="300", n=i) for i in range(1, 4)]
               + [_episode("Cheers", "cheers", 1982, tmdb="4471", plex="301", n=1)]})


def _resolve(content, lib, report=None, cache=None):
    movie_map, show_map, idx = lib
    return channel_engine.resolve_content(
        content, movie_map, show_map, plex_url="http://plex", plex_token="t",
        collection_cache={} if cache is None else cache, id_index=idx, report=report)


def _titles(resolved):
    return sorted(r["title"] for r in resolved)


# ── each kind of rule ───────────────────────────────────────────────────────────────

def test_a_genre_rule_finds_the_movies_plex_says_have_that_genre(plex, lib):
    resolved, missing = _resolve([{"rule": "genre", "value": "Animation"}], lib)
    assert _titles(resolved) == ["Shrek", "Toy Story"] and not missing


def test_rule_values_ignore_case_and_spacing(plex, lib):
    resolved, _ = _resolve([{"rule": "genre", "value": "  animation "}], lib)
    assert _titles(resolved) == ["Shrek", "Toy Story"]


def test_a_studio_rule(plex, lib):
    resolved, _ = _resolve([{"rule": "studio", "value": "20th Century Studios"}], lib)
    assert _titles(resolved) == ["Big", "Die Hard"]


def test_a_director_rule(plex, lib):
    resolved, _ = _resolve([{"rule": "director", "value": "John Lasseter"}], lib)
    assert _titles(resolved) == ["Toy Story"]


def test_an_actor_rule_counts_only_the_top_billed_cast(plex, lib):
    """Same cut as the Planner's export, so a rule finds what the Planner would have."""
    assert _titles(_resolve([{"rule": "actor", "value": "Tom Hanks"}], lib)[0]) == ["Big", "Toy Story"]
    assert _resolve([{"rule": "actor", "value": "Jim Varney"}], lib)[0] == []  # 4th billed


@pytest.mark.parametrize("value", ["1980", "1980s", "1988", "1989"])
def test_a_decade_rule_covers_every_year_of_that_decade(plex, lib, value):
    resolved, _ = _resolve([{"rule": "decade", "value": value}], lib)
    assert _titles(resolved) == ["Big", "Die Hard"]


def test_a_rule_for_shows_reads_the_show_libraries(plex, lib):
    resolved, _ = _resolve([{"rule": "genre", "value": "Comedy", "kind": "show"}], lib)
    assert [(r["title"], r["type"]) for r in resolved] == [("Cheers", "TV")]


def test_a_movie_rule_never_returns_the_show_with_the_same_title(plex, lib):
    resolved, _ = _resolve([{"rule": "genre", "value": "Action"}], lib)
    assert sorted((r["title"], r["type"]) for r in resolved) == [("Die Hard", "Movie"), ("Wonder Woman", "Movie")]


# ── what is not there yet ───────────────────────────────────────────────────────────

def test_a_match_tunarr_has_not_scanned_yet_is_reported_not_lost(plex, lib):
    plex.movies.append(_pm("Moana 2", 2024, 106, genres=["Animation"]))
    report = {}
    resolved, missing = _resolve([{"rule": "genre", "value": "Animation"}], lib, report)
    assert _titles(resolved) == ["Shrek", "Toy Story"]
    (m,) = report["missing"]
    assert m["label"] == "[genre:Animation]" and "1 in Plex but not in Tunarr yet" in m["why"] and "Moana 2" in m["why"]
    assert "blocked" not in report and missing == ["[genre:Animation]"]


def test_it_joins_on_its_own_once_tunarr_has_it(plex, monkeypatch):
    plex.movies.append(_pm("Moana 2", 2024, 106, genres=["Animation"]))
    before = _index(monkeypatch, [("mv", "movies")],
                    {"mv": [_movie("Shrek", 2001, tmdb="808", plex="101", tunarr="m-shrek")]})
    assert _titles(_resolve([{"rule": "genre", "value": "Animation"}], before)[0]) == ["Shrek"]
    after = _index(monkeypatch, [("mv", "movies")],
                   {"mv": [_movie("Shrek", 2001, tmdb="808", plex="101", tunarr="m-shrek"),
                           _movie("Moana 2", 2024, tmdb="1241982", plex="106", tunarr="m-moana2")]})
    assert _titles(_resolve([{"rule": "genre", "value": "Animation"}], after)[0]) == ["Moana 2", "Shrek"]


def test_a_rule_that_matches_nothing_says_so(plex, lib):
    report = {}
    resolved, _ = _resolve([{"rule": "studio", "value": "No Such Studio"}], lib, report)
    assert resolved == []
    assert report["missing"] == [{"label": "[studio:No Such Studio]", "why": "matched nothing in Plex"}]
    assert "blocked" not in report


# ── Plex can't answer ───────────────────────────────────────────────────────────────

def test_when_plex_cannot_be_read_the_rule_is_blocked_not_treated_as_empty(plex, lib):
    plex.down = True
    report = {}
    resolved, _ = _resolve(["Big", {"rule": "genre", "value": "Animation"}], lib, report)
    assert _titles(resolved) == ["Big"]  # the hand-picked title is unaffected
    (reason,) = report["blocked"]
    assert "[genre:Animation]" in reason and "Plex could not be read" in reason


def test_without_a_plex_connection_the_rule_is_blocked(lib):
    movie_map, show_map, idx = lib
    report = {}
    channel_engine.resolve_content([{"rule": "genre", "value": "Animation"}], movie_map, show_map,
                                   id_index=idx, report=report)
    assert report["blocked"]


# ── mixing and details ──────────────────────────────────────────────────────────────

def test_a_title_listed_by_hand_and_found_by_the_rule_counts_once(plex, lib):
    resolved, _ = _resolve(["Shrek", {"rule": "genre", "value": "Animation"}], lib)
    assert _titles(resolved) == ["Shrek", "Toy Story"]


def test_two_rules_share_one_fetch_of_the_library(plex, lib):
    cache = {}
    _resolve([{"rule": "genre", "value": "Animation"}, {"rule": "studio", "value": "Pixar"}], lib, cache=cache)
    assert plex.calls.count("/library/sections/1/all?type=1") == 1


def test_a_rule_next_to_other_kinds_of_entries(plex, lib):
    resolved, missing = _resolve(["Big", {"movie": "Die Hard"}, {"rule": "studio", "value": "Pixar"}], lib)
    assert _titles(resolved) == ["Big", "Die Hard", "Toy Story"] and not missing


@pytest.mark.parametrize("entry", [{"rule": "mood", "value": "Dark"}, {"rule": "genre", "value": ""},
                                   {"rule": "genre"}])
def test_an_unusable_rule_is_reported_and_never_blocks(plex, lib, entry):
    report = {}
    resolved, _ = _resolve(["Big", entry], lib, report)
    assert _titles(resolved) == ["Big"]
    assert report["missing"][0]["why"] == "unsupported rule" and "blocked" not in report


# ── the places that deploy a channel ────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch, plex, lib):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    (tmp_path / "config.json").write_text(json.dumps(
        {"tunarr_url": "http://t", "plex_url": "http://plex", "plex_token": "t"}))
    monkeypatch.setattr(channel_engine, "load_franchise_index", lambda data_dir: {})
    monkeypatch.setattr(channel_engine, "find_channel_by_number",
                        lambda url, n: {"id": "tid", "number": n, "name": "Test"})
    monkeypatch.setattr(channel_engine, "read_channel_programming", lambda url, cid: set())
    pushed = []
    monkeypatch.setattr(channel_engine, "update_channel_in_place",
                        lambda tunarr_url, number, shuffle, resolved, **kw: pushed.append(_titles(resolved)))
    return type("Srv", (), {"dir": tmp_path, "pushed": pushed})()


def _write(srv, content, live=True):
    ch = {"number": 7, "name": "Test", "shuffle": "shuffle", "content": content}
    if live:
        ch["live"] = True
    (srv.dir / "channels.json").write_text(json.dumps({"channels": [ch]}))


def test_apply_deploys_what_the_rule_finds(server):
    _write(server, [{"rule": "genre", "value": "Animation"}])
    assert asyncio.run(channels_router.apply_channel(7))["program_count"] == 2
    assert server.pushed == [["Shrek", "Toy Story"]]


def test_apply_leaves_the_channel_alone_when_plex_is_down(server, plex):
    plex.down = True
    _write(server, ["Big", {"rule": "genre", "value": "Animation"}])
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.apply_channel(7))
    assert e.value.status_code == 409 and "left as it is" in e.value.detail
    assert server.pushed == []


def test_the_auto_update_picks_up_a_new_match(server, plex, monkeypatch):
    plex.movies.append(_pm("Shrek 2", 2004, 107, genres=["Animation"]))
    _library(monkeypatch, [("mv", "movies")],  # Tunarr has scanned the new movie in
             {"mv": [_movie("Shrek", 2001, tmdb="808", plex="101", tunarr="m-shrek"),
                     _movie("Toy Story", 1995, tmdb="862", plex="102", tunarr="m-toy"),
                     _movie("Shrek 2", 2004, tmdb="809", plex="107", tunarr="m-shrek2")]})
    _write(server, [{"rule": "genre", "value": "Animation"}])
    summary = scheduler._run_cycle_blocking(apply=True)
    assert summary["error"] is None and summary["changed"] == 1
    assert server.pushed == [["Shrek", "Shrek 2", "Toy Story"]]


def test_the_auto_update_skips_a_channel_whose_rule_plex_could_not_answer(server, plex):
    plex.down = True
    _write(server, ["Big", {"rule": "genre", "value": "Animation"}])
    summary = scheduler._run_cycle_blocking(apply=True)
    assert server.pushed == []
    (skipped,) = summary["skipped"]
    assert skipped["number"] == 7 and "left as it is" in skipped["reason"]


# ── the pick-lists: what each rule can be set to ────────────────────────────────────

def _values(rule, kind="movie", cache=None):
    return channel_engine.rule_values(rule, kind, "http://plex", "t", [], {} if cache is None else cache)


def test_genre_choices_come_with_counts_most_used_first(plex):
    values = _values("genre")
    got = {v["value"]: v["count"] for v in values}
    assert got == {"Animation": 2, "Comedy": 2, "Family": 1, "Action": 2, "Fantasy": 1}
    assert [v["count"] for v in values] == sorted((v["count"] for v in values), reverse=True)


def test_choices_keep_plexs_spelling_not_a_lowercased_copy(plex):
    assert "Animation" in {v["value"] for v in _values("genre")}
    assert "20th Century Studios" in {v["value"] for v in _values("studio")}


def test_every_choice_actually_finds_something(plex, lib):
    """A pick-list that offered a value no item has would just recreate the typo problem."""
    for rule in ("genre", "studio", "director", "actor", "decade"):
        for v in _values(rule):
            resolved, _ = _resolve([{"rule": rule, "value": v["value"]}], lib)
            assert len(resolved) == v["count"], (rule, v)


def test_decade_choices_are_the_decades_in_plex(plex):
    assert _values("decade") == [{"value": "1980", "label": "1980s", "count": 2},
                                 {"value": "1990", "label": "1990s", "count": 1},
                                 {"value": "2000", "label": "2000s", "count": 1},
                                 {"value": "2010", "label": "2010s", "count": 1}]


def test_actor_choices_use_the_same_top_three_cut_as_the_rule(plex):
    assert "Jim Varney" not in {v["value"] for v in _values("actor")}


def test_show_choices_read_the_show_libraries(plex):
    assert {v["value"] for v in _values("genre", "show")} == {"Action", "Comedy"}


def test_choices_are_none_when_plex_cannot_be_read(plex):
    plex.down = True
    assert _values("genre") is None


def test_one_fetch_serves_every_pick_list(plex):
    cache = {}
    for rule in ("genre", "studio", "actor"):
        _values(rule, cache=cache)
    assert plex.calls.count("/library/sections/1/all?type=1") == 1


def test_the_choices_endpoint(plex, tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"plex_url": "http://plex", "plex_token": "t"}))
    channels_router._values_cache.update(at=0.0, plex={})
    out = asyncio.run(channels_router.library_rule_values("genre", "movie"))
    assert {v["value"] for v in out["values"]} >= {"Animation", "Action"}


def test_the_choices_endpoint_errors_are_plain(plex, tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"plex_url": "http://plex", "plex_token": "t"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("mood", "movie"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("genre", "anime"))
    assert e.value.status_code == 400
    plex.down = True
    channels_router._values_cache.update(at=0.0, plex={})
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("genre", "movie"))
    assert e.value.status_code == 502
    (tmp_path / "config.json").write_text(json.dumps({}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("genre", "movie"))
    assert e.value.status_code == 502
