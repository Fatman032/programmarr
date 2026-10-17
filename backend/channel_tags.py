"""channel_tags.py — every channel remembers the ONE server its content plays from (beta).

A channel is tagged "server": "plex" or "jellyfin" when it's made, even when only one server
is set up — so someone who adds Jellyfin later (or Plex later) still knows where each existing
channel came from. The tag is only shown on screen when both servers are set up.

Upgrading: every channel saved before the tag existed is a PLEX channel — no earlier version
could use Jellyfin — so at startup every untagged channel is tagged "plex", whatever is set up
now. A fresh install has no channels; each one is tagged when it's made.
"""

import json
import os
import sys
from pathlib import Path

# channel_engine lives at the repo root (the scripts dir), like scheduler.py finds it — main.py
# loads this module before anything else has put that folder on the path.
_SCRIPTS_DIR = Path(os.environ.get("PROGRAMMARR_SCRIPTS", Path(__file__).parent.parent))
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import channel_engine  # noqa: E402

CHANNEL_FILES = ("channels.json", "channels.draft.json")
UPGRADE_SERVER = "plex"   # what every pre-tag channel is


def default_for(cfg: dict) -> str:
    """The server an untagged channel belongs to, given this config."""
    has_plex = bool(cfg.get("plex_servers") or (cfg.get("plex_url") and cfg.get("plex_token")))
    has_jellyfin = bool(cfg.get("jellyfin_url") and cfg.get("jellyfin_api_key"))
    return channel_engine.default_server(has_plex, has_jellyfin)


def tag(data, cfg: dict, server: str | None = None) -> int:
    """Tag the untagged channels in a channels file's data ({"channels": [...]} or a list) with
    `server`, else the file's own "server" (a Build Channels choice), else the default for `cfg`.
    Returns how many were tagged."""
    channels = data if isinstance(data, list) else (data or {}).get("channels", [])
    if server not in channel_engine.SERVERS:
        own = data.get("server") if isinstance(data, dict) else None
        server = own if own in channel_engine.SERVERS else default_for(cfg)
    return channel_engine.tag_channels(channels, server)


def tag_files(data_dir, cfg: dict) -> int:
    """At startup: tag the untagged channels in channels.json and channels.draft.json "plex" —
    they were saved by a version that could only use Plex. A file that's missing or unreadable
    is left alone. Returns how many channels were tagged."""
    total = 0
    for name in CHANNEL_FILES:
        path = Path(data_dir) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            continue
        changed = tag(data, cfg, UPGRADE_SERVER)
        if changed:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            total += changed
    return total
