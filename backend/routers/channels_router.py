import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

# backend/ is on sys.path when running under uvicorn/the app; add it for direct
# imports (tests, type checkers) so channel_engine and scheduler resolve correctly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import channel_engine  # noqa: E402
import channel_tags    # noqa: E402
import notices         # noqa: E402
import scheduler       # noqa: E402  (shared deploy_lock)
import badge_renderer  # noqa: E402
import icon_engine     # noqa: E402

router = APIRouter()
DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent.parent))


def _path() -> Path:
    return DATA_DIR / "channels.json"


def _load_config() -> dict:
    try:
        with open(DATA_DIR / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def load() -> dict:
    try:
        with open(_path()) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"channels": data, "orphaned": [], "suggested_channels": []}
        return data
    except FileNotFoundError:
        return {"channels": [], "orphaned": [], "suggested_channels": []}
    except Exception as e:
        raise HTTPException(500, f"channels.json unreadable: {e}")


def save(data: dict):
    channel_tags.tag(data, _load_config())   # a new channel remembers its server
    with open(_path(), "w") as f:
        json.dump(data, f, indent=2)


@router.get("/channel-notices")
def channel_notices():
    """Per channel: what its last resolve skipped (and why) or found again."""
    return notices.load(DATA_DIR)


@router.get("/channels")
def get_channels():
    return load()


@router.put("/channels")
def replace_channels(data: dict):
    save(data)
    return {"ok": True}


@router.get("/channels/{number}")
def get_channel(number: int):
    for ch in load().get("channels", []):
        if ch.get("number") == number:
            return ch
    raise HTTPException(404, f"Channel {number} not found")


@router.put("/channels/{number}")
def update_channel(number: int, channel: dict):
    # A blank name can't reach Tunarr, so it would leave the record and Tunarr out of step.
    if not isinstance(channel.get("name"), str) or not channel["name"].strip():
        raise HTTPException(400, "A channel needs a name")
    data = load()
    for i, ch in enumerate(data.get("channels", [])):
        if ch.get("number") == number:
            # A channel keeps the server it was made for — an edit that doesn't say never resets it.
            if channel.get("server") not in channel_engine.SERVERS and ch.get("server") in channel_engine.SERVERS:
                channel["server"] = ch["server"]
            # A rename reaches Tunarr only on Apply — remember the name Tunarr has until then.
            channel_engine.note_rename(ch, channel)
            data["channels"][i] = channel
            save(data)
            return {"ok": True}
    raise HTTPException(404, f"Channel {number} not found")


@router.delete("/channels/{number}")
def delete_channel(number: int):
    data = load()
    before = len(data.get("channels", []))
    data["channels"] = [ch for ch in data.get("channels", []) if ch.get("number") != number]
    if len(data["channels"]) == before:
        raise HTTPException(404, f"Channel {number} not found")
    save(data)
    return {"ok": True}


@router.post("/channels/{number}/apply")
async def apply_channel(number: int):
    """Push ONE channel to Tunarr in place (preserves Tunarr id / Plex mapping).
    Edit-only: the channel must already exist in Tunarr — new channels go through
    the Planner deploy flow."""
    ch = next((c for c in load().get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    plex_url = cfg.get("plex_url", "").rstrip("/")
    plex_token = cfg.get("plex_token", "")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    def _do():
        # The library read the editor's check did when it opened (its server only) — not a second
        # full read of Tunarr. Something added to the library in the last few minutes joins at the
        # next refresh, the way a live rule's new titles do.
        movie_map, show_map, id_index = _cached_library(
            tunarr_url, servers={channel_engine.channel_server(ch, plex_url, plex_token)})

        plex_sections, collection_cache = [], {}
        if any(isinstance(it, dict) and "collection" in it for it in ch.get("content", [])):
            if plex_url and plex_token:
                plex_sections = channel_engine.get_plex_sections(plex_url, plex_token)

        report = {}
        resolved, _missing = channel_engine.resolve_content(
            ch.get("content", []), movie_map, show_map,
            plex_url=plex_url, plex_token=plex_token,
            plex_sections=plex_sections, collection_cache=collection_cache,
            franchise_index=channel_engine.load_franchise_index(DATA_DIR),
            id_index=id_index, report=report,
            server=channel_engine.channel_server(ch, plex_url, plex_token),
        )
        # Say what was skipped (or found again) instead of letting it vanish — recorded
        # before anything below can fail, so even a channel that resolves to nothing
        # explains itself.
        notice = notices.record(DATA_DIR, number, report)
        if report.get("blocked"):
            raise channel_engine.ChannelEngineError(
                "not updated, the channel was left as it is — " + "; ".join(report["blocked"]))
        if not resolved:
            raise channel_engine.ChannelEngineError(
                "resolved to empty — refusing to wipe the channel")

        if channel_engine.find_channel_by_number(tunarr_url, number) is None:
            raise channel_engine.ChannelEngineError(
                f"Channel #{number} not in Tunarr — create it in the Planner first")

        pad_ms, filler_ids = channel_engine.commercial_settings(ch.get("commercials"))
        outcome = {}
        channel_engine.update_channel_in_place(
            tunarr_url, number, ch.get("shuffle", "shuffle"), resolved, pad_ms=pad_ms,
            expected_name=channel_engine.guard_names(ch), playback=ch.get("playback"),
            filler_list_ids=filler_ids, restart=True, new_name=ch.get("name"), outcome=outcome)
        scheduler.record_applied(DATA_DIR, number, scheduler._program_ids(resolved))

        # Re-loaded fresh, and only that channel is touched:
        #  - `tunarr_name` (the names Tunarr might still have) is settled now that Tunarr's
        #    name is confirmed: dropped when it equals the saved name, or set to just that
        #    name when a Save renamed the channel again while this Apply ran;
        #  - an entry whose main number went stale but was found through a spare gets its
        #    current numbers written back, so the next resolve matches on the first number.
        in_tunarr = outcome.get("name")
        if report.get("healed") or "tunarr_name" in ch or in_tunarr:
            data = load()
            for saved in data.get("channels", []):
                if saved.get("number") == number:
                    changed = False
                    if in_tunarr:
                        before = saved.get("tunarr_name")
                        if (saved.get("name") or "").strip() == in_tunarr:
                            saved.pop("tunarr_name", None)
                        else:
                            saved["tunarr_name"] = [in_tunarr]
                        changed = saved.get("tunarr_name") != before
                    if report.get("healed"):
                        refreshed = channel_engine.apply_healed(saved.get("content", []), report["healed"])
                        if refreshed != saved.get("content"):
                            saved["content"] = refreshed
                            changed = True
                    if changed:
                        save(data)
                    break
        return len(resolved), notice

    try:
        async with scheduler.deploy_lock:
            count, notice = await asyncio.to_thread(_do)
        return {"ok": True, "number": number, "program_count": count, "notice": notice}
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(409, str(e))


@router.post("/channels/{number}/icon")
async def channel_icon(number: int, body: dict):
    """Set or pin a channel's Tunarr icon.

    body: {"mode": "badge" | "tmdb" | "custom" | "clear", "url": "..."(custom only)}
    badge/tmdb/custom write a pin into channels.json ("icon": {..., "pinned": true})
    so automatic art passes (fetch_images.py) skip the channel; "clear" resets the
    Tunarr icon to default and removes the pin (back to automatic).
    """
    mode = (body or {}).get("mode")
    if mode not in ("badge", "tmdb", "custom", "clear"):
        raise HTTPException(422, "mode must be one of: badge, tmdb, custom, clear")

    data = load()
    ch = next((c for c in data.get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")

    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    channel_engine.set_tunarr_auth_from_config(cfg)
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")

    name = (ch.get("name") or "").strip()

    def _do():
        summary = channel_engine.find_channel_by_number(tunarr_url, number)
        if summary is None:
            raise channel_engine.ChannelEngineError(
                f"Channel #{number} not in Tunarr — deploy it first")
        tch = icon_engine.get_full_channel(tunarr_url, summary["id"])
        if tch is None:
            raise channel_engine.ChannelEngineError("Could not read channel from Tunarr")

        if mode == "clear":
            if not icon_engine.clear_tunarr_channel_icon(tunarr_url, tch):
                raise channel_engine.ChannelEngineError("Tunarr icon reset failed")
            return ""

        if mode == "custom":
            url = (body.get("url") or "").strip()
            if not url:
                raise channel_engine.ChannelEngineError("url required for custom mode")
        elif mode == "badge":
            spec = icon_engine.load_spec_hints(
                DATA_DIR / "planner_state.json").get(name.lower(), {})
            png = badge_renderer.render_badge(
                name, kind=spec.get("kind"), genre=icon_engine.spec_genre(spec))
            url = icon_engine.upload_image_to_tunarr(
                tunarr_url, png, f"programmarr-ch{number}-{os.urandom(4).hex()}.png")
        else:  # tmdb
            key = cfg.get("tmdb_api_key", "")
            if not key:
                raise channel_engine.ChannelEngineError(
                    "tmdb_api_key not configured — use a badge or custom URL")
            spec = icon_engine.load_spec_hints(
                DATA_DIR / "planner_state.json").get(name.lower(), {})
            url = icon_engine.resolve_tmdb_logo(
                icon_engine.icon_attempts(ch, spec.get("kind")), key)
            if not url:
                raise channel_engine.ChannelEngineError(
                    "No verified TMDB logo for this channel — use a badge or custom URL")

        if not icon_engine.set_tunarr_channel_icon(tunarr_url, tch, url):
            raise channel_engine.ChannelEngineError("Tunarr icon update failed")
        return url

    try:
        async with scheduler.deploy_lock:
            url = await asyncio.to_thread(_do)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(409, str(e))

    if mode == "clear":
        ch.pop("icon", None)
    else:
        ch["icon"] = {"mode": mode, "url": url, "pinned": True}
    save(data)
    return {"ok": True, "mode": mode, "url": url}


# Reading Tunarr's library is heavy: Tunarr builds every program of every library into one
# answer (a big Plex TV library took ~50 s and ~2.5 GB of Tunarr's memory), and several at once
# ran Tunarr out of memory. So one read serves the Add box, the editor's check, the channel list's
# checks AND Save and Apply for a few minutes, and a read covers only the servers it's for.
_INDEX_TTL_SECONDS = 300
_index_cache = {}   # frozenset of servers -> {"at": monotonic time, "index": (movie_map, show_map, id_index)}
_index_lock = threading.Lock()


def _saved_channel_servers():
    cfg = _load_config()
    return channel_engine.channel_servers(load().get("channels", []), cfg.get("plex_url", "").rstrip("/"),
                                          cfg.get("plex_token", ""))


def _cached_library(tunarr_url, refresh=False, servers=None):
    """(movie_map, show_map, id_index) for `servers` (default: the servers the saved channels use),
    read at most once every few minutes. A fresh read that covers more servers serves too."""
    want = frozenset(servers or _saved_channel_servers() or ())
    with _index_lock:
        now = time.monotonic()
        if not refresh:
            for key, entry in _index_cache.items():
                if want <= key and now - entry["at"] <= _INDEX_TTL_SECONDS:
                    return entry["index"]
        index = channel_engine.build_library_index_with_ids(tunarr_url, set(want) or None)
        _index_cache[want] = {"at": time.monotonic(), "index": index}
        return index


# The rule pick-lists read Plex's whole listing; keep it a few minutes so opening the Add
# box repeatedly doesn't refetch it. (Applying a channel always reads Plex fresh.)
_VALUES_TTL_SECONDS = 300
_values_cache = {"at": 0.0, "plex": {}}
_values_lock = threading.Lock()


@router.get("/library/rule-values")
async def library_rule_values(rule: str, kind: str = "movie", refresh: bool = False, server: str = ""):
    """The choices for a live rule's pick-list — {"values": [{value, label, count}], "server"}:
    the genres / studios / directors / actors on the server the rule asks — Plex when set up,
    else Jellyfin (beta) — as that server spells them, with how many items have each, or the
    decades. 400 for an unknown rule; 502 when the server can't be read (or none is set up), so
    the screen can fall back to typing."""
    if rule not in channel_engine.RULE_KINDS:
        raise HTTPException(400, f"rule must be one of: {', '.join(channel_engine.RULE_KINDS)}")
    if kind not in ("movie", "show"):
        raise HTTPException(400, "kind must be 'movie' or 'show'")
    cfg = _load_config()
    channel_engine.set_tunarr_auth_from_config(cfg)   # also remembers the Jellyfin connection
    plex_url, plex_token = cfg.get("plex_url", "").rstrip("/"), cfg.get("plex_token", "")
    server = channel_engine.rule_server({"server": server} if server else {}, plex_url, plex_token)
    if server is None:
        raise HTTPException(502, "No Plex or Jellyfin server is set up")
    name = "Plex" if server == "plex" else "Jellyfin"

    def _do():
        with _values_lock:
            if refresh or time.monotonic() - _values_cache["at"] > _VALUES_TTL_SECONDS:
                _values_cache.update(at=time.monotonic(), plex={})
            return channel_engine.rule_values(rule, kind, plex_url, plex_token, [], _values_cache["plex"], server=server)

    values = await asyncio.to_thread(_do)
    if values is None:
        with _values_lock:
            _values_cache["at"] = 0.0   # don't keep a failed read around
        raise HTTPException(502, f"{name} could not be read")
    return {"values": values, "server": name}


@router.get("/library/lookup")
async def library_lookup(title: str, kind: str = "", refresh: bool = False, server: str = ""):
    """Every movie/show in the library with exactly this title, each with a ready-to-save
    channel entry that carries its own numbers ({"movie": .., "year": .., "ids": {..}}).
    One match: the Add box pins it. Several: it asks. None: it adds the text as typed.
    With `server` ("plex" / "jellyfin"), only the copies Tunarr plays from that server."""
    if kind not in ("", "movie", "show"):
        raise HTTPException(400, "kind must be 'movie' or 'show'")
    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")
    channel_engine.set_tunarr_auth_from_config(cfg)
    try:
        _, _, index = await asyncio.to_thread(
            _cached_library, tunarr_url, refresh, {server} if server in channel_engine.SERVERS else None)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))
    if server in channel_engine.SERVERS:
        _, _, index = channel_engine.library_for_server({}, {}, index, server)
    matches = []
    for item in channel_engine.find_by_title(title, index, kind or None):
        k = "movie" if item["type"] == "Movie" else "show"
        matches.append({"kind": k, "title": item["title"], "year": item.get("year"),
                        "entry": channel_engine.entry_for_item(item)})
    return {"matches": matches}


def _clean_review():
    return {"missing_count": 0, "missing": [], "healed_count": 0, "ambiguous_count": 0, "ambiguous": []}


def _review_setup():
    """The connection details a review needs, or a clear error."""
    cfg = _load_config()
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    if not tunarr_url:
        raise HTTPException(400, "Tunarr not configured")
    channel_engine.set_tunarr_auth_from_config(cfg)
    return tunarr_url, cfg.get("plex_url", "").rstrip("/"), cfg.get("plex_token", "")


def _review_channels(channels, tunarr_url, plex_url, plex_token, refresh=False, isolate=False):
    """{channel number: review} for these channels — each resolved against the library
    exactly as a deploy would, but writing nothing and never touching Tunarr's channels.
    One cached library scan and one Plex collection lookup serve them all. With `isolate`, a
    channel that can't be checked is left out instead of failing the rest."""
    movie_map, show_map, id_index = _cached_library(
        tunarr_url, refresh, servers=channel_engine.channel_servers(channels, plex_url, plex_token))
    uses_collection = any(isinstance(it, dict) and "collection" in it
                          for ch in channels for it in ch.get("content", []))
    plex_sections = (channel_engine.get_plex_sections(plex_url, plex_token)
                     if uses_collection and plex_url and plex_token else [])
    franchise_index = channel_engine.load_franchise_index(DATA_DIR)
    collection_cache, reviews = {}, {}
    for ch in channels:
        report = {}
        try:
            channel_engine.resolve_content(
                ch.get("content", []), movie_map, show_map, plex_url=plex_url, plex_token=plex_token,
                plex_sections=plex_sections, collection_cache=collection_cache,
                franchise_index=franchise_index, id_index=id_index, report=report,
                server=channel_engine.channel_server(ch, plex_url, plex_token))
        except Exception:
            if not isolate:
                raise
            continue  # a check is information: one odd channel must not blank the others
        reviews[ch.get("number")] = notices.summarize(report) or _clean_review()
    return reviews


@router.get("/channels/{number}/review")
async def channel_review(number: int, refresh: bool = False):
    """Check ONE channel against the library right now, without deploying anything: what
    it would skip (and why), what it would find again through a backup number, and every
    plain title that names more than one movie/show — with the choices. Nothing is written,
    and it never touches Tunarr's channels; the editor shows this when it opens."""
    ch = next((c for c in load().get("channels", []) if c.get("number") == number), None)
    if ch is None:
        raise HTTPException(404, f"Channel {number} not in channels.json")
    tunarr_url, plex_url, plex_token = _review_setup()
    try:
        reviews = await asyncio.to_thread(_review_channels, [ch], tunarr_url, plex_url, plex_token, refresh)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))
    return reviews[number]


@router.get("/channel-reviews")
async def channel_reviews(refresh: bool = False):
    """The same live check for EVERY channel, so the channel list can flag the ones that need
    a look without anyone opening them first (the saved notices only know what the last
    Apply or auto-update saw)."""
    channels = load().get("channels", [])
    if not channels:
        return {}
    tunarr_url, plex_url, plex_token = _review_setup()
    try:
        return await asyncio.to_thread(
            _review_channels, channels, tunarr_url, plex_url, plex_token, refresh, True)
    except channel_engine.ChannelEngineError as e:
        raise HTTPException(502, str(e))


@router.get("/library/titles")
def library_titles():
    csv = channel_engine.library_csv(DATA_DIR)
    if not csv.exists():
        return []
    titles = []
    try:
        with open(csv, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == 0:
                    continue
                parts = line.split(",", 1)
                if parts:
                    titles.append(parts[0].strip().strip('"'))
    except Exception:
        pass
    return titles
