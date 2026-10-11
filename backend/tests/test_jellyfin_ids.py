"""test_jellyfin_ids.py — an item is known by the same numbers, in the same order, on any server.

Tunarr describes a movie/show the same way whether it came from Plex or Jellyfin: outside ids
(TMDB, IMDB) plus the server's own id. So Programmarr matches in ONE order everywhere:

    1. TMDB   2. IMDB   3. the server's own id (plex / jellyfin)   4. Tunarr's id
    ...and as a last resort the exact title + year, if that names exactly one item.

The outside ids come first so a saved channel survives a rename, a re-add, and a move from
one server to another; the server's own id only works on that server. (Jellyfin is BETA.)
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
from test_id_index import _index, _library


def _movie(title, year, tmdb=None, imdb=None, server=None, server_id=None, tunarr="t", src="srcA"):
    """A movie as Tunarr lists it. `server` is "plex" or "jellyfin"."""
    idents = []
    if tmdb:
        idents.append({"id": tmdb, "type": "tmdb"})
    if imdb:
        idents.append({"id": imdb, "type": "imdb"})
    if server and server_id:
        idents.append({"id": server_id, "type": server, "sourceId": src})
    return {"id": tunarr, "program": {"title": title, "year": year, "state": "ok",
                                      "mediaSourceId": src, "identifiers": idents}}


def _lib(monkeypatch, *movies):
    return _index(monkeypatch, [("mv", "movies")], {"mv": list(movies)})


# ── the order ───────────────────────────────────────────────────────────────────────

def test_the_numbers_are_tried_in_the_agreed_order():
    assert channel_engine._ID_PRIORITY == ("tmdb", "imdb", "plex", "jellyfin", "tunarr")


@pytest.mark.parametrize("server", ["plex", "jellyfin"])
def test_an_item_keeps_every_number_it_has_whichever_server_it_is_on(monkeypatch, server):
    _, _, idx = _lib(monkeypatch, _movie("Heat", 1995, tmdb="949", imdb="tt0113277", server=server,
                                         server_id="900", tunarr="tid"))
    (item,) = idx["all"]["movie"]
    assert item["ids"] == {"tmdb": "949", "imdb": "tt0113277", server: "srcA:900", "tunarr": "tid"}


def test_a_servers_own_id_is_namespaced_by_its_media_source(monkeypatch):
    """Two servers can reuse a number; the source id keeps them apart."""
    _, _, idx = _lib(monkeypatch, _movie("A", 2000, server="jellyfin", server_id="7", tunarr="a", src="jfOne"),
                     _movie("B", 2001, server="jellyfin", server_id="7", tunarr="b", src="jfTwo"))
    ids = {it["title"]: it["ids"]["jellyfin"] for it in idx["all"]["movie"]}
    assert ids == {"A": "jfOne:7", "B": "jfTwo:7"}


# ── finding things ──────────────────────────────────────────────────────────────────

def test_a_movie_with_no_tmdb_number_is_found_by_its_imdb_number(monkeypatch):
    """E.g. a fan edit TMDB doesn't list, but IMDB does."""
    _, _, idx = _lib(monkeypatch, _movie("The Patterson Cut", 2023, imdb="tt35121657", server="jellyfin",
                                         server_id="x1", tunarr="pc"))
    item, status, _ = channel_engine.resolve_by_ids("movie", {"imdb": "tt35121657"}, "something else", 1999, idx)
    assert item["title"] == "The Patterson Cut" and status == "ok"


def test_a_stale_tmdb_number_is_healed_through_imdb(monkeypatch):
    _, _, idx = _lib(monkeypatch, _movie("Heat", 1995, tmdb="949", imdb="tt0113277", tunarr="h"))
    item, status, current = channel_engine.resolve_by_ids("movie", {"tmdb": "WRONG", "imdb": "tt0113277"}, "Heat", 1995, idx)
    assert status == "healed" and current["tmdb"] == "949"


def test_imdb_is_tried_before_the_servers_own_id(monkeypatch):
    """Two items: one matches the saved IMDB, a different one matches the saved server id."""
    _, _, idx = _lib(monkeypatch,
                     _movie("By Imdb", 2000, imdb="tt1", server="plex", server_id="1", tunarr="a"),
                     _movie("By Server", 2001, imdb="tt2", server="plex", server_id="2", tunarr="b"))
    item, _, _ = channel_engine.resolve_by_ids("movie", {"imdb": "tt1", "plex": "srcA:2"}, "x", None, idx)
    assert item["title"] == "By Imdb"


def test_the_servers_own_id_is_tried_before_tunarrs(monkeypatch):
    _, _, idx = _lib(monkeypatch,
                     _movie("By Server", 2000, server="jellyfin", server_id="1", tunarr="a"),
                     _movie("By Tunarr", 2001, server="jellyfin", server_id="2", tunarr="b"))
    item, _, _ = channel_engine.resolve_by_ids("movie", {"jellyfin": "srcA:1", "tunarr": "b"}, "x", None, idx)
    assert item["title"] == "By Server"


def test_two_remakes_stay_apart_and_each_is_found_by_every_number(monkeypatch):
    _, _, idx = _lib(monkeypatch,
                     _movie("Aladdin", 1992, tmdb="812", imdb="tt0103639", server="jellyfin", server_id="1", tunarr="a92"),
                     _movie("Aladdin", 2019, tmdb="420817", imdb="tt6139732", server="jellyfin", server_id="2", tunarr="a19"))
    for item in idx["all"]["movie"]:
        for source in ("tmdb", "imdb", "jellyfin", "tunarr"):
            assert idx["by_id"]["movie"][(source, item["ids"][source])] is item


# ── a pin made on one server on another ─────────────────────────────────────────────

def test_a_pin_made_on_plex_finds_the_same_movie_on_jellyfin_through_tmdb(monkeypatch):
    _, _, idx = _lib(monkeypatch, _movie("Aladdin", 2019, tmdb="420817", imdb="tt6139732", server="jellyfin",
                                         server_id="jf-1", tunarr="jf-tunarr"))
    plex_pin = {"tmdb": "420817", "imdb": "tt6139732", "plex": "plexSrc:555", "tunarr": "plex-tunarr"}
    item, status, current = channel_engine.resolve_by_ids("movie", plex_pin, "Aladdin", 2019, idx)
    assert item["year"] == 2019 and status == "ok" and "jellyfin" in current


def test_a_pin_with_no_outside_numbers_falls_back_to_exact_title_and_year(monkeypatch):
    """The family-video case: no TMDB or IMDB, and the other server's own ids mean nothing here."""
    _, _, idx = _lib(monkeypatch, _movie("Christmas 2019", 2019, server="jellyfin", server_id="9", tunarr="fam"))
    pin = {"plex": "plexSrc:1", "tunarr": "plex-tunarr"}
    item, status, _ = channel_engine.resolve_by_ids("movie", pin, "Christmas 2019", 2019, idx)
    assert item["title"] == "Christmas 2019" and status == "healed"
    item, status, _ = channel_engine.resolve_by_ids("movie", pin, "Christmas 2019", 2020, idx)
    assert item is None and status == "missing"


def test_a_title_and_year_that_fit_two_different_movies_is_flagged_not_guessed(monkeypatch):
    _, _, idx = _lib(monkeypatch, _movie("Same", 2000, tmdb="1", tunarr="a"), _movie("Same", 2000, tmdb="2", tunarr="b"))
    item, status, _ = channel_engine.resolve_by_ids("movie", {"plex": "x:1"}, "Same", 2000, idx)
    assert item is None and status == "ambiguous"


def test_an_old_saved_entry_with_only_the_old_numbers_still_works(monkeypatch):
    """Entries saved before IMDB was kept have tmdb / plex / tunarr only."""
    _, _, idx = _lib(monkeypatch, _movie("Heat", 1995, tmdb="949", imdb="tt0113277", server="plex", server_id="900", tunarr="h"))
    item, status, _ = channel_engine.resolve_by_ids("movie", {"tmdb": "949", "plex": "srcA:900", "tunarr": "h"}, "Heat", 1995, idx)
    assert item["title"] == "Heat" and status == "ok"


# ── which Tunarr sources are read ───────────────────────────────────────────────────

def test_plex_and_jellyfin_sources_are_read_but_not_emby(monkeypatch):
    sources = [{"type": "plex", "name": "P"}, {"type": "jellyfin", "name": "J"},
               {"type": "emby", "name": "E"}, {"type": "local", "name": "L"}]
    monkeypatch.setattr(channel_engine, "api", lambda url, method, path, body=None, timeout=60: sources)
    assert [s["name"] for s in channel_engine.get_library_sources("http://t")] == ["P", "J"]


def test_a_tunarr_with_both_a_plex_and_a_jellyfin_source_indexes_both(monkeypatch):
    """The same movie on both servers is ONE item by its outside numbers (best copy kept)."""
    plex = {"name": "Plex", "libraries": [{"id": "pm", "name": "Movies", "mediaType": "movies", "enabled": True}]}
    jelly = {"name": "Jellyfin", "libraries": [{"id": "jm", "name": "Movies", "mediaType": "movies", "enabled": True}]}
    monkeypatch.setattr(channel_engine, "get_library_sources", lambda url: [plex, jelly])
    programs = {"pm": [_movie("Heat", 1995, tmdb="949", server="plex", server_id="1", tunarr="plex-heat", src="P")],
                "jm": [_movie("Heat", 1995, tmdb="949", server="jellyfin", server_id="2", tunarr="jf-heat", src="J"),
                       _movie("Only On Jellyfin", 2001, tmdb="5", tunarr="jf-only", src="J")]}
    monkeypatch.setattr(channel_engine, "api", lambda url, method, path, body=None, timeout=60:
                        next(v for k, v in programs.items() if path.endswith(f"/{k}/programs")))
    _, _, idx = channel_engine.build_library_index_with_ids("http://t")
    assert idx["by_id"]["movie"][("tmdb", "949")]["title"] == "Heat"
    assert idx["by_id"]["movie"][("plex", "P:1")]["ids"]["tunarr"] == "plex-heat"
    assert idx["by_id"]["movie"][("jellyfin", "J:2")]["ids"]["tunarr"] == "jf-heat"
    assert idx["by_id"]["movie"][("tmdb", "5")]["title"] == "Only On Jellyfin"
