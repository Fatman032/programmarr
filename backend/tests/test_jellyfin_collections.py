"""test_jellyfin_collections.py — channels built from Jellyfin collections (BETA), like Plex's.

A {"collection": "Name"} entry is asked of Plex and/or Jellyfin on every refresh (Plex first,
then Jellyfin; "server" pins it to one). Each member is matched the same way on both servers:
TMDB, then IMDB, then the server's own id, then title + year — never a bare title alone.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import channel_engine
import jellyfin_api
from test_id_index import _index

JF_SOURCE = "jf-src"


def _jf_movie(title, year, tunarr, tmdb=None, imdb=None, jellyfin=None):
    idents = [{"type": t, "id": v} for t, v in (("tmdb", tmdb), ("imdb", imdb), ("jellyfin", jellyfin)) if v]
    return {"id": tunarr, "program": {"title": title, "year": year, "state": "ok",
                                      "mediaSourceId": JF_SOURCE, "identifiers": idents}}


def _jf_episode(show, uuid, year, tmdb=None, jellyfin=None, n=1):
    idents = [{"type": t, "id": v} for t, v in (("tmdb", tmdb), ("jellyfin", jellyfin)) if v]
    return {"id": f"{uuid}-e{n}", "program": {"title": f"{show} S1E{n}", "state": "ok", "show": {
        "uuid": uuid, "title": show, "year": year, "mediaSourceId": JF_SOURCE, "identifiers": idents}}}


@pytest.fixture
def library(monkeypatch):
    """Two Aladdins (1992 and 2019) and a Wonder Woman that is both a movie and a show."""
    return _index(monkeypatch, [("mv", "movies"), ("tv", "shows")], {
        "mv": [_jf_movie("Aladdin", 1992, "al-92", tmdb="812", imdb="tt0103639", jellyfin="j-al92"),
               _jf_movie("Aladdin", 2019, "al-19", tmdb="420817", imdb="tt6139732", jellyfin="j-al19"),
               _jf_movie("Wonder Woman", 2017, "ww-mv", tmdb="297762", jellyfin="j-wwmv"),
               _jf_movie("Untitled Cut", 2020, "uc", imdb="tt9999999", jellyfin="j-uc")],
        "tv": [_jf_episode("Wonder Woman", "ww-tv", 1975, tmdb="1924", jellyfin="j-wwtv", n=i) for i in range(30)],
    })


def _jellyfin(monkeypatch, collections):
    """Fake Jellyfin with these collections: {name: [members as jellyfin_api returns them]}."""
    channel_engine.set_jellyfin("http://jf:8096", "KEY")
    asked = []
    monkeypatch.setattr(jellyfin_api, "collections", lambda url, key: (
        asked.append("list") or [{"id": f"bs-{n}", "name": n, "count": len(m), "summary": "", "has_poster": False}
                                 for n, m in collections.items()]))
    monkeypatch.setattr(jellyfin_api, "collection_members", lambda url, key, cid: (
        asked.append(cid) or collections[cid[3:]]))
    return asked


def _member(title, kind, year, **ids):
    return {"title": title, "kind": kind, "year": year, "ids": ids}


def _resolve(content, library, **kw):
    movie_map, show_map, idx = library
    report = {}
    resolved, missing = channel_engine.resolve_content(
        content, movie_map, show_map, collection_cache={}, id_index=idx, report=report, **kw)
    return [r.get("ids", {}).get("tunarr") or r.get("showId") for r in resolved], missing, report


# ── matching: the same order as everywhere else ──────────────────────────────────────

def test_a_member_is_matched_by_tmdb_first(monkeypatch, library):
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 2019, tmdb="420817", jellyfin="j-wrong")]})
    ids, missing, _ = _resolve([{"collection": "Disney"}], library)
    assert ids == ["al-19"] and not missing


def test_then_by_imdb(monkeypatch, library):
    _jellyfin(monkeypatch, {"Odd": [_member("Untitled Cut", "movie", 2020, tmdb="nope", imdb="tt9999999")]})
    assert _resolve([{"collection": "Odd"}], library)[0] == ["uc"]


def test_then_by_jellyfins_own_id(monkeypatch, library):
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992, jellyfin="j-al92")]})
    assert _resolve([{"collection": "Disney"}], library)[0] == ["al-92"]


def test_then_by_title_and_year(monkeypatch, library):
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992)]})
    assert _resolve([{"collection": "Disney"}], library)[0] == ["al-92"]


def test_a_title_alone_that_names_two_things_is_not_guessed(monkeypatch, library):
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", None)]})
    ids, missing, _ = _resolve([{"collection": "Disney"}], library)
    assert ids == [] and missing


def test_a_movie_member_stays_the_movie(monkeypatch, library):
    _jellyfin(monkeypatch, {"DC": [_member("Wonder Woman", "movie", 2017, tmdb="297762"),
                                   _member("Wonder Woman", "show", 1975, tmdb="1924")]})
    assert _resolve([{"collection": "DC"}], library)[0] == ["ww-mv", "ww-tv"]


# ── which server is asked ─────────────────────────────────────────────────────────────

def _plex_has(monkeypatch, name, members):
    def fake(url, token, path, timeout=60):
        path = path.split("?")[0]
        if path == "/library/sections/1/collections":
            return {"MediaContainer": {"Metadata": [{"title": name, "ratingKey": "77"}]}}
        if path == "/library/collections/77/children":
            return {"MediaContainer": {"Metadata": members}}
        return None
    monkeypatch.setattr(channel_engine, "plex_get", fake)


PLEX = {"plex_url": "http://plex", "plex_token": "t", "plex_sections": [{"key": "1"}]}


def test_jellyfin_only_setup_reads_the_collection_from_jellyfin(monkeypatch, library):
    asked = _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992, tmdb="812")]})
    assert _resolve([{"collection": "Disney"}], library)[0] == ["al-92"]
    assert asked == ["list", "bs-Disney"]


def test_plex_is_asked_first_then_jellyfin(monkeypatch, library):
    _plex_has(monkeypatch, "Something Else", [])
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992, tmdb="812")]})
    assert _resolve([{"collection": "Disney"}], library, **PLEX)[0] == ["al-92"]


def test_a_collection_both_servers_have_comes_from_plex(monkeypatch, library):
    _plex_has(monkeypatch, "Disney", [{"title": "Aladdin", "type": "movie", "year": 2019, "ratingKey": "5",
                                        "Guid": [{"id": "tmdb://420817"}, {"id": "imdb://tt6139732"}]}])
    asked = _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992, tmdb="812")]})
    assert _resolve([{"collection": "Disney"}], library, **PLEX)[0] == ["al-19"]
    assert asked == []


def test_server_pins_the_collection_to_jellyfin(monkeypatch, library):
    _plex_has(monkeypatch, "Disney", [{"title": "Aladdin", "type": "movie", "year": 2019, "ratingKey": "5"}])
    _jellyfin(monkeypatch, {"Disney": [_member("Aladdin", "movie", 1992, tmdb="812")]})
    assert _resolve([{"collection": "Disney", "server": "jellyfin"}], library, **PLEX)[0] == ["al-92"]


def test_a_plex_member_is_matched_by_its_tmdb_number_too(monkeypatch, library):
    """Plex members carry their TMDB / IMDB numbers now (includeGuids), so a Plex collection
    finds a movie Tunarr holds from another server — the same order as Jellyfin's."""
    _plex_has(monkeypatch, "Disney", [{"title": "Aladdin", "type": "movie", "year": 1992, "ratingKey": "999",
                                        "Guid": [{"id": "tmdb://812"}]}])
    assert _resolve([{"collection": "Disney"}], library, **PLEX)[0] == ["al-92"]


def test_a_missing_collection_names_the_servers_asked(monkeypatch, library):
    _plex_has(monkeypatch, "Other", [])
    _jellyfin(monkeypatch, {})
    _, missing, report = _resolve([{"collection": "Nope"}], library, **PLEX)
    assert missing == ["[collection:Nope]"]
    assert report["missing"][0]["why"] == "collection not found in Plex or Jellyfin"


def test_without_jellyfin_set_up_nothing_is_asked_of_it(monkeypatch, library):
    called = []
    monkeypatch.setattr(jellyfin_api, "collections", lambda *a: called.append(1))
    _, missing, report = _resolve([{"collection": "Disney"}], library)
    assert not called and report["missing"][0]["why"] == "collection not found in Plex"


def test_the_collection_list_is_read_once_per_refresh(monkeypatch, library):
    asked = _jellyfin(monkeypatch, {"A": [_member("Aladdin", "movie", 1992, tmdb="812")],
                                    "B": [_member("Aladdin", "movie", 2019, tmdb="420817")]})
    movie_map, show_map, idx = library
    cache = {}
    for name in ("A", "B", "A"):
        channel_engine.resolve_content([{"collection": name}], movie_map, show_map,
                                       collection_cache=cache, id_index=idx)
    assert asked == ["list", "bs-A", "bs-B"]


# ── the connection comes from config, like the Tunarr login ──────────────────────────

def test_the_jellyfin_connection_is_set_from_config():
    channel_engine.set_tunarr_auth_from_config({"jellyfin_url": "http://jf:8096/", "jellyfin_api_key": "K"})
    assert channel_engine._JELLYFIN == ("http://jf:8096", "K")
    channel_engine.set_tunarr_auth_from_config({"plex_url": "http://p", "plex_token": "x"})
    assert channel_engine._JELLYFIN is None


# ── jellyfin_api reads collections ────────────────────────────────────────────────────

def test_collections_are_listed_with_their_counts(monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda url, key, path, timeout=120: {"Items": [
        {"Id": "b1", "Name": "Disney ", "ChildCount": 12, "Overview": "x", "ImageTags": {"Primary": "t"}},
        {"Id": "b2", "Name": ""}]})
    assert jellyfin_api.collections("http://jf", "K") == [
        {"id": "b1", "name": "Disney", "count": 12, "summary": "x", "has_poster": True}]


def test_members_carry_their_ids_and_kind(monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda url, key, path, timeout=120: {"Items": [
        {"Id": "m1", "Name": "Aladdin", "Type": "Movie", "ProductionYear": 1992,
         "ProviderIds": {"Tmdb": "812", "Imdb": "tt0103639", "Tvdb": ""}},
        {"Id": "s1", "Name": "Wonder Woman", "Type": "Series", "ProductionYear": 1975, "ProviderIds": {}},
        {"Id": "e1", "Name": "Some Episode", "Type": "Episode"}]})
    assert jellyfin_api.collection_members("http://jf", "K", "b1") == [
        {"title": "Aladdin", "kind": "movie", "year": 1992, "ids": {"tmdb": "812", "imdb": "tt0103639", "jellyfin": "m1"}},
        {"title": "Wonder Woman", "kind": "show", "year": 1975, "ids": {"jellyfin": "s1"}},
        {"title": "Some Episode", "kind": None, "year": None, "ids": {"jellyfin": "e1"}}]


def test_unreadable_jellyfin_gives_none(monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda *a, **k: None)
    assert jellyfin_api.collections("http://jf", "K") is None
    assert jellyfin_api.collection_members("http://jf", "K", "b1") is None


# ── the Collections step: list, poster, add ──────────────────────────────────────────

JF_CFG = {"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "K"}
JF_COLS = [{"id": "b1", "name": "Disney", "count": 3, "summary": "", "has_poster": True}]


def _plex_sections(pr, monkeypatch):
    def fake(url, token, path):
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [{"key": "1", "title": "Movies"}]}}
        return {"MediaContainer": {"Metadata": [{"title": "Marvel", "ratingKey": "9", "childCount": 4}]}}
    monkeypatch.setattr(pr, "_plex_get", fake)


def test_jellyfin_collections_are_listed(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: dict(JF_CFG))
    monkeypatch.setattr(jellyfin_api, "collections", lambda url, key: JF_COLS)
    assert pr.list_collections() == [{"id": "jellyfin:b1", "name": "Disney", "count": 3, "summary": "",
                                      "has_poster": True, "section": "Jellyfin", "server": "jellyfin"}]


def test_both_servers_collections_are_listed_and_labelled(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: {"plex_url": "http://p", "plex_token": "x", **JF_CFG})
    _plex_sections(pr, monkeypatch)
    monkeypatch.setattr(jellyfin_api, "collections", lambda url, key: JF_COLS)
    got = pr.list_collections()
    assert [(c["name"], c["section"], c["server"]) for c in got] == [
        ("Marvel", "Plex · Movies", "plex"), ("Disney", "Jellyfin", "jellyfin")]


def test_plex_only_listing_is_unchanged(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: {"plex_url": "http://p", "plex_token": "x"})
    _plex_sections(pr, monkeypatch)
    assert pr.list_collections()[0]["section"] == "Movies"


def test_unreadable_jellyfin_says_why(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: dict(JF_CFG))
    monkeypatch.setattr(jellyfin_api, "collections", lambda url, key: None)
    with pytest.raises(pr.HTTPException) as e:
        pr.list_collections()
    assert e.value.status_code == 502 and "Jellyfin" in e.value.detail


def test_no_server_at_all_is_a_clear_error(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: {"tunarr_url": "http://t"})
    with pytest.raises(pr.HTTPException) as e:
        pr.list_collections()
    assert e.value.status_code == 400


def test_a_jellyfin_poster_comes_from_jellyfin(pr, monkeypatch):
    monkeypatch.setattr(pr, "_load_config", lambda: dict(JF_CFG))
    monkeypatch.setattr(jellyfin_api, "image", lambda url, key, item_id: (b"img-" + item_id.encode(), "image/png"))
    r = pr.collection_poster("jellyfin:b1")
    assert r.body == b"img-b1" and r.media_type == "image/png"


def test_an_added_collection_remembers_its_server(pr):
    pr.apply_collections([pr.CollectionSelection(name="Disney", channel_number=80, include=True, server="jellyfin"),
                          pr.CollectionSelection(name="Marvel", channel_number=81, include=True)])
    import json
    data = json.loads((pr._test_data_dir / "channels.draft.json").read_text(encoding="utf-8"))
    assert [c["content"] for c in data["channels"]] == [[{"collection": "Disney", "server": "jellyfin"}],
                                                        [{"collection": "Marvel"}]]


def test_members_are_asked_for_recursively(monkeypatch):
    """Jellyfin answers 0 members for a collection unless asked recursively (or as a user) — the
    real "Fish" collection came back empty and its channel was skipped."""
    seen = []
    monkeypatch.setattr(jellyfin_api, "get", lambda url, key, path, timeout=120: seen.append(path) or {"Items": []})
    jellyfin_api.collection_members("http://jf", "K", "b1")
    assert "Recursive=true" in seen[0] and "IncludeItemTypes=Movie%2CSeries" in seen[0] and "ParentId=b1" in seen[0]


def test_a_missing_child_count_is_counted(monkeypatch):
    """Jellyfin leaves ChildCount out unless asked as a user, so the list showed 0 items."""
    def fake(url, key, path, timeout=120):
        if "IncludeItemTypes=BoxSet" in path:
            return {"Items": [{"Id": "b1", "Name": "Fish"}]}
        assert "ParentId=b1" in path and "Limit=0" in path
        return {"TotalRecordCount": 10, "Items": []}
    monkeypatch.setattr(jellyfin_api, "get", fake)
    assert jellyfin_api.collections("http://jf", "K")[0]["count"] == 10


# ── a collection must not hide its movies from the export ─────────────────────────────

def test_the_export_asks_jellyfin_not_to_fold_collections(monkeypatch):
    """By default Jellyfin lists a collection IN PLACE OF its members: once "Fish" existed, the
    export lost its 10 movies and reported "Fish" as a movie not in Tunarr."""
    seen = []
    monkeypatch.setattr(jellyfin_api, "get", lambda url, key, path, timeout=120: seen.append(path) or {"Items": [
        {"Name": "Antz", "Type": "Movie"}, {"Name": "Fish", "Type": "BoxSet"}]})
    got = jellyfin_api.items("http://jf", "K", "lib", "Movie")
    assert "CollapseBoxSetItems=false" in seen[0]
    assert [i["Name"] for i in got] == ["Antz"]


def test_a_listing_with_cast_and_crew_is_read_in_pages(monkeypatch):
    """Cast and crew for every movie in one answer took ~54 s; it's read in pages, a few at once."""
    seen = []
    def fake(url, key, path, timeout=120):
        seen.append(path)
        start = int(path.split("StartIndex=")[1].split("&")[0])
        return {"TotalRecordCount": 250, "Items": [{"Name": f"m{start + i}", "Type": "Movie"}
                                                   for i in range(min(100, 250 - start))]}
    monkeypatch.setattr(jellyfin_api, "get", fake)
    got = jellyfin_api.items("http://jf", "K", "lib", "Movie", fields="Genres,People")
    assert len(got) == 250 and len({g["Name"] for g in got}) == 250
    assert sorted(int(p.split("StartIndex=")[1].split("&")[0]) for p in seen) == [0, 100, 200]
    assert all("Limit=100" in p for p in seen)


def test_a_failed_page_means_unreadable(monkeypatch):
    def fake(url, key, path, timeout=120):
        return None if "StartIndex=100" in path else {"TotalRecordCount": 150, "Items": [{"Name": "a", "Type": "Movie"}]}
    monkeypatch.setattr(jellyfin_api, "get", fake)
    assert jellyfin_api.items("http://jf", "K", "lib", "Movie", fields="People") is None


def test_a_persons_exact_spelling_is_looked_up(monkeypatch):
    monkeypatch.setattr(jellyfin_api, "get", lambda url, key, path, timeout=120: {"Items": [
        {"Name": "Colin Hanks"}, {"Name": "Tom Hanks"}]})
    assert jellyfin_api.person_name("http://jf", "K", "tom hanks") == "Tom Hanks"
    assert jellyfin_api.person_name("http://jf", "K", "Nobody") == "Nobody"
