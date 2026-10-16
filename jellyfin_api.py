"""
jellyfin_api.py — the Jellyfin server calls Programmarr makes directly (BETA).

The Jellyfin counterpart of the Plex helpers: list the libraries, and read every movie / show
with the same details the Plex export reads (genres, studios, directors, cast, ratings,
countries). Authenticated with an API key (Jellyfin: Dashboard -> API Keys).

No dependencies beyond the Python standard library.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# What we ask Jellyfin to include for each movie / show — the details the CSV export needs.
ITEM_FIELDS = "Genres,Studios,People,ProductionLocations,OfficialRating,ProviderIds"

# Jellyfin's library kinds -> the two kinds Programmarr works with.
_LIBRARY_KINDS = {"movies": "movie", "tvshows": "show"}


def headers(api_key):
    return {"Authorization": f'MediaBrowser Token="{api_key}"', "X-Emby-Token": api_key,
            "Accept": "application/json"}


def get(base_url, api_key, path, timeout=120):
    """GET a Jellyfin API path; the decoded JSON, or None on any failure (which is printed)."""
    req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  ! Jellyfin HTTP {e.code} [{path[:60]}]")
        return None
    except Exception as e:
        print(f"  ! Jellyfin error [{path[:60]}]: {e}")
        return None


def post(base_url, api_key, path, timeout=30):
    """POST to a Jellyfin API path with no body. True if Jellyfin accepted it (failures are printed)."""
    req = urllib.request.Request(base_url.rstrip("/") + path, data=b"", method="POST", headers=headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        print(f"  ! Jellyfin HTTP {e.code} [{path[:60]}]")
        return False
    except Exception as e:
        print(f"  ! Jellyfin error [{path[:60]}]: {e}")
        return False


def libraries(base_url, api_key):
    """Jellyfin's libraries as [{"id", "name", "type": "movie" | "show" | None}], or None if
    Jellyfin couldn't be read. Music, books, home videos and the like come back with type None."""
    data = get(base_url, api_key, "/Library/VirtualFolders")
    if not isinstance(data, list):
        return None
    return [{"id": lib.get("ItemId"), "name": lib.get("Name", ""),
             "type": _LIBRARY_KINDS.get((lib.get("CollectionType") or "").lower())}
            for lib in data if lib.get("ItemId")]


def items(base_url, api_key, parent_id, item_type, fields=ITEM_FIELDS):
    """Every item of `item_type` ("Movie", "Series", "Episode") in one library, or None if
    Jellyfin couldn't be read."""
    query = urllib.parse.urlencode({
        "ParentId": parent_id, "Recursive": "true", "IncludeItemTypes": item_type,
        "Fields": fields, "EnableImages": "false", "EnableUserData": "false",
    })
    data = get(base_url, api_key, f"/Items?{query}")
    if not isinstance(data, dict):
        return None
    return data.get("Items") or []


def episode_counts(base_url, api_key, parent_id):
    """{series id: (seasons, episodes)} for one TV library, from a single listing of its
    episodes. A season is counted once however many episodes it has. {} if it couldn't be read."""
    episodes = items(base_url, api_key, parent_id, "Episode", fields="")
    counts = {}
    for ep in episodes or []:
        series = ep.get("SeriesId")
        if not series:
            continue
        seasons, total = counts.get(series, (set(), 0))
        seasons.add(ep.get("ParentIndexNumber") if ep.get("ParentIndexNumber") is not None else ep.get("SeasonId"))
        counts[series] = (seasons, total + 1)
    return {sid: (len(s), n) for sid, (s, n) in counts.items()}
