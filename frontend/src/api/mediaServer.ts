// Which media servers are set up — Plex, Jellyfin (beta), or both — so a screen can name the
// server(s) you actually use, and leave out what a server you don't have would need.
import { useEffect, useState } from 'react';
import { api } from './client';

export type MediaServer = 'plex' | 'jellyfin';

let known: MediaServer[] | null = null;
const listeners = new Set<(m: MediaServer[]) => void>();

/** Called after Settings saves different servers, so every open screen updates. */
export function setKnownMediaServers(m: MediaServer[]) {
  known = m;
  listeners.forEach((fn) => fn(m));
}

export function useMediaServers(): {
  servers: MediaServer[];
  hasPlex: boolean;
  hasJellyfin: boolean;
  /** "Plex", "Jellyfin", or "Plex and Jellyfin" */
  name: string;
} {
  // Until the answer arrives, assume Plex: that's what an existing install has.
  const [servers, setServers] = useState<MediaServer[]>(known ?? ['plex']);
  useEffect(() => {
    listeners.add(setServers);
    if (!known) {
      api.getConfigStatus().then((s) => setKnownMediaServers(s.servers?.length ? s.servers : ['plex'])).catch(() => {});
    }
    return () => { listeners.delete(setServers); };
  }, []);
  const hasPlex = servers.includes('plex');
  const hasJellyfin = servers.includes('jellyfin');
  const name = [hasPlex && 'Plex', hasJellyfin && 'Jellyfin'].filter(Boolean).join(' and ') || 'Plex';
  return { servers, hasPlex, hasJellyfin, name };
}
