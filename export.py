#!/usr/bin/env python3
"""
export.py — Export your Plex or Jellyfin (beta) library to CSV for channel generation.

Fetches full metadata from your media server (genres, ratings, directors, episode counts),
cross-references against Tunarr to keep only synced content, and writes
plex_library.csv ready to paste into any LLM. (The file keeps its old name for either server.)

Usage:
    python export.py                    # writes plex_library.csv
    python export.py --out myfile.csv   # custom output path
    python export.py --no-crossref      # skip Tunarr sync check
"""

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request

import channel_engine
import jellyfin_api

CONFIG_FILE = "config.json"
OUTPUT_FILE = "plex_library.csv"


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {CONFIG_FILE} not found.")
        print("Create it with: tunarr_url and plex_servers (or plex_url + plex_token, "
              "or jellyfin_url + jellyfin_api_key with media_server \"jellyfin\")")
        sys.exit(1)
    if not cfg.get("tunarr_url"):
        print(f"ERROR: 'tunarr_url' missing from {CONFIG_FILE}")
        sys.exit(1)
    if cfg.get("media_server") == "jellyfin":
        if not (cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key")):
            print("ERROR: Jellyfin connection missing — set the Jellyfin URL and API key "
                  f"(Settings -> Connections, or jellyfin_url + jellyfin_api_key in {CONFIG_FILE})")
            sys.exit(1)
        return cfg
    has_plex = cfg.get("plex_servers") or (cfg.get("plex_url") and cfg.get("plex_token"))
    if not has_plex:
        print(f"ERROR: Plex connection missing — set plex_servers or plex_url + plex_token in {CONFIG_FILE}")
        sys.exit(1)
    return cfg


def _get_plex_servers(cfg):
    """Return list of (name, url, token) for all configured Plex servers."""
    servers = cfg.get("plex_servers") or []
    if servers:
        return [
            (s.get("name", "Plex"), s["url"].rstrip("/"), s["token"])
            for s in servers
            if s.get("url") and s.get("token")
        ]
    return [("Plex", cfg["plex_url"].rstrip("/"), cfg["plex_token"])]


def _media_server(cfg):
    return "jellyfin" if cfg.get("media_server") == "jellyfin" else "plex"


def _get_servers(cfg):
    """(name, url, key) for the configured media server(s): the Plex servers + tokens, or
    the Jellyfin server + API key."""
    if _media_server(cfg) == "jellyfin":
        return [("Jellyfin", cfg["jellyfin_url"].rstrip("/"), cfg["jellyfin_api_key"])]
    return _get_plex_servers(cfg)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def plex_get(base_url, token, path, timeout=60):
    sep = "&" if "?" in path else "?"
    url = base_url + path + sep + f"X-Plex-Token={token}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  ! Plex HTTP {e.code} [{path[:60]}]")
        return None
    except Exception as e:
        print(f"  ! Plex error [{path[:60]}]: {e}")
        return None


def tunarr_get(base_url, path, timeout=60):
    req = urllib.request.Request(base_url + path,
                                 headers=channel_engine.tunarr_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! Tunarr error [{path}]: {e}")
        return None


# ── Plex fetchers ──────────────────────────────────────────────────────────────

def get_plex_sections(plex_url, token):
    data = plex_get(plex_url, token, "/library/sections")
    if not data:
        return []
    return data["MediaContainer"].get("Directory", [])


def fetch_plex_movies(plex_url, token, section_key):
    print(f"  Fetching Plex movies (section {section_key})...")
    data = plex_get(plex_url, token, f"/library/sections/{section_key}/all?type=1", timeout=120)
    if not data:
        return []
    items = data["MediaContainer"].get("Metadata", [])
    print(f"  Found {len(items)} movies in Plex")
    return items


def fetch_plex_shows(plex_url, token, section_key):
    print(f"  Fetching Plex TV shows (section {section_key})...")
    data = plex_get(plex_url, token, f"/library/sections/{section_key}/all?type=2", timeout=120)
    if not data:
        return []
    items = data["MediaContainer"].get("Metadata", [])
    print(f"  Found {len(items)} shows in Plex")
    return items


# ── Tunarr cross-reference ─────────────────────────────────────────────────────

def build_tunarr_title_sets(tunarr_url):
    print("  Fetching Tunarr library for cross-reference...")
    sources = tunarr_get(tunarr_url, "/api/media-sources") or []
    plex_sources = [s for s in sources if s.get("type") in channel_engine.LIBRARY_SOURCE_TYPES]
    if not plex_sources:
        print("  WARNING: No Plex or Jellyfin source in Tunarr — skipping cross-reference")
        return None, None

    movie_titles: set = set()
    tv_titles: set    = set()

    for plex_source in plex_sources:
        libs = plex_source.get("libraries", [])
        movie_libs = [l for l in libs if l.get("mediaType") in ("movie", "movies") and l.get("enabled")]
        tv_libs    = [l for l in libs if l.get("mediaType") == "shows" and l.get("enabled")]

        for lib in movie_libs:
            programs = tunarr_get(tunarr_url, f"/api/media-libraries/{lib['id']}/programs", timeout=120) or []
            titles   = {p.get("program", {}).get("title", "").lower().strip() for p in programs}
            movie_titles |= titles
            print(f"  Tunarr has {len(titles)} movies ({lib.get('name', lib['id'])})")

        for lib in tv_libs:
            programs = tunarr_get(tunarr_url, f"/api/media-libraries/{lib['id']}/programs", timeout=120) or []
            titles   = {p.get("program", {}).get("show", {}).get("title", "").lower().strip()
                        for p in programs if p.get("program", {}).get("show")}
            tv_titles |= titles
            print(f"  Tunarr has {len(titles)} TV shows ({lib.get('name', lib['id'])})")

    if movie_titles:
        print(f"  Tunarr movie total (all sources): {len(movie_titles)}")
    if tv_titles:
        print(f"  Tunarr TV total (all sources): {len(tv_titles)}")

    return movie_titles, tv_titles


# ── Tunarr-sourced row builders ────────────────────────────────────────────────
# Used to supplement the primary Plex export with content from other Tunarr sources.
# Tunarr has all sources already indexed; these builders produce equivalent CSV rows
# from Tunarr program data. Country/Mood/Style are Plex-specific and left empty here.

def _tunarr_tags(obj, key):
    return "|".join(t.get("name", "") for t in obj.get(key, []) if t.get("name"))


def tunarr_movie_to_row(prog, source_name):
    return {
        "Title":   prog.get("title", ""),
        "Year":    prog.get("year", ""),
        "Type":    "Movie",
        "Rating":  prog.get("rating", ""),
        "Genres":  _tunarr_tags(prog, "genres"),
        "Director": _tunarr_tags(prog, "directors"),
        "Studio":  _tunarr_tags(prog, "studios"),
        "Actors":  "|".join(a.get("name", "") for a in prog.get("actors", [])[:LEAD_CAST] if a.get("name")),
        "Seasons": "", "Episodes": "",
        "Country": "", "Mood": "", "Style": "",
        "Source":  source_name,
    }


def tunarr_show_to_row(show_title, episodes, source_name):
    first = episodes[0] if episodes else {}
    show  = first.get("show", {})
    seasons = len({ep.get("season", {}).get("index") for ep in episodes if ep.get("season", {}).get("index") is not None})
    # Prefer show-level metadata if present; fall back to first episode's own fields.
    meta = show if show.get("genres") or show.get("studios") else first
    return {
        "Title":   show_title,
        "Year":    show.get("year", ""),
        "Type":    "TV",
        "Rating":  first.get("rating", ""),
        "Genres":  _tunarr_tags(meta, "genres"),
        "Director": "",
        "Studio":  _tunarr_tags(meta, "studios"),
        "Actors":  "|".join(a.get("name", "") for a in meta.get("actors", [])[:LEAD_CAST] if a.get("name")),
        "Seasons": str(seasons) if seasons else "",
        "Episodes": str(len(episodes)),
        "Country": "", "Mood": "", "Style": "",
        "Source":  source_name,
    }


def build_extra_rows_from_tunarr(tunarr_url, seen_movie_titles, seen_show_titles,
                                  allowed_movie_libs=None, allowed_tv_libs=None):
    """Fetch content from all Tunarr sources, skipping titles already in the primary export.

    allowed_movie_libs / allowed_tv_libs: set of Tunarr library UUIDs to include.
    None = include all (CLI default). Empty set = skip entirely.
    """
    print("  Fetching additional content from other Tunarr sources...")
    sources = tunarr_get(tunarr_url, "/api/media-sources") or []
    if not sources:
        return [], []

    extra_movie_rows = []
    extra_show_rows  = []

    for source in sources:
        source_name = source.get("name", source.get("type", "Tunarr"))
        libs = source.get("libraries", [])
        movie_libs = [l for l in libs if l.get("mediaType") in ("movie", "movies") and l.get("enabled")]
        show_libs  = [l for l in libs if l.get("mediaType") == "shows" and l.get("enabled")]

        for lib in movie_libs:
            if allowed_movie_libs is not None and lib["id"] not in allowed_movie_libs:
                continue
            programs = tunarr_get(tunarr_url, f"/api/media-libraries/{lib['id']}/programs", timeout=120) or []
            for p in programs:
                prog = p.get("program", {})
                title = prog.get("title", "")
                if not title or title.lower().strip() in seen_movie_titles:
                    continue
                seen_movie_titles.add(title.lower().strip())
                extra_movie_rows.append(tunarr_movie_to_row(prog, source_name))

        for lib in show_libs:
            if allowed_tv_libs is not None and lib["id"] not in allowed_tv_libs:
                continue
            programs = tunarr_get(tunarr_url, f"/api/media-libraries/{lib['id']}/programs", timeout=120) or []
            shows: dict = {}
            for p in programs:
                prog = p.get("program", {})
                show = prog.get("show", {})
                show_title = show.get("title", "")
                if not show_title or show_title.lower().strip() in seen_show_titles:
                    continue
                key = show_title.lower().strip()
                shows.setdefault(key, {"title": show_title, "episodes": [], "source": source_name})
                shows[key]["episodes"].append(prog)
            for key, s in shows.items():
                seen_show_titles.add(key)
                extra_show_rows.append(tunarr_show_to_row(s["title"], s["episodes"], s["source"]))

    new_movies = len(extra_movie_rows)
    new_shows  = len(extra_show_rows)
    if new_movies or new_shows:
        print(f"  Found {new_movies} additional movies, {new_shows} additional TV shows from Tunarr")
    else:
        print("  No additional content (your main library covers everything)")

    return extra_movie_rows, extra_show_rows


# ── Row builders ───────────────────────────────────────────────────────────────

# Top-N billed cast kept per title — enough for actor channels without the noise
# of full cast lists. Plex Role elements come back in billing order.
LEAD_CAST = 3


def _lead_actors(item):
    return "|".join(r["tag"] for r in item.get("Role", [])[:LEAD_CAST] if r.get("tag"))


def _join_tags(item, tag_name):
    """Return a pipe-joined string of tag values for a given tag array key.

    Plex returns tag arrays as lists of dicts with a "tag" key, e.g.:
        [{"tag": "France"}, {"tag": "Japan"}]
    Returns an empty string when the key is absent or the list is empty.
    Works for Genre, Country, Mood, Style, and any other tag array.
    """
    return "|".join(t["tag"] for t in item.get(tag_name, []) if t.get("tag"))


def movie_to_row(item):
    return {
        "Title": item.get("title", ""),
        "Year": item.get("year", ""),
        "Type": "Movie",
        "Rating": item.get("contentRating", ""),
        "Genres": _join_tags(item, "Genre"),
        "Director": _join_tags(item, "Director"),
        "Studio": item.get("studio", ""),
        "Actors": _lead_actors(item),
        "Seasons": "",
        "Episodes": "",
        "Country": _join_tags(item, "Country"),
        "Mood": _join_tags(item, "Mood"),
        "Style": _join_tags(item, "Style"),
    }


def show_to_row(item):
    return {
        "Title": item.get("title", ""),
        "Year": item.get("year", ""),
        "Type": "TV",
        "Rating": item.get("contentRating", ""),
        "Genres": _join_tags(item, "Genre"),
        "Director": "",
        "Studio": item.get("studio", ""),
        "Actors": _lead_actors(item),
        "Seasons": item.get("childCount", ""),
        "Episodes": item.get("leafCount", ""),
        "Country": _join_tags(item, "Country"),
        "Mood": _join_tags(item, "Mood"),
        "Style": _join_tags(item, "Style"),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def _jf_names(values):
    """'A|B' from Jellyfin's lists of names (plain strings, or {"Name": ..} objects)."""
    out = []
    for v in values or []:
        name = v.get("Name") if isinstance(v, dict) else v
        if name:
            out.append(str(name))
    return "|".join(out)


def _jf_people(item, role, limit=None):
    names = [p.get("Name") for p in item.get("People") or [] if p.get("Type") == role and p.get("Name")]
    return "|".join(names[:limit] if limit else names)


def jellyfin_movie_to_row(item):
    return {
        "Title": item.get("Name", ""),
        "Year": item.get("ProductionYear", ""),
        "Type": "Movie",
        "Rating": item.get("OfficialRating", ""),
        "Genres": _jf_names(item.get("Genres")),
        "Director": _jf_people(item, "Director"),
        "Studio": _jf_names(item.get("Studios")),
        "Actors": _jf_people(item, "Actor", LEAD_CAST),
        "Seasons": "",
        "Episodes": "",
        "Country": _jf_names(item.get("ProductionLocations")),
        "Mood": "",    # Plex-only tags; Jellyfin has none
        "Style": "",
    }


def jellyfin_show_to_row(item, counts):
    seasons, episodes = counts.get(item.get("Id"), (0, 0))
    return {
        "Title": item.get("Name", ""),
        "Year": item.get("ProductionYear", ""),
        "Type": "TV",
        "Rating": item.get("OfficialRating", ""),
        "Genres": _jf_names(item.get("Genres")),
        "Director": "",
        "Studio": _jf_names(item.get("Studios")),
        "Actors": _jf_people(item, "Actor", LEAD_CAST),
        "Seasons": seasons or "",
        "Episodes": episodes or "",
        "Country": _jf_names(item.get("ProductionLocations")),
        "Mood": "",
        "Style": "",
    }


# ── One media server or the other: the same four steps either way ───────────────────

def _sections(kind, url, key):
    """The server's movie and TV libraries as [{key, title, type: "movie" | "show"}]."""
    if kind == "jellyfin":
        return [{"key": lib["id"], "title": lib["name"], "type": lib["type"]}
                for lib in (jellyfin_api.libraries(url, key) or []) if lib["type"]]
    return [{"key": s.get("key"), "title": s.get("title", ""), "type": s.get("type")}
            for s in get_plex_sections(url, key) if s.get("type") in ("movie", "show")]


def _chosen(sections, kind, picked, srv_idx):
    """The libraries of one kind to export: all of them, or the ones picked in the UI.
    Accepts both "{server_index}:{key}" (multi-server UI) and a bare "{key}" (CLI)."""
    wanted = [s for s in sections if s["type"] == kind]
    if picked is None:
        return wanted
    keys = {k.strip() for k in picked.split(",") if k.strip()}
    return [s for s in wanted if f"{srv_idx}:{s['key']}" in keys or s["key"] in keys]


def _movie_rows(kind, url, key, section_key):
    if kind == "jellyfin":
        found = jellyfin_api.items(url, key, section_key, "Movie") or []
        print(f"  Found {len(found)} movies in Jellyfin")
        return [jellyfin_movie_to_row(i) for i in found]
    return [movie_to_row(i) for i in fetch_plex_movies(url, key, section_key)]


def _show_rows(kind, url, key, section_key):
    if kind == "jellyfin":
        found = jellyfin_api.items(url, key, section_key, "Series") or []
        print(f"  Found {len(found)} TV shows in Jellyfin")
        counts = jellyfin_api.episode_counts(url, key, section_key) if found else {}
        return [jellyfin_show_to_row(i, counts) for i in found]
    return [show_to_row(i) for i in fetch_plex_shows(url, key, section_key)]


def main():
    parser = argparse.ArgumentParser(description="Export your Plex or Jellyfin (beta) library to CSV")
    parser.add_argument("--out", default=OUTPUT_FILE, help="Output CSV path")
    parser.add_argument("--no-crossref", action="store_true", help="Skip Tunarr sync check")
    parser.add_argument("--movie-sections", default=None,
                        help="Comma-separated movie library keys — Plex section keys or Jellyfin library ids "
                             "(auto-detect if omitted, empty = skip).")
    parser.add_argument("--tv-sections", default=None,
                        help="Comma-separated TV library keys — Plex section keys or Jellyfin library ids "
                             "(auto-detect if omitted, empty = skip).")
    parser.add_argument("--tunarr-movie-libs", default=None,
                        help="Comma-separated Tunarr library UUIDs for extra movie sources (auto = all, empty = skip).")
    parser.add_argument("--tunarr-tv-libs", default=None,
                        help="Comma-separated Tunarr library UUIDs for extra TV sources (auto = all, empty = skip).")
    args = parser.parse_args()

    cfg = load_config()
    tunarr_url = cfg["tunarr_url"].rstrip("/")
    kind = _media_server(cfg)
    label = "Jellyfin" if kind == "jellyfin" else "Plex"
    server_list = _get_servers(cfg)
    multi = len(server_list) > 1

    # ── Discover and fetch from the media server(s) ────────────────────────────
    print(f"\n[1/4] Discovering {label} libraries ({len(server_list)} server(s))...")

    primary_movie_rows: list = []
    primary_show_rows: list  = []
    seen_movie_titles: set = set()
    seen_show_titles: set  = set()

    for srv_idx, (srv_name, srv_url, srv_key) in enumerate(server_list):
        print(f"\n  Server: {srv_name} ({srv_url})")
        sections = _sections(kind, srv_url, srv_key)
        if not sections:
            print(f"  WARNING: Could not reach {srv_name} or no libraries found — skipping")
            continue

        movie_sections = _chosen(sections, "movie", args.movie_sections, srv_idx)
        tv_sections = _chosen(sections, "show", args.tv_sections, srv_idx)
        for s in movie_sections:
            print(f"  Movie library: [{s['key']}] {s['title']}")
        for s in tv_sections:
            print(f"  TV library:    [{s['key']}] {s['title']}")

        # ── Fetch content ──────────────────────────────────────────────────────
        print(f"\n[2/4] Fetching content from {srv_name}...")
        for sec in movie_sections:
            for row in _movie_rows(kind, srv_url, srv_key, sec["key"]):
                t = str(row["Title"]).lower().strip()
                if t not in seen_movie_titles:
                    seen_movie_titles.add(t)
                    row["Source"] = srv_name if multi else ""
                    primary_movie_rows.append(row)

        for sec in tv_sections:
            for row in _show_rows(kind, srv_url, srv_key, sec["key"]):
                t = str(row["Title"]).lower().strip()
                if t not in seen_show_titles:
                    seen_show_titles.add(t)
                    row["Source"] = srv_name if multi else ""
                    primary_show_rows.append(row)

    if not primary_movie_rows and not primary_show_rows:
        print(f"ERROR: No content fetched from any {label} server")
        sys.exit(1)

    # ── Supplement from other Tunarr sources ───────────────────────────────────
    # Pull content that Tunarr knows about but isn't in the primary Plex export.
    # allowed_*_libs: None = all (auto/CLI), set = user-selected, empty set = skip.
    def _parse_lib_ids(raw):
        if raw is None:
            return None
        ids = {s.strip() for s in raw.split(",") if s.strip()}
        return ids  # empty set means "user selected nothing" → skip all

    allowed_movie_libs = _parse_lib_ids(args.tunarr_movie_libs)
    allowed_tv_libs    = _parse_lib_ids(args.tunarr_tv_libs)

    extra_movie_rows, extra_show_rows = build_extra_rows_from_tunarr(
        tunarr_url, seen_movie_titles, seen_show_titles,
        allowed_movie_libs=allowed_movie_libs,
        allowed_tv_libs=allowed_tv_libs,
    )

    # ── Cross-reference with Tunarr ────────────────────────────────────────────
    tunarr_movies = None
    tunarr_shows = None

    if not args.no_crossref:
        print("\n[3/4] Cross-referencing with Tunarr...")
        tunarr_movies, tunarr_shows = build_tunarr_title_sets(tunarr_url)
    else:
        print("\n[3/4] Skipping Tunarr cross-reference (--no-crossref)")

    # ── Filter and build rows ──────────────────────────────────────────────────
    print("\n[4/4] Building export...")
    rows = []
    skipped_movies = []
    skipped_shows = []

    for row in primary_movie_rows:
        title = str(row["Title"])
        if tunarr_movies is not None and title.lower().strip() not in tunarr_movies:
            skipped_movies.append(title)
            continue
        rows.append(row)

    for row in primary_show_rows:
        title = str(row["Title"])
        if tunarr_shows is not None and title.lower().strip() not in tunarr_shows:
            skipped_shows.append(title)
            continue
        rows.append(row)

    # Append content from other Tunarr sources (already crossref'd — they came from Tunarr).
    rows.extend(extra_movie_rows)
    rows.extend(extra_show_rows)

    # ── Write CSV ──────────────────────────────────────────────────────────────
    fieldnames = ["Title", "Year", "Type", "Rating", "Genres", "Director", "Studio", "Actors", "Seasons", "Episodes",
                  "Country", "Mood", "Style", "Source"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # ── Summary ────────────────────────────────────────────────────────────────
    movies_written = sum(1 for r in rows if r["Type"] == "Movie")
    shows_written = sum(1 for r in rows if r["Type"] == "TV")

    print(f"\n  Exported {movies_written} movies, {shows_written} TV shows -> {args.out}")

    if skipped_movies:
        print(f"\n  Skipped {len(skipped_movies)} movies not in Tunarr (not synced):")
        for t in sorted(skipped_movies)[:10]:
            print(f"    - {t}")
        if len(skipped_movies) > 10:
            print(f"    ... and {len(skipped_movies) - 10} more")

    if skipped_shows:
        print(f"\n  Skipped {len(skipped_shows)} shows not in Tunarr (not synced):")
        for t in sorted(skipped_shows):
            print(f"    - {t}")

    print(f"\nDone. Feed {args.out} to your LLM with the prompt in PROMPT.md")

    with open("export_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "movies": movies_written,
            "tv_shows": shows_written,
            "skipped_movies": len(skipped_movies),
            "skipped_shows": len(skipped_shows),
        }, f)


if __name__ == "__main__":
    main()
