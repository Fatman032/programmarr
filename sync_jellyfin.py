#!/usr/bin/env python3
"""
sync_jellyfin.py — Bring Tunarr's channels into Jellyfin's Live TV guide (BETA).

The Jellyfin counterpart of sync_plex.py. You add Tunarr to Jellyfin's Live TV once by hand (an
M3U tuner and an XMLTV guide, both from Tunarr). After that, every deploy runs this: it has
Jellyfin refresh its guide, so new and changed channels show up right away instead of at
Jellyfin's next scheduled refresh.

Jellyfin keeps its downloaded copy of the XMLTV guide for an hour and reuses it on a refresh, so a
channel added within that hour shows up without guide listings — and Jellyfin's guide hides a
channel with no listings. So the guide provider is saved again unchanged first, which is what
Save does in Jellyfin's dashboard: Jellyfin then drops its copy and downloads Tunarr's guide fresh.

Exit codes: 0 = Jellyfin's guide is up to date; 2 = Jellyfin has no Tunarr tuner or guide yet,
or channels are still missing after the refresh (the deploy screen then shows the one-time
steps); 3 = every channel is in Jellyfin but some have no guide listings yet; 1 = Jellyfin or
Tunarr couldn't be read.

Usage:
    python sync_jellyfin.py            # refresh Jellyfin's guide
    python sync_jellyfin.py --probe    # show the current state, no changes
"""

import argparse
import json
import sys
import time
import urllib.parse

import channel_engine
import jellyfin_api
from sync_plex import get_tunarr_channels

CONFIG_FILE = "config.json"

# What Tunarr serves for Jellyfin's Live TV.
M3U_PATH = "/api/channels.m3u"
XMLTV_PATH = "/api/xmltv.xml"

REFRESH_TASK_KEY = "RefreshGuide"
REFRESH_TIMEOUT_S = 300
POLL_S = 2


def _channels(n):
    return f"{n} channel" + ("" if n == 1 else "s")


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {CONFIG_FILE} not found.")
        sys.exit(1)


def _port(url):
    try:
        p = urllib.parse.urlsplit(url)
        return p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None


def find_tunarr_tuner(livetv, tunarr_url):
    """Jellyfin's tuner that reads Tunarr: an M3U tuner on Tunarr's channels.m3u, or an
    HDHomeRun tuner on Tunarr's port (Tunarr can act as one). Jellyfin may reach Tunarr by a
    different address than Programmarr does, so the address itself isn't compared."""
    for t in livetv.get("TunerHosts") or []:
        url = (t.get("Url") or "").rstrip("/")
        kind = (t.get("Type") or "").lower()
        if kind == "m3u" and urllib.parse.urlsplit(url).path.lower().endswith(M3U_PATH):
            return t
        if kind == "hdhomerun" and url and _port(url) == _port(tunarr_url):
            return t
    return None


def find_tunarr_guide(livetv):
    """Jellyfin's XMLTV guide that reads Tunarr's xmltv.xml."""
    for p in livetv.get("ListingProviders") or []:
        path = (p.get("Path") or "").rstrip("/")
        if (p.get("Type") or "").lower() == "xmltv" and urllib.parse.urlsplit(path).path.lower().endswith(XMLTV_PATH):
            return p
    return None


def jellyfin_channels(url, key):
    """{channel number: Jellyfin channel id} for Jellyfin's Live TV channels, or None."""
    data = jellyfin_api.get(url, key, "/LiveTv/Channels?EnableImages=false&EnableUserData=false")
    if not isinstance(data, dict):
        return None
    return {str(c.get("Number")): c.get("Id") for c in data.get("Items", []) if c.get("Number") is not None}


def jellyfin_channel_numbers(url, key):
    channels = jellyfin_channels(url, key)
    return None if channels is None else set(channels)


def channels_without_listings(url, key, channels):
    """The channel numbers (of {number: Jellyfin id}) that have no guide listings in Jellyfin.
    Jellyfin's guide doesn't show a channel without listings."""
    empty = []
    for number, channel_id in channels.items():
        data = jellyfin_api.get(url, key, f"/LiveTv/Programs?ChannelIds={channel_id}&Limit=1&EnableImages=false")
        if isinstance(data, dict) and not data.get("TotalRecordCount"):
            empty.append(number)
    return sorted(empty, key=int)


def refresh_guide(url, key, guide=None):
    """Have Jellyfin download Tunarr's guide fresh and refresh, then wait for it to finish. True
    if it completed. With `guide` (Jellyfin's XMLTV provider entry, as read), the entry is saved
    again unchanged: Jellyfin then deletes its cached copy and queues the refresh itself. If that
    isn't accepted, the refresh is started directly (and may reuse a copy up to an hour old)."""
    tasks = jellyfin_api.get(url, key, "/ScheduledTasks") or []
    task = next((t for t in tasks if t.get("Key") == REFRESH_TASK_KEY), None)
    if not task:
        print("  ! Jellyfin has no Refresh Guide task")
        return False
    before = (task.get("LastExecutionResult") or {}).get("EndTimeUtc")
    resaved = guide is not None and jellyfin_api.post(
        url, key, "/LiveTv/ListingProviders?validateListings=false&validateLogin=false", body=guide)
    if resaved:
        print("  Asked Jellyfin to download Tunarr's guide fresh")
    elif task.get("State") != "Running" and not jellyfin_api.post(url, key, f"/ScheduledTasks/Running/{task['Id']}"):
        return False

    deadline = time.time() + REFRESH_TIMEOUT_S
    last_pct = -1
    while time.time() < deadline:
        time.sleep(POLL_S)
        t = jellyfin_api.get(url, key, f"/ScheduledTasks/{task['Id']}") or {}
        result = t.get("LastExecutionResult") or {}
        if t.get("State") == "Running":
            pct = int(t.get("CurrentProgressPercentage") or 0)
            if pct >= last_pct + 25:
                print(f"  Refreshing… {pct}%")
                last_pct = pct
        elif result.get("EndTimeUtc") and result.get("EndTimeUtc") != before:
            if result.get("Status") == "Completed":
                return True
            if result.get("Status") == "Cancelled" and resaved:
                before = result.get("EndTimeUtc")  # a run already going was stopped for the queued one
                continue
            print(f"  ! Jellyfin's guide refresh ended with: {result.get('Status')} {result.get('ErrorMessage') or ''}".rstrip())
            return False
    print(f"  ! Jellyfin's guide refresh didn't finish within {REFRESH_TIMEOUT_S // 60} minutes")
    return False


def print_manual_instructions(tunarr_url):
    print()
    print("  One-time setup in Jellyfin:")
    print("  1. Open Jellyfin > Dashboard > Live TV")
    print("  2. Under Tuner Devices, click + and choose M3U Tuner. Under File or URL, paste:")
    print(f"       {tunarr_url}{M3U_PATH}")
    print("     Click Save.")
    print("  3. Under TV Guide Data Providers, click + and choose XMLTV. Under File or URL, paste:")
    print(f"       {tunarr_url}{XMLTV_PATH}")
    print("     Click Save.")
    print("  4. Click Refresh Guide Data.")


def main():
    parser = argparse.ArgumentParser(description="Refresh Jellyfin's Live TV guide from Tunarr (beta)")
    parser.add_argument("--probe", action="store_true", help="Show state only, no changes")
    args = parser.parse_args()

    cfg = load_config()
    channel_engine.set_tunarr_auth_from_config(cfg)
    tunarr_url = cfg.get("tunarr_url", "").rstrip("/")
    url, key = cfg.get("jellyfin_url", "").rstrip("/"), cfg.get("jellyfin_api_key", "")
    if not (tunarr_url and url and key):
        print("ERROR: Jellyfin isn't set up — add the Jellyfin URL and API key in Settings.")
        sys.exit(1)

    print("Fetching Tunarr channel list...")
    tunarr_nums = set(get_tunarr_channels(tunarr_url))
    if not tunarr_nums:
        print("ERROR: Could not fetch XMLTV from Tunarr")
        sys.exit(1)
    print(f"  {len(tunarr_nums)} channels in Tunarr")

    print("Fetching Jellyfin Live TV setup...")
    livetv = jellyfin_api.get(url, key, "/System/Configuration/livetv")
    if not isinstance(livetv, dict):
        print("ERROR: Could not read Jellyfin's Live TV settings — check the Jellyfin URL and API key.")
        sys.exit(1)
    tuner, guide = find_tunarr_tuner(livetv, tunarr_url), find_tunarr_guide(livetv)
    if not tuner or not guide:
        missing = " and ".join(w for w, ok in (("a Tunarr tuner", tuner), ("a Tunarr XMLTV guide", guide)) if not ok)
        print(f"  Jellyfin Live TV doesn't have {missing} yet.")
        print_manual_instructions(tunarr_url)
        sys.exit(2)
    print(f"  Tuner: {tuner.get('Url')}")
    print(f"  Guide: {guide.get('Path')}")

    have = jellyfin_channel_numbers(url, key)
    if have is None:
        print("ERROR: Could not read Jellyfin's Live TV channels")
        sys.exit(1)
    missing = tunarr_nums - have
    print(f"  {len(tunarr_nums & have)} of {len(tunarr_nums)} Tunarr channels in Jellyfin")

    if args.probe:
        print(f"\n[PROBE] Would refresh Jellyfin's guide"
              + (f" to add {_channels(len(missing))}: " + ", ".join(f"#{n}" for n in sorted(missing, key=int)) if missing else "") + ".")
        return

    print("\nRefreshing Jellyfin's Live TV guide...")
    if not refresh_guide(url, key, guide):
        sys.exit(1)

    now = jellyfin_channels(url, key) or {}
    have = set(now)
    still_missing = tunarr_nums - have
    if still_missing:
        print(f"  Jellyfin still doesn't have {_channels(len(still_missing))}: "
              + ", ".join(f"#{n}" for n in sorted(still_missing, key=int)))
        print("  Its tuner may point at a different Tunarr — check the address under Tuner Devices.")
        print_manual_instructions(tunarr_url)
        sys.exit(2)
    added = len(missing)
    print(f"  Guide refreshed — all {len(tunarr_nums)} channels are in Jellyfin"
          + (f" ({added} added)." if added else "."))
    no_listings = channels_without_listings(url, key, {n: now[n] for n in tunarr_nums})
    if no_listings:
        print(f"  ! No guide listings yet for {_channels(len(no_listings))}: "
              + ", ".join(f"#{n}" for n in no_listings)
              + ". Jellyfin's guide hides a channel until it has listings; they arrive on its next guide refresh.")
        sys.exit(3)


if __name__ == "__main__":
    main()
