"""test_jellyfin_rules.py — live rules ({genre: ..}, studio, director, actor, decade) on Jellyfin (BETA).

The same as on Plex: the rule is asked of the server again on every refresh, so a new movie with
that genre joins on its own; each match is found in Tunarr by TMDB, IMDB, the server's own id,
then title + year; a server that can't be read means "not checked" (the channel is left alone),
never "nothing matches". A rule asks Plex when Plex is set up, else Jellyfin ("server" pins it).
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
import jellyfin_api
from routers import channels_router
from test_jellyfin_collections import _jf_episode, _jf_movie
from test_id_index import _index


def _jm(name, year, jid, genres=(), studios=(), directors=(), actors=(), tmdb=None, imdb=None):
    """One movie as Jellyfin lists it."""
    people = [{"Name": d, "Type": "Director"} for d in directors] + [{"Name": a, "Type": "Actor"} for a in actors]
    return {"Id": jid, "Name": name, "Type": "Movie", "ProductionYear": year, "Genres": list(genres),
            "Studios": [{"Name": s} for s in studios], "People": people,
            "ProviderIds": {k: v for k, v in (("Tmdb", tmdb), ("Imdb", imdb)) if v}}


JF_MOVIES = [
    _jm("Antz", 1998, "j-antz", genres=["Animation", "Comedy"], studios=["DreamWorks Animation"],
        directors=["Eric Darnell"], actors=["Woody Allen", "Dan Aykroyd", "Anne Bancroft", "Jennifer Lopez"],
        tmdb="8916", imdb="tt0120587"),
    _jm("Shrek", 2001, "j-shrek", genres=["Animation"], studios=["DreamWorks Animation"], tmdb="808"),
    _jm("Heat", 1995, "j-heat", genres=["Crime", "Action"], studios=["Warner Bros."], directors=["Michael Mann"],
        imdb="tt0113277"),
    _jm("New Movie", 2026, "j-new", genres=["Animation"], tmdb="999999"),   # Tunarr hasn't scanned it yet
]
JF_SHOWS = [{"Id": "j-cheers", "Name": "Cheers", "Type": "Series", "ProductionYear": 1982, "Genres": ["Comedy"],
             "Studios": [{"Name": "NBC"}], "People": [], "ProviderIds": {"Tmdb": "3918"}}]


@pytest.fixture
def jellyfin(monkeypatch):
    """Fake Jellyfin with a movie and a show library. `jellyfin.calls` counts listings;
    `jellyfin.down = True` makes it unreadable."""
    state = type("JF", (), {"calls": [], "fields": [], "person_calls": [], "down": False})()
    channel_engine.set_jellyfin("http://jf:8096", "K")

    def libraries(url, key):
        return None if state.down else [{"id": "mv", "name": "Movies", "type": "movie"},
                                        {"id": "tv", "name": "Shows", "type": "show"}]

    def items(url, key, parent_id, item_type, fields=None):
        state.calls.append((parent_id, item_type))
        state.fields.append(fields)
        return None if state.down else (JF_MOVIES if item_type == "Movie" else JF_SHOWS)
    def person_name(url, key, name):
        if state.down:
            return None
        names = {p["Name"] for m in JF_MOVIES + JF_SHOWS for p in m["People"]}
        return next((n for n in names if n.lower() == name.strip().lower()), name)

    def person_items(url, key, parent_id, item_type, name, role, fields=None):
        """Jellyfin's person filter: exact spelling, credited in that role."""
        state.person_calls.append((parent_id, item_type, name, role))
        pool = JF_MOVIES if item_type == "Movie" else JF_SHOWS
        return [m for m in pool if any(p["Name"] == name and p["Type"] == role for p in m["People"])]

    monkeypatch.setattr(jellyfin_api, "libraries", libraries)
    monkeypatch.setattr(jellyfin_api, "items", items)
    monkeypatch.setattr(jellyfin_api, "person_name", person_name)
    monkeypatch.setattr(jellyfin_api, "person_items", person_items)
    return state


@pytest.fixture
def library(monkeypatch):
    """What Tunarr holds from Jellyfin (no "New Movie" yet)."""
    return _index(monkeypatch, [("mv", "movies"), ("tv", "shows")], {
        "mv": [_jf_movie("Antz", 1998, "t-antz", tmdb="8916", imdb="tt0120587", jellyfin="j-antz"),
               _jf_movie("Shrek", 2001, "t-shrek", tmdb="808", jellyfin="j-shrek"),
               _jf_movie("Heat", 1995, "t-heat", imdb="tt0113277", jellyfin="j-heat")],
        "tv": [_jf_episode("Cheers", "t-cheers", 1982, tmdb="3918", jellyfin="j-cheers", n=i) for i in range(5)],
    })


def _resolve(content, library, **kw):
    movie_map, show_map, idx = library
    report = {}
    resolved, _ = channel_engine.resolve_content(content, movie_map, show_map, collection_cache={},
                                                 id_index=idx, report=report, **kw)
    return sorted(r.get("ids", {}).get("tunarr") or r.get("showId") for r in resolved), report


# ── the rules ─────────────────────────────────────────────────────────────────────────

def test_a_genre_rule_asks_jellyfin(jellyfin, library):
    ids, report = _resolve([{"rule": "genre", "value": "animation"}], library)
    assert ids == ["t-antz", "t-shrek"]


def test_studio_director_actor_and_decade(jellyfin, library):
    assert _resolve([{"rule": "studio", "value": "DreamWorks Animation"}], library)[0] == ["t-antz", "t-shrek"]
    assert _resolve([{"rule": "director", "value": "Michael Mann"}], library)[0] == ["t-heat"]
    assert _resolve([{"rule": "actor", "value": "Dan Aykroyd"}], library)[0] == ["t-antz"]
    assert _resolve([{"rule": "decade", "value": "1990s"}], library)[0] == ["t-antz", "t-heat"]


def test_only_the_top_billed_actors_count(jellyfin, library):
    """The same cut as the export and Plex: the 4th-billed actor doesn't make a match."""
    assert _resolve([{"rule": "actor", "value": "Jennifer Lopez"}], library)[0] == []


def test_a_tv_rule_asks_for_shows(jellyfin, library):
    assert _resolve([{"rule": "genre", "value": "Comedy", "kind": "show"}], library)[0] == ["t-cheers"]


def test_matches_are_found_by_imdb_when_there_is_no_tmdb(jellyfin, library):
    assert _resolve([{"rule": "genre", "value": "Crime"}], library)[0] == ["t-heat"]


def test_a_match_tunarr_hasnt_scanned_is_reported_against_jellyfin(jellyfin, library):
    _, report = _resolve([{"rule": "genre", "value": "Animation"}], library)
    why = report["missing"][0]["why"]
    assert "1 in Jellyfin but not in Tunarr yet" in why and "New Movie" in why


def test_a_rule_that_matches_nothing_names_jellyfin(jellyfin, library):
    _, report = _resolve([{"rule": "studio", "value": "No Such Studio"}], library)
    assert report["missing"] == [{"label": "[studio:No Such Studio]", "why": "matched nothing in Jellyfin"}]


def test_unreadable_jellyfin_blocks_the_rule(jellyfin, library):
    jellyfin.down = True
    _, report = _resolve([{"rule": "genre", "value": "Animation"}], library)
    assert report["blocked"] and "Jellyfin could not be read" in report["blocked"][0]


def test_no_server_at_all_blocks_the_rule(library):
    channel_engine.set_jellyfin()
    _, report = _resolve([{"rule": "genre", "value": "Animation"}], library)
    assert report["blocked"] and "no media server is set up" in report["blocked"][0]


def test_jellyfin_is_listed_once_per_refresh(jellyfin, library):
    movie_map, show_map, idx = library
    cache = {}
    for value in ("Animation", "Crime", "Comedy"):
        channel_engine.resolve_content([{"rule": "genre", "value": value}], movie_map, show_map,
                                       collection_cache=cache, id_index=idx)
    assert jellyfin.calls == [("mv", "Movie")]


# ── which server a rule asks ──────────────────────────────────────────────────────────

def test_which_server(jellyfin):
    assert channel_engine.rule_server({}, "http://p", "t") == "plex"
    assert channel_engine.rule_server({}, "", "") == "jellyfin"
    assert channel_engine.rule_server({"server": "jellyfin"}, "http://p", "t") == "jellyfin"
    assert channel_engine.rule_server({"server": "plex"}, "", "") is None
    channel_engine.set_jellyfin()
    assert channel_engine.rule_server({}, "", "") is None


def test_with_both_servers_a_rule_asks_plex_unless_pinned(jellyfin, library, monkeypatch):
    monkeypatch.setattr(channel_engine, "_plex_listing", lambda *a: [])
    plex = {"plex_url": "http://p", "plex_token": "t"}
    assert _resolve([{"rule": "genre", "value": "Animation"}], library, **plex)[0] == []
    assert _resolve([{"rule": "genre", "value": "Animation", "server": "jellyfin"}], library, **plex)[0] == [
        "t-antz", "t-shrek"]


# ── the pick-list ─────────────────────────────────────────────────────────────────────

def test_choices_come_from_jellyfin_with_counts(jellyfin):
    values = channel_engine.rule_values("genre", "movie", "", "", [], {})
    assert values[:2] == [{"value": "Animation", "label": "Animation", "count": 3},
                          {"value": "Action", "label": "Action", "count": 1}]
    assert [v["value"] for v in channel_engine.rule_values("decade", "movie", "", "", [], {})] == [
        "1990", "2000", "2020"]


def test_the_choices_endpoint_on_a_jellyfin_setup(jellyfin, tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    channels_router._values_cache.update(at=0.0, plex={})
    (tmp_path / "config.json").write_text(json.dumps({"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "K"}))
    got = asyncio.run(channels_router.library_rule_values("studio", "movie"))
    assert got["server"] == "Jellyfin" and got["values"][0]["value"] == "DreamWorks Animation"


def test_the_choices_endpoint_says_which_server_failed(jellyfin, tmp_path, monkeypatch):
    jellyfin.down = True
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    channels_router._values_cache.update(at=0.0, plex={})
    (tmp_path / "config.json").write_text(json.dumps({"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "K"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("genre", "movie"))
    assert e.value.status_code == 502 and e.value.detail == "Jellyfin could not be read"


def test_the_choices_endpoint_with_no_server(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(channels_router.library_rule_values("genre", "movie"))
    assert e.value.status_code == 502


# ── cast and crew only when a rule needs them ─────────────────────────────────────────

def test_a_genre_rule_doesnt_ask_for_cast_and_crew(jellyfin, library):
    """Asking Jellyfin for People made saving a {genre: Comedy} channel take about a minute
    (54 s for 1,630 movies, against half a second without)."""
    _resolve([{"rule": "genre", "value": "Animation"}], library)
    assert "People" not in jellyfin.fields[0]


def test_actor_and_director_rules_ask_for_that_one_person(jellyfin, library):
    """Everyone's cast took ~24 s per save; one person's items take well under a second."""
    assert _resolve([{"rule": "actor", "value": "dan aykroyd"}], library)[0] == ["t-antz"]
    assert _resolve([{"rule": "director", "value": "MICHAEL MANN"}], library)[0] == ["t-heat"]
    assert jellyfin.calls == []   # no full listing at all
    assert jellyfin.person_calls == [("mv", "Movie", "Dan Aykroyd", "Actor"), ("mv", "Movie", "Michael Mann", "Director")]


def test_an_actor_rule_still_counts_only_the_top_billed(jellyfin, library):
    """Jellyfin's person filter finds any credit; the rule keeps the same top-3 cut as Plex."""
    assert _resolve([{"rule": "actor", "value": "Jennifer Lopez"}], library)[0] == []


def test_an_unknown_person_matches_nothing(jellyfin, library):
    ids, report = _resolve([{"rule": "actor", "value": "Nobody At All"}], library)
    assert ids == [] and report["missing"][0]["why"] == "matched nothing in Jellyfin"


def test_unreadable_jellyfin_blocks_a_person_rule(jellyfin, library):
    jellyfin.down = True
    _, report = _resolve([{"rule": "actor", "value": "Dan Aykroyd"}], library)
    assert report["blocked"] and "Jellyfin could not be read" in report["blocked"][0]


def test_the_actor_pick_list_still_reads_everyones_cast(jellyfin):
    values = channel_engine.rule_values("actor", "movie", "", "", [], {})
    assert "People" in jellyfin.fields[0] and {"value": "Woody Allen", "label": "Woody Allen", "count": 1} in values


def test_the_genre_pick_list_doesnt_ask_for_cast_and_crew(jellyfin):
    channel_engine.rule_values("genre", "movie", "", "", [], {})
    assert "People" not in jellyfin.fields[0]
