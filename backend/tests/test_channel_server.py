"""test_channel_server.py — one server per channel (beta).

A Tunarr with both Plex and Jellyfin holds the same movie twice. Each channel is tagged with the
ONE server its content plays from and only that server's copies are used, so a channel never
mixes the two. Every channel is tagged when it's made (even with one server set up), so someone
who adds a second server later still knows where each channel came from. Channels saved before the
tag existed (an upgrade) are all Plex — no earlier version could use Jellyfin. A fresh install has
no channels; each is tagged when made, with the server it's for (by itself, the one set up).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import channel_engine
import channel_tags
import jellyfin_api

PLEX_CFG = {"plex_url": "http://p", "plex_token": "x"}
JF_CFG = {"jellyfin_url": "http://jf", "jellyfin_api_key": "K"}


# ── which server an untagged channel belongs to ───────────────────────────────────────

@pytest.mark.parametrize("cfg, want", [
    (PLEX_CFG, "plex"), (JF_CFG, "jellyfin"), ({**PLEX_CFG, **JF_CFG}, "plex"), ({}, "plex"),
])
def test_the_default_server(cfg, want):
    assert channel_tags.default_for(cfg) == want


def test_tagging_leaves_a_tagged_channel_alone():
    data = {"channels": [{"number": 1}, {"number": 2, "server": "jellyfin"}, {"number": 3, "server": "bogus"}]}
    assert channel_tags.tag(data, PLEX_CFG) == 2
    assert [c["server"] for c in data["channels"]] == ["plex", "jellyfin", "plex"]


def test_a_draft_follows_its_build_channels_choice():
    """AI extras or collections added to a Jellyfin draft are Jellyfin channels, even with Plex set up."""
    data = {"server": "jellyfin", "channels": [{"number": 9}]}
    channel_tags.tag(data, {**PLEX_CFG, **JF_CFG})
    assert data["channels"][0]["server"] == "jellyfin"


@pytest.mark.parametrize("cfg", [PLEX_CFG, JF_CFG, {**PLEX_CFG, **JF_CFG}, {}])
def test_upgrading_tags_every_old_channel_plex(tmp_path, cfg):
    """Scenario 1: an upgrade from a version before tags. Whatever is set up now, those channels
    were made from Plex — no earlier version could use Jellyfin."""
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [{"number": 1, "name": "A"}]}))
    (tmp_path / "channels.draft.json").write_text(json.dumps({"channels": [{"number": 2, "server": "jellyfin"}]}))
    assert channel_tags.tag_files(tmp_path, cfg) == 1
    assert json.loads((tmp_path / "channels.json").read_text())["channels"][0] == {"number": 1, "name": "A", "server": "plex"}
    assert json.loads((tmp_path / "channels.draft.json").read_text())["channels"][0]["server"] == "jellyfin"
    assert channel_tags.tag_files(tmp_path, cfg) == 0   # once


def test_a_fresh_install_has_nothing_to_tag(tmp_path):
    """Scenario 2: no channels yet; each is tagged when it's made."""
    assert channel_tags.tag_files(tmp_path, JF_CFG) == 0
    assert not (tmp_path / "channels.json").exists()


def test_startup_tagging_survives_missing_or_broken_files(tmp_path):
    (tmp_path / "channels.json").write_text("{ not json")
    assert channel_tags.tag_files(tmp_path, PLEX_CFG) == 0


def test_someone_who_adds_jellyfin_later_keeps_their_plex_channels(tmp_path):
    """Scenario 3: tagged when they upgraded; adding Jellyfin later changes nothing about them."""
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [{"number": 1}]}))
    channel_tags.tag_files(tmp_path, PLEX_CFG)
    channel_tags.tag_files(tmp_path, {**PLEX_CFG, **JF_CFG})
    assert json.loads((tmp_path / "channels.json").read_text())["channels"][0]["server"] == "plex"


def test_a_channel_saved_from_the_channels_page_is_tagged(tmp_path, monkeypatch):
    from routers import channels_router
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(JF_CFG))
    channels_router.save({"channels": [{"number": 5, "name": "New", "content": []}]})
    assert json.loads((tmp_path / "channels.json").read_text())["channels"][0]["server"] == "jellyfin"


# ── a Tunarr holding both servers' copies ─────────────────────────────────────────────

def _prog(tid, title, year, source, sid, tmdb):
    return {"id": tid, "program": {"title": title, "year": year, "state": "ok", "sourceType": source,
                                   "mediaSourceId": sid, "identifiers": [{"type": "tmdb", "id": tmdb},
                                                                         {"type": source, "id": f"{source}-{tid}"}]}}


def _ep(tid, show, uuid, source, sid, tmdb):
    return {"id": tid, "program": {"title": f"{show} S1E1", "state": "ok", "show": {
        "uuid": uuid, "title": show, "year": 1982, "mediaSourceId": sid,
        "identifiers": [{"type": "tmdb", "id": tmdb}, {"type": source, "id": f"{source}-{uuid}"}]}}}


@pytest.fixture
def both(monkeypatch):
    """Tunarr with a Plex source and a Jellyfin source, each holding Aladdin and Cheers;
    Jellyfin also has Antz."""
    sources = [
        {"name": "Plex", "type": "plex", "libraries": [
            {"id": "pm", "mediaType": "movies", "enabled": True}, {"id": "pt", "mediaType": "shows", "enabled": True}]},
        {"name": "Jellyfin", "type": "jellyfin", "libraries": [
            {"id": "jm", "mediaType": "movies", "enabled": True}, {"id": "jt", "mediaType": "shows", "enabled": True}]},
    ]
    programs = {
        "pm": [_prog("p-aladdin", "Aladdin", 1992, "plex", "S1", "812")],
        "pt": [_ep("p-cheers-1", "Cheers", "p-cheers", "plex", "S1", "3918")],
        "jm": [_prog("j-aladdin", "Aladdin", 1992, "jellyfin", "S2", "812"),
               _prog("j-antz", "Antz", 1998, "jellyfin", "S2", "8916")],
        "jt": [_ep("j-cheers-1", "Cheers", "j-cheers", "jellyfin", "S2", "3918")],
    }
    monkeypatch.setattr(channel_engine, "get_library_sources", lambda url: sources)
    monkeypatch.setattr(channel_engine, "api", lambda url, method, path, body=None, timeout=60:
                        next(v for k, v in programs.items() if path.endswith(f"/{k}/programs")))
    return channel_engine.build_library_index_with_ids("http://t")


def _played(content, both, server):
    movie_map, show_map, idx = both
    resolved, _ = channel_engine.resolve_content(content, movie_map, show_map, collection_cache={},
                                                 id_index=idx, server=server)
    # A pinned/typed entry comes back as a library item; a plain title as Tunarr's own program.
    return [r.get("ids", {}).get("tunarr") or r.get("showId") or r["programs"][0]["id"] for r in resolved]


@pytest.mark.parametrize("server, want", [("plex", ["p-aladdin", "p-cheers"]), ("jellyfin", ["j-aladdin", "j-cheers"])])
def test_a_channel_plays_only_its_servers_copies(both, server, want):
    content = [{"movie": "Aladdin", "year": 1992, "ids": {"tmdb": "812"}}, {"show": "Cheers"}]
    assert _played(content, both, server) == want


def test_plain_titles_follow_the_channel_too(both):
    assert _played(["Aladdin"], both, "jellyfin") == ["j-aladdin"]
    assert _played(["Aladdin"], both, "plex") == ["p-aladdin"]


def test_something_only_the_other_server_has_is_not_borrowed(both):
    assert _played(["Antz"], both, "plex") == []
    assert _played(["Antz"], both, "jellyfin") == ["j-antz"]


def test_a_plex_channel_never_borrows_jellyfins_copies(monkeypatch):
    """Tunarr has only Jellyfin: a Plex channel finds nothing (the deploy then leaves it as it is)
    — it never plays another server's copies."""
    sources = [{"name": "Jellyfin", "type": "jellyfin", "libraries": [{"id": "jm", "mediaType": "movies", "enabled": True}]}]
    monkeypatch.setattr(channel_engine, "get_library_sources", lambda url: sources)
    monkeypatch.setattr(channel_engine, "api", lambda *a, **k: [_prog("j-antz", "Antz", 1998, "jellyfin", "S2", "8916")])
    movie_map, show_map, idx = channel_engine.build_library_index_with_ids("http://t")
    assert channel_engine.resolve_content(["Antz"], movie_map, show_map, id_index=idx, server="plex")[0] == []
    assert len(channel_engine.resolve_content(["Antz"], movie_map, show_map, id_index=idx, server="jellyfin")[0]) == 1


def test_a_channels_rules_ask_its_server(both, monkeypatch):
    """A rule with no server of its own asks the channel's server, even with Plex set up."""
    channel_engine.set_jellyfin("http://jf", "K")
    monkeypatch.setattr(jellyfin_api, "libraries", lambda u, k: [{"id": "L", "name": "Movies", "type": "movie"}])
    monkeypatch.setattr(jellyfin_api, "items", lambda *a, **k: [
        {"Id": "x", "Name": "Antz", "Type": "Movie", "ProductionYear": 1998, "Genres": ["Animation"],
         "ProviderIds": {"Tmdb": "8916"}}])
    monkeypatch.setattr(channel_engine, "_plex_listing", lambda *a: pytest.fail("Plex was asked"))
    movie_map, show_map, idx = both
    resolved, _ = channel_engine.resolve_content([{"rule": "genre", "value": "Animation"}], movie_map, show_map,
                                                 plex_url="http://p", plex_token="x", collection_cache={},
                                                 id_index=idx, server="jellyfin")
    assert [r["ids"]["tunarr"] for r in resolved] == ["j-antz"]


def test_the_channels_server(monkeypatch):
    assert channel_engine.channel_server({"server": "jellyfin"}, "http://p", "x") == "jellyfin"
    assert channel_engine.channel_server({}, "http://p", "x") == "plex"
    channel_engine.set_jellyfin("http://jf", "K")
    assert channel_engine.channel_server({}, "", "") == "jellyfin"


def test_a_one_server_view_never_hands_back_someone_elses_maps(monkeypatch):
    """The Add box's lookup asks with empty maps; a channel check on the same shared library
    must still get the real ones."""
    sources = [{"name": "Jellyfin", "type": "jellyfin", "libraries": [{"id": "jm", "mediaType": "movies", "enabled": True}]}]
    monkeypatch.setattr(channel_engine, "get_library_sources", lambda url: sources)
    monkeypatch.setattr(channel_engine, "api", lambda *a, **k: [_prog("j-antz", "Antz", 1998, "jellyfin", "S2", "8916")])
    movie_map, show_map, idx = channel_engine.build_library_index_with_ids("http://t")
    channel_engine.library_for_server({}, {}, idx, "jellyfin")
    assert channel_engine.library_for_server(movie_map, show_map, idx, "jellyfin")[0] is movie_map


def test_the_add_box_lookup_only_offers_the_channels_servers_copies(both, monkeypatch, tmp_path):
    import asyncio
    from routers import channels_router
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t"}))
    monkeypatch.setattr(channels_router, "_cached_library", lambda url, refresh=False, servers=None: both)
    got = asyncio.run(channels_router.library_lookup("Aladdin", server="jellyfin"))
    assert [m["entry"]["ids"]["tunarr"] for m in got["matches"]] == ["j-aladdin"]


# ── go easy on Tunarr: only the servers needed, one read at a time ────────────────────

def _counting(monkeypatch, sources, programs, delay=0.0):
    import threading, time
    state = type("S", (), {"paths": [], "now": 0, "most": 0})()
    lock = threading.Lock()
    monkeypatch.setattr(channel_engine, "get_library_sources", lambda url: sources)
    def api(url, method, path, body=None, timeout=60):
        with lock:
            state.now += 1
            state.most = max(state.most, state.now)
            state.paths.append(path)
        time.sleep(delay)
        with lock:
            state.now -= 1
        return next(v for k, v in programs.items() if path.endswith(f"/{k}/programs"))
    monkeypatch.setattr(channel_engine, "api", api)
    return state


BOTH_SOURCES = [
    {"name": "Plex", "type": "plex", "libraries": [{"id": "pm", "mediaType": "movies", "enabled": True}]},
    {"name": "Jellyfin", "type": "jellyfin", "libraries": [{"id": "jm", "mediaType": "movies", "enabled": True}]},
]
BOTH_PROGRAMS = {"pm": [_prog("p-aladdin", "Aladdin", 1992, "plex", "S1", "812")],
                 "jm": [_prog("j-aladdin", "Aladdin", 1992, "jellyfin", "S2", "812")]}


def test_a_plex_channel_reads_only_tunarrs_plex_libraries(monkeypatch):
    """Reading both servers' libraries for every save doubled the work and, with several reads at
    once, ran the test Tunarr out of memory."""
    state = _counting(monkeypatch, BOTH_SOURCES, BOTH_PROGRAMS)
    channel_engine.build_library_index_with_ids("http://t", "plex")
    assert state.paths == ["/api/media-libraries/pm/programs"]


def test_a_read_for_both_kinds_of_channel_reads_both(monkeypatch):
    state = _counting(monkeypatch, BOTH_SOURCES, BOTH_PROGRAMS)
    channel_engine.build_library_index_with_ids("http://t", {"plex", "jellyfin"})
    assert len(state.paths) == 2


def test_reading_for_a_server_tunarr_doesnt_have_says_so(monkeypatch):
    """A Plex channel on a Tunarr with no Plex: a clear error (add Plex to Tunarr), and Jellyfin's
    libraries aren't read in its place."""
    state = _counting(monkeypatch, BOTH_SOURCES[1:], BOTH_PROGRAMS)
    with pytest.raises(channel_engine.ChannelEngineError) as e:
        channel_engine.build_library_index_with_ids("http://t", "plex")
    assert "Tunarr has no Plex library" in str(e.value) and state.paths == []


def test_only_one_library_read_runs_at_a_time(monkeypatch, tmp_path):
    import threading
    monkeypatch.setenv("PROGRAMMARR_DATA", str(tmp_path))
    state = _counting(monkeypatch, BOTH_SOURCES, BOTH_PROGRAMS, delay=0.05)
    threads = [threading.Thread(target=channel_engine.build_library_index_with_ids, args=("http://t",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(state.paths) == 8 and state.most == 1


def test_which_servers_a_set_of_channels_needs():
    assert channel_engine.channel_servers([{"server": "plex"}, {"server": "jellyfin"}, {}], "http://p", "x") == {"plex", "jellyfin"}


def test_one_read_serves_the_editors_check_and_save_and_apply(monkeypatch, tmp_path):
    """Opening a channel read the whole library, then Save and Apply read it all over again —
    two ~50 s, multi-GB answers from Tunarr per edit. Now the save reuses the editor's read."""
    from routers import channels_router
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t", **PLEX_CFG}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [{"number": 1, "server": "plex"}]}))
    reads = []
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids",
                        lambda url, servers=None: reads.append(servers) or ({}, {}, {"by_id": {}, "all": {}}))
    channels_router._cached_library("http://t", servers={"plex"})      # the editor's check
    channels_router._cached_library("http://t", servers={"plex"})      # Save and Apply
    assert reads == [{"plex"}]


def test_a_read_of_both_servers_serves_a_one_server_request(monkeypatch, tmp_path):
    from routers import channels_router
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    reads = []
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids",
                        lambda url, servers=None: reads.append(servers) or ({}, {}, {}))
    channels_router._cached_library("http://t", servers={"plex", "jellyfin"})
    channels_router._cached_library("http://t", servers={"jellyfin"})
    assert len(reads) == 1


def test_the_channel_lists_check_reads_only_the_servers_the_channels_use(monkeypatch, tmp_path):
    from routers import channels_router
    monkeypatch.setattr(channels_router, "DATA_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"tunarr_url": "http://t", **PLEX_CFG, **JF_CFG}))
    (tmp_path / "channels.json").write_text(json.dumps({"channels": [{"number": 1, "server": "plex"}]}))
    reads = []
    monkeypatch.setattr(channel_engine, "build_library_index_with_ids",
                        lambda url, servers=None: reads.append(servers) or ({}, {}, {}))
    channels_router._cached_library("http://t")
    assert reads == [{"plex"}]


# ── one server per run: a run never touches the other server's channels ──────────────

@pytest.fixture
def run_both(pr, tmp_path):
    """Both servers set up; a Plex channel deployed; the Plex Planner remembers it."""
    d = pr._test_data_dir
    (d / "config.json").write_text(json.dumps({"tunarr_url": "http://t", **PLEX_CFG, **JF_CFG}))
    (d / "channels.json").write_text(json.dumps({"channels": [
        {"number": 1, "name": "2020s Comedy", "server": "plex", "content": ["A"]},
        {"number": 2, "name": "Old Plex Pick", "server": "plex", "content": ["B"]}]}))
    (d / "planner_state.json").write_text(json.dumps({"managed_names": ["2020s Comedy", "Old Plex Pick"]}))
    return pr


def _draft(pr, server, channels):
    (pr._test_data_dir / "channels.draft.json").write_text(json.dumps({"server": server, "channels": channels}))


def test_a_jellyfin_run_never_updates_or_deletes_plex_channels(run_both):
    """A restored "2020s Comedy" pick in a Jellyfin run used to update the PLEX channel of that
    name into a Jellyfin one; and Plex channels missing from the run looked like removals."""
    _draft(run_both, "jellyfin", [{"number": 10, "name": "2020s Comedy", "server": "jellyfin", "content": ["X"]}])
    got = run_both.deploy_preview(run_both.DeployPreviewRequest(mode="edit"))
    assert [c["name"] for c in got["create"]] == ["2020s Comedy"]
    assert got["update"] == [] and got["delete"] == []
    assert sorted(c["name"] for c in got["other_server"]) == ["2020s Comedy", "Old Plex Pick"]


def test_a_plex_run_still_updates_and_removes_its_own_channels(run_both):
    _draft(run_both, "plex", [{"number": 1, "name": "2020s Comedy", "server": "plex", "content": ["changed"]}])
    got = run_both.deploy_preview(run_both.DeployPreviewRequest(mode="edit"))
    assert [c["name"] for c in got["update"]] == ["2020s Comedy"]
    assert [c["name"] for c in got["delete"]] == ["Old Plex Pick"]
    assert got["other_server"] == []


def test_a_jellyfin_full_rebuild_keeps_every_plex_channel(run_both, monkeypatch):
    """A full rebuild deletes every channel not ticked to keep — never the other server's."""
    seen = {}
    async def fake_deploy(args, protected, server=None):
        seen.update(args=args, protected=protected, server=server)
        yield ""
    monkeypatch.setattr(run_both, "_deploy_and_reconcile", fake_deploy)
    _draft(run_both, "jellyfin", [{"number": 10, "name": "Fish", "server": "jellyfin", "content": ["X"]}])
    import asyncio
    resp = asyncio.run(run_both.run_deploy_selective(run_both.DeployRequest(
        selections=[run_both.DeploySelection(original_number=10, deploy_number=10, include=True)])))
    async def drain():
        async for _ in resp.body_iterator:
            pass
    asyncio.run(drain())
    assert seen["protected"] == [1, 2] and "--protect" in seen["args"] and seen["server"] == "jellyfin"


def test_the_planner_remembers_picks_per_server(run_both):
    run_both.save_planner_state({"selected": {"g:Comedy": True}}, server="jellyfin")
    assert run_both.get_planner_state(server="jellyfin") == {"selected": {"g:Comedy": True}}
    assert run_both.get_planner_state(server="plex") == {"managed_names": ["2020s Comedy", "Old Plex Pick"]}
    assert (run_both._test_data_dir / "planner_state.jellyfin.json").exists()
    run_both.delete_planner_state(server="jellyfin")
    assert run_both.get_planner_state(server="jellyfin") == {}
    assert run_both.get_planner_state(server="plex") != {}


def test_a_drafts_server_comes_from_its_channels_when_not_stated(run_both):
    assert run_both._run_server({"channels": [{"server": "jellyfin"}]}) == "jellyfin"
    assert run_both._run_server({"server": "plex", "channels": [{"server": "jellyfin"}]}) == "plex"
    assert run_both._run_server({"channels": []}) == "plex"
