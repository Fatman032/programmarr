// Which media server feeds Tunarr — Plex (the default) or Jellyfin (beta) — so a screen can name
// the server you actually use instead of always saying "Plex".
import { useEffect, useState } from 'react';
import { api } from './client';

export type MediaServer = 'plex' | 'jellyfin';

let known: MediaServer | null = null;
const listeners = new Set<(m: MediaServer) => void>();

/** Called after Settings saves a different choice, so every open screen updates. */
export function setKnownMediaServer(m: MediaServer) {
  known = m;
  listeners.forEach((fn) => fn(m));
}

export function useMediaServer(): { server: MediaServer; name: string; isJellyfin: boolean } {
  const [server, setServer] = useState<MediaServer>(known ?? 'plex');
  useEffect(() => {
    listeners.add(setServer);
    if (!known) {
      api.getConfigStatus()
        .then((s) => setKnownMediaServer(s.media_server === 'jellyfin' ? 'jellyfin' : 'plex'))
        .catch(() => {});
    }
    return () => { listeners.delete(setServer); };
  }, []);
  return { server, name: server === 'jellyfin' ? 'Jellyfin' : 'Plex', isJellyfin: server === 'jellyfin' };
}
