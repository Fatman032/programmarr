"""test_jellyfin_export.py — Export/Scan and the library picker read Jellyfin the way they read Plex (beta).

Export/Scan listed Plex's library sections, read every movie and show from Plex, cross-checked
them against Tunarr and wrote the CSV the Planner works from. With Jellyfin as the media server
it now does the same four steps against Jellyfin (its libraries, its movies and shows with the
same details), and the library picker lists Jellyfin's libraries. The Plex path is unchanged.
"""

import csv
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
import export
import jellyfin_api
from routers import pipeline_router


# ── a fake Jellyfin and a fake Tunarr ───────────────────────────────────────────────

def _person(name, kind):
    return {"Name": name, "Type": kind}


JF_MOVIES = [
    {"Id": "m1", "Name": "Heat", "ProductionYear": 1995, "OfficialRating": "R",
     "Genres": ["Crime", "Drama"], "Studios": [{"Name": "Warner Bros."}, {"Name": "Regency"}],
     "ProductionLocations": ["United States of America"],
     "People": [_person("Michael Mann", "Director"), _person("Al Pacino", "Actor"), _person("Robert De Niro", "Actor"),
                _person("Val Kilmer", "Actor"), _person("Jon Voight", "Actor"), _person("Dante Spinotti", "Writer")]},
    {"Id": "m2", "Name": "Aladdin", "ProductionYear": 1992, "Genres": ["Animation"], "Studios": [], "People": []},
    {"Id": "m3", "Name": "Not Synced", "ProductionYear": 2001, "Genres": ["Drama"], "People": []},
]
JF_SHOWS = [{"Id": "s1", "Name": "Cheers", "ProductionYear": 1982, "OfficialRating": "TV-PG",
             "Genres": ["Comedy"], "Studios": [{"Name": "NBC"}], "People": [_person("Ted Danson", "Actor")]}]
JF_EPISODES = [{"SeriesId": "s1", "ParentIndexNumber": 1}, {"SeriesId": "s1", "ParentIndexNumber": 1},
               {"SeriesId": "s1", "ParentIndexNumber": 2}]
JF_LIBRARIES = [{"Name": "Movies", "ItemId": "lm", "CollectionType": "movies"},
                {"Name": "Shows", "ItemId": "ls", "CollectionType": "tvshows"},
                {"Name": "Music", "ItemId": "mu", "CollectionType": "music"}]


@pytest.fixture
def jellyfin(monkeypatch):
    calls = []

    def fake_get(base_url, api_key, path, timeout=120):
        calls.append(path)
        if path == "/Library/VirtualFolders":
            return JF_LIBRARIES
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        kind, parent = q["IncludeItemTypes"][0], q["ParentId"][0]
        if (kind, parent) == ("Movie", "lm"):
            return {"Items": JF_MOVIES}
        if (kind, parent) == ("Series", "ls"):
            return {"Items": JF_SHOWS}
        if (kind, parent) == ("Episode", "ls"):
            return {"Items": JF_EPISODES}
        return {"Items": []}
    monkeypatch.setattr(jellyfin_api, "get", fake_get)
    return calls


TUNARR_SOURCES = [{"type": "jellyfin", "name": "Jellyfined", "uri": "http://192.0.2.1:8096", "libraries": [
    {"id": "tm", "name": "Movies", "mediaType": "movies", "enabled": True},
    {"id": "ts", "name": "Shows", "mediaType": "shows", "enabled": True}]}]
TUNARR_PROGRAMS = {
    "tm": [{"program": {"title": "Heat"}}, {"program": {"title": "Aladdin"}},
           {"program": {"title": "Only In Tunarr", "year": 2010, "genres": [{"name": "Drama"}]}}],
    "ts": [{"program": {"title": "Pilot", "show": {"title": "Cheers"}, "season": {"index": 1}}}],
}


@pytest.fixture
def tunarr(monkeypatch):
    def fake_tunarr_get(base_url, path, timeout=60):
        if path == "/api/media-sources":
            return TUNARR_SOURCES
        return next((v for k, v in TUNARR_PROGRAMS.items() if path.endswith(f"/{k}/programs")), [])
    monkeypatch.setattr(export, "tunarr_get", fake_tunarr_get)


def _run_export(tmp_path, monkeypatch, config, *args):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["export.py", *args])
    export.main()
    with open(tmp_path / "plex_library.csv", encoding="utf-8") as f:
        return {r["Title"]: r for r in csv.DictReader(f)}


JF_CONFIG = {"tunarr_url": "http://t", "media_server": "jellyfin",
             "jellyfin_url": "http://jf:8096", "jellyfin_api_key": "KEY"}


# ── the export ──────────────────────────────────────────────────────────────────────

def test_a_jellyfin_movie_gets_the_same_columns_a_plex_movie_does(tmp_path, monkeypatch, jellyfin, tunarr):
    rows = _run_export(tmp_path, monkeypatch, JF_CONFIG)
    heat = rows["Heat"]
    assert heat["Year"] == "1995" and heat["Type"] == "Movie" and heat["Rating"] == "R"
    assert heat["Genres"] == "Crime|Drama" and heat["Director"] == "Michael Mann"
    assert heat["Studio"] == "Warner Bros.|Regency" and heat["Country"] == "United States of America"
    assert heat["Actors"] == "Al Pacino|Robert De Niro|Val Kilmer"  # top three billed, like Plex


def test_a_jellyfin_show_gets_its_season_and_episode_counts(tmp_path, monkeypatch, jellyfin, tunarr):
    cheers = _run_export(tmp_path, monkeypatch, JF_CONFIG)["Cheers"]
    assert cheers["Type"] == "TV" and cheers["Seasons"] == "2" and cheers["Episodes"] == "3"
    assert cheers["Genres"] == "Comedy" and cheers["Studio"] == "NBC" and cheers["Rating"] == "TV-PG"


def test_what_tunarr_has_not_synced_is_left_out_like_with_plex(tmp_path, monkeypatch, jellyfin, tunarr):
    rows = _run_export(tmp_path, monkeypatch, JF_CONFIG)
    assert "Not Synced" not in rows and "Aladdin" in rows


def test_titles_only_in_tunarr_are_still_added(tmp_path, monkeypatch, jellyfin, tunarr):
    assert "Only In Tunarr" in _run_export(tmp_path, monkeypatch, JF_CONFIG)


def test_non_video_libraries_are_never_read(tmp_path, monkeypatch, jellyfin, tunarr):
    _run_export(tmp_path, monkeypatch, JF_CONFIG)
    assert not any("ParentId=mu" in c for c in jellyfin)


def test_the_libraries_picked_in_the_ui_are_the_ones_exported(tmp_path, monkeypatch, jellyfin, tunarr):
    rows = _run_export(tmp_path, monkeypatch, JF_CONFIG, "--movie-sections", "0:lm", "--tv-sections", "",
                       "--tunarr-movie-libs", "", "--tunarr-tv-libs", "")  # extra Tunarr libraries off too
    assert "Heat" in rows and "Cheers" not in rows


def test_a_jellyfin_setup_without_its_url_and_key_stops_with_a_clear_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t", "media_server": "jellyfin"}))
    with pytest.raises(SystemExit):
        export.load_config()
    assert "Jellyfin connection missing" in capsys.readouterr().out


def test_the_plex_export_still_works_the_same(tmp_path, monkeypatch, tunarr):
    def fake_plex_get(base_url, token, path, timeout=60):
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"},
                                                      {"key": "2", "title": "TV", "type": "show"}]}}
        if path.startswith("/library/sections/1/all"):
            return {"MediaContainer": {"Metadata": [{"title": "Heat", "year": 1995, "contentRating": "R",
                                                     "Genre": [{"tag": "Crime"}], "Director": [{"tag": "Michael Mann"}],
                                                     "studio": "Warner Bros.", "Role": [{"tag": "Al Pacino"}]}]}}
        if path.startswith("/library/sections/2/all"):
            return {"MediaContainer": {"Metadata": [{"title": "Cheers", "year": 1982, "childCount": 11, "leafCount": 275}]}}
        return None
    monkeypatch.setattr(export, "plex_get", fake_plex_get)
    rows = _run_export(tmp_path, monkeypatch, {"tunarr_url": "http://t", "plex_url": "http://p", "plex_token": "x"})
    assert rows["Heat"]["Genres"] == "Crime" and rows["Heat"]["Director"] == "Michael Mann"
    assert rows["Cheers"]["Seasons"] == "11" and rows["Cheers"]["Episodes"] == "275"


# ── the Jellyfin helper ─────────────────────────────────────────────────────────────

def test_libraries_map_jellyfins_kinds_to_movie_and_show(jellyfin):
    libs = jellyfin_api.libraries("http://jf", "KEY")
    assert [(l["id"], l["type"]) for l in libs] == [("lm", "movie"), ("ls", "show"), ("mu", None)]


def test_an_unreadable_jellyfin_is_none_not_an_empty_library(monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda *a, **k: None)
    assert jellyfin_api.libraries("http://jf", "KEY") is None
    assert jellyfin_api.items("http://jf", "KEY", "lm", "Movie") is None


def test_items_asks_for_everything_under_a_library_with_the_api_key(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return b'{"Items": []}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp()
    monkeypatch.setattr(jellyfin_api.urllib.request, "urlopen", fake_urlopen)
    assert jellyfin_api.items("http://jf:8096/", "KEY", "lm", "Movie") == []
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert seen["url"].startswith("http://jf:8096/Items?")
    assert q["ParentId"] == ["lm"] and q["Recursive"] == ["true"] and q["IncludeItemTypes"] == ["Movie"]
    assert "Genres" in q["Fields"][0] and "People" in q["Fields"][0]
    assert seen["headers"]["X-emby-token"] == "KEY"


def test_episode_counts_count_each_season_once(jellyfin):
    assert jellyfin_api.episode_counts("http://jf", "KEY", "ls") == {"s1": (2, 3)}


# ── the library picker ──────────────────────────────────────────────────────────────

@pytest.fixture
def picker(monkeypatch, jellyfin):
    holder = {"cfg": dict(JF_CONFIG)}
    monkeypatch.setattr(pipeline_router, "_load_config", lambda: holder["cfg"])
    sources = TUNARR_SOURCES + [{"type": "plex", "name": "Friend's Plex", "uri": "http://friend:32400", "libraries": [
        {"id": "fp", "name": "Friend Movies", "mediaType": "movies", "enabled": True}]}]
    monkeypatch.setattr(channel_engine, "api", lambda url, method, path, body=None, timeout=60:
                        sources if path == "/api/media-sources" else None)
    return holder


def test_the_picker_lists_jellyfins_own_libraries(picker):
    libs = pipeline_router.list_libraries()
    mine = [l for l in libs if l["key"].startswith("jellyfin:")]
    assert [(l["key"], l["title"], l["type"]) for l in mine] == [("jellyfin:lm", "Movies", "movie"), ("jellyfin:ls", "Shows", "show")]


def test_the_picker_does_not_list_the_same_jellyfin_twice_but_keeps_other_sources(picker):
    keys = [l["key"] for l in pipeline_router.list_libraries()]
    assert "tunarr:tm" not in keys and "tunarr:ts" not in keys  # Tunarr's copy of the same Jellyfin
    assert "tunarr:fp" in keys                                    # a different server stays


def test_the_picker_without_jellyfin_settings_says_so(picker):
    picker["cfg"] = {"tunarr_url": "http://t", "media_server": "jellyfin"}
    with pytest.raises(HTTPException) as e:
        pipeline_router.list_libraries()
    assert e.value.status_code == 400 and "Jellyfin not configured" in e.value.detail


def test_the_picker_explains_an_unreadable_jellyfin(picker, monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda *a, **k: None)
    picker["cfg"] = {**JF_CONFIG, "tunarr_url": ""}
    with pytest.raises(HTTPException) as e:
        pipeline_router.list_libraries()
    assert e.value.status_code == 502 and "Could not read Jellyfin" in e.value.detail
