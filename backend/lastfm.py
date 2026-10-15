"""Minimal Last.fm API client for Discover Weekly (optional taste and similarity signal).

Only public read methods with an API key - no user auth. Responses are cached in the library cache
database so a weekly run makes a few dozen requests at most.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend import library_cache

logger = logging.getLogger(__name__)

API_URL = "https://ws.audioscrobbler.com/2.0/"
MIN_INTERVAL_S = 0.25          # Last.fm asks for at most ~5 requests per second
DAY = 24 * 3600

_request_lock = threading.Lock()
_last_request_at = 0.0


class LastFmError(Exception):
    """A Last.fm request failed."""


def _ensure_cache_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lastfm_cache (key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )


class LastFmClient:
    def __init__(self, api_key: str, user: str = "", timeout: float = 15.0):
        self.api_key = api_key
        self.user = user
        self.timeout = timeout

    # ---- transport -------------------------------------------------------------------------------------------

    def _get(self, method: str, ttl_s: int, **params: Any) -> dict[str, Any]:
        """Call a Last.fm method, using the cache when the stored answer is younger than ttl_s."""
        key = method + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        conn = library_cache.ensure_db_initialized()
        try:
            _ensure_cache_table(conn)
            row = conn.execute("SELECT fetched_at, payload FROM lastfm_cache WHERE key = ?", (key,)).fetchone()
            if row:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
                if age < timedelta(seconds=ttl_s):
                    return json.loads(row["payload"])
        finally:
            conn.close()

        global _last_request_at
        with _request_lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()
            try:
                resp = httpx.get(
                    API_URL,
                    params={"method": method, "api_key": self.api_key, "format": "json", **params},
                    timeout=self.timeout,
                    headers={"User-Agent": "MediaSage Discover Weekly"},
                )
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise LastFmError(f"{method}: {e}") from e
        if "error" in data:
            # error 6 = artist/track not found: cache the miss like an empty answer
            if data.get("error") == 6:
                data = {}
            else:
                raise LastFmError(f"{method}: {data.get('message', data.get('error'))}")

        conn = library_cache.ensure_db_initialized()
        try:
            _ensure_cache_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO lastfm_cache (key, fetched_at, payload) VALUES (?, ?, ?)",
                (key, datetime.now(timezone.utc).isoformat(), json.dumps(data)),
            )
            conn.commit()
        finally:
            conn.close()
        return data

    # ---- methods ---------------------------------------------------------------------------------------------

    def top_artists(self, period: str = "overall", limit: int = 50, page: int = 1) -> list[tuple[str, int]]:
        """(artist, playcount) for the user. period: overall | 7day | 1month | 3month | 6month | 12month."""
        data = self._get("user.gettopartists", DAY, user=self.user, period=period, limit=limit, page=page)
        artists = (data.get("topartists") or {}).get("artist") or []
        return [(a["name"], int(a.get("playcount") or 0)) for a in artists]

    def known_artists(self, min_plays: int = 5, max_pages: int = 5) -> dict[str, int]:
        """Every artist the user has scrobbled at least min_plays times (overall), name -> playcount."""
        known: dict[str, int] = {}
        for page in range(1, max_pages + 1):
            batch = self.top_artists("overall", limit=1000, page=page)
            for name, plays in batch:
                if plays >= min_plays:
                    known[name] = plays
            if len(batch) < 1000 or (batch and batch[-1][1] < min_plays):
                break
        return known

    def similar_artists(self, artist: str, limit: int = 50) -> list[tuple[str, float]]:
        """(artist, match 0-1) of artists similar to artist."""
        data = self._get("artist.getsimilar", 30 * DAY, artist=artist, limit=limit, autocorrect=1)
        similar = (data.get("similarartists") or {}).get("artist") or []
        return [(a["name"], float(a.get("match") or 0)) for a in similar]

    def artist_top_tracks(self, artist: str, limit: int = 50) -> list[str]:
        """Track titles of the artist, most listened first."""
        data = self._get("artist.gettoptracks", 30 * DAY, artist=artist, limit=limit, autocorrect=1)
        tracks = (data.get("toptracks") or {}).get("track") or []
        return [t["name"] for t in tracks]
