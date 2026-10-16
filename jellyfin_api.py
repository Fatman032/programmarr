"""
jellyfin_api.py — the Jellyfin server calls Programmarr makes directly (BETA).

The Jellyfin counterpart of the Plex helpers: list the libraries, and read every movie / show
with the same details the Plex export reads (genres, studios, directors, cast, ratings,
countries). Authenticated with an API key (Jellyfin: Dashboard -> API Keys).

No dependencies beyond the Python standard library.
"""

import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
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


def post(base_url, api_key, path, body=None, timeout=30):
    """POST to a Jellyfin API path, with `body` sent as JSON (or no body). True if Jellyfin accepted
    it (failures are printed)."""
    hdrs = headers(api_key)
    data = b""
    if body is not None:
        data = json.dumps(body).encode()
        hdrs = {**hdrs, "Content-Type": "application/json"}
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method="POST", headers=hdrs)
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


# Cast and crew (People) is slow for Jellyfin to put together — ~1,600 movies took almost a
# minute in one answer — so a listing that asks for it is read in pages, a few at a time.
PEOPLE_PAGE = 100
PEOPLE_WORKERS = 4


def items(base_url, api_key, parent_id, item_type, fields=ITEM_FIELDS, extra=None):
    """Every item of `item_type` ("Movie", "Series", "Episode") in one library, or None if
    Jellyfin couldn't be read. `extra` adds query filters (e.g. one person's items).

    CollapseBoxSetItems=false: by default Jellyfin folds the members of a collection into the
    collection itself, so a movie list would hold "Fish" (a collection) instead of the 10 movies
    in it. Anything that still isn't `item_type` is left out."""
    params = {
        "ParentId": parent_id, "Recursive": "true", "IncludeItemTypes": item_type,
        "CollapseBoxSetItems": "false",
        "Fields": fields, "EnableImages": "false", "EnableUserData": "false", **(extra or {}),
    }
    if "People" not in fields:
        data = get(base_url, api_key, f"/Items?{urllib.parse.urlencode(params)}")
        if not isinstance(data, dict):
            return None
        found = data.get("Items") or []
    else:
        first = get(base_url, api_key, f"/Items?{urllib.parse.urlencode({**params, 'StartIndex': 0, 'Limit': PEOPLE_PAGE})}")
        if not isinstance(first, dict):
            return None
        total = int(first.get("TotalRecordCount") or 0)
        starts = range(PEOPLE_PAGE, total, PEOPLE_PAGE)
        with ThreadPoolExecutor(PEOPLE_WORKERS) as pool:
            pages = list(pool.map(lambda start: get(base_url, api_key, "/Items?" + urllib.parse.urlencode(
                {**params, "StartIndex": start, "Limit": PEOPLE_PAGE})), starts))
        if any(not isinstance(p, dict) for p in pages):
            return None
        found = [i for p in [first, *pages] for i in p.get("Items") or []]
    return [i for i in found if i.get("Type") in (None, item_type)]


def person_name(base_url, api_key, name):
    """Jellyfin's own spelling of a person's name ("tom hanks" -> "Tom Hanks"), since its
    person filter only matches the exact spelling. The name as given when there's no such person;
    None if Jellyfin couldn't be read."""
    query = urllib.parse.urlencode({"SearchTerm": name, "Limit": 50, "EnableImages": "false"})
    data = get(base_url, api_key, f"/Persons?{query}")
    if not isinstance(data, dict):
        return None
    want = name.strip().lower()
    return next((p["Name"] for p in data.get("Items") or [] if (p.get("Name") or "").strip().lower() == want), name)


def person_items(base_url, api_key, parent_id, item_type, name, role, fields=ITEM_FIELDS):
    """The items of `item_type` in one library that `name` is credited on as `role` ("Actor" /
    "Director"), with their cast and crew — fast, unlike every item's cast. None if unreadable."""
    return items(base_url, api_key, parent_id, item_type, fields=fields,
                 extra={"Person": name, "PersonTypes": role})


# What a collection member is, in Programmarr's words.
_MEMBER_KINDS = {"movie": "movie", "series": "show"}


def collections(base_url, api_key):
    """Jellyfin's collections (BoxSets) as [{"id", "name", "count", "summary", "has_poster"}],
    or None if Jellyfin couldn't be read."""
    query = urllib.parse.urlencode({
        "IncludeItemTypes": "BoxSet", "Recursive": "true", "Fields": "ChildCount,Overview",
        "EnableUserData": "false", "SortBy": "SortName",
    })
    data = get(base_url, api_key, f"/Items?{query}")
    if not isinstance(data, dict):
        return None
    out = []
    for c in data.get("Items") or []:
        if not c.get("Id") or not (c.get("Name") or "").strip():
            continue
        count = c.get("ChildCount")
        if count is None:
            # Jellyfin only fills ChildCount when asked as a user; count the members instead.
            got = get(base_url, api_key, f"/Items?{_member_query(c['Id'], Limit=0)}")
            count = (got or {}).get("TotalRecordCount") if isinstance(got, dict) else 0
        out.append({"id": c["Id"], "name": c["Name"].strip(), "count": int(count or 0),
                    "summary": c.get("Overview") or "", "has_poster": "Primary" in (c.get("ImageTags") or {})})
    return out


def _member_query(collection_id, **extra):
    """The movies and shows in a collection. Without Recursive Jellyfin answers 0 for a collection
    (its members live in the libraries, linked in) unless asked as a particular user."""
    return urllib.parse.urlencode({"ParentId": collection_id, "Recursive": "true",
                                   "IncludeItemTypes": "Movie,Series", "CollapseBoxSetItems": "false", **extra})


def collection_members(base_url, api_key, collection_id):
    """The movies and shows in one Jellyfin collection, as [{"title", "kind" ("movie" / "show" /
    None for anything else), "year", "ids": {"tmdb", "imdb", "jellyfin"}}], or None if Jellyfin
    couldn't be read. The ids are what Programmarr matches by (TMDB, then IMDB, then Jellyfin's own id)."""
    query = _member_query(collection_id, Fields="ProviderIds", EnableImages="false", EnableUserData="false")
    data = get(base_url, api_key, f"/Items?{query}")
    if not isinstance(data, dict):
        return None
    members = []
    for it in data.get("Items") or []:
        if not it.get("Name"):
            continue
        providers = {k.lower(): str(v) for k, v in (it.get("ProviderIds") or {}).items() if v}
        ids = {"tmdb": providers.get("tmdb"), "imdb": providers.get("imdb"), "jellyfin": it.get("Id")}
        members.append({"title": it["Name"], "kind": _MEMBER_KINDS.get((it.get("Type") or "").lower()),
                        "year": it.get("ProductionYear"), "ids": {k: v for k, v in ids.items() if v}})
    return members


def image(base_url, api_key, item_id, timeout=10):
    """(bytes, content type) of an item's poster, or None."""
    req = urllib.request.Request(f"{base_url.rstrip('/')}/Items/{urllib.parse.quote(item_id)}/Images/Primary",
                                 headers=headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "image/jpeg")
    except Exception:
        return None


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
