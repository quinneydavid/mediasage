"""Discover Weekly: a weekly AI-picked playlist of library tracks the user has never played.

A run:
1. builds a taste profile from the cache's play history (and Last.fm, when configured);
2. lists candidate artists - artists the user has never played ("new") and unplayed albums of artists they know
   ("deep cuts") - and lets the LLM pick the ones that fit (stage A);
3. collects those artists' never-played tracks and lets the LLM pick and order the playlist (stage B);
4. replaces the tracks of one fixed playlist on the media server and saves the result to the history.

Every candidate carries an id (A12, T345) that the LLM must echo back, so 100 % of picks map to library tracks.
Feedback comes from the cache too: picks that were played since boost their artists, low ratings block them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend import library_cache
from backend.config import load_user_yaml_config, save_user_config
from backend.lastfm import LastFmClient, LastFmError
from backend.llm_client import get_llm_client, get_model_context_limit
from backend.models import GenerateResponse, Track
from backend.plex_client import simplify_string
from backend.sonic_order import order_by_sound
from backend.themes import (FIT_MIN, LANE_ARTISTS, RECENT_THEMES, Theme, choose_themes, fetch_tags, listening_lanes,
                            tag_share, tag_specificity, theme_fit)

logger = logging.getLogger(__name__)

RESULT_TYPE = "discover_weekly"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
RETRY_MINUTES = 60
STAGE_A_ARTISTS = 40          # artists the LLM picks in stage A (at least; scales with the playlist size)
TRACKS_PER_ARTIST = 12        # candidate tracks per chosen artist for stage B
RECENT_DAYS = 90
TOKENS_PER_LINE = 22          # rough size of one candidate line in the prompt
SKIP_ARTISTS = {"various artists", "various", "unknown artist", "soundtrack", "original soundtrack"}
MIN_TRACK_MS, MAX_TRACK_MS = 60_000, 15 * 60_000

_run_lock = threading.Lock()
_running = False
_last_attempt: dict[str, Any] = {"at": None, "error": None}


# =============================================================================
# Settings
# =============================================================================

ENV = {
    "enabled": "DISCOVER_WEEKLY_ENABLED",
    "day": "DISCOVER_WEEKLY_DAY",
    "time": "DISCOVER_WEEKLY_TIME",
    "track_count": "DISCOVER_WEEKLY_TRACKS",
    "playlist_title": "DISCOVER_WEEKLY_TITLE",
    "archive_title": "DISCOVER_WEEKLY_ARCHIVE",
    "new_artist_share": "DISCOVER_WEEKLY_NEW_ARTIST_SHARE",
    "max_per_artist": "DISCOVER_WEEKLY_MAX_PER_ARTIST",
    "order_by_sound": "DISCOVER_WEEKLY_ORDER_BY_SOUND",
    "theme_mode": "DISCOVER_WEEKLY_THEMES",
    "next_theme": "DISCOVER_WEEKLY_NEXT_THEME",
    "lastfm_user": "LASTFM_USER",
    "lastfm_api_key": "LASTFM_API_KEY",
}


@dataclass
class Settings:
    enabled: bool = False
    day: str = "monday"
    time: str = "06:00"
    track_count: int = 30
    playlist_title: str = "Discover Weekly"
    archive_title: str = ""         # a playlist that keeps every past week's tracks (empty = off)
    new_artist_share: float = 0.7
    max_per_artist: int = 1         # tracks by the same (main) artist in one week
    order_by_sound: bool = True     # reorder by the media server's sonic analysis so tracks blend (Plex)
    theme_mode: str = "auto"        # auto = one style theme per week from what the listener plays (Last.fm); off
    next_theme: str = ""            # the listener's wish for the next run's theme (used once)
    lastfm_user: str = ""
    lastfm_api_key: str = ""
    env_locked: list[str] = field(default_factory=list)

    @property
    def lastfm_enabled(self) -> bool:
        return bool(self.lastfm_user and self.lastfm_api_key)

    def public(self) -> dict[str, Any]:
        """Settings for the API: the Last.fm key is never sent back, only whether one is set."""
        return {
            "enabled": self.enabled, "day": self.day, "time": self.time, "track_count": self.track_count,
            "playlist_title": self.playlist_title, "archive_title": self.archive_title,
            "new_artist_share": self.new_artist_share,
            "max_per_artist": self.max_per_artist, "order_by_sound": self.order_by_sound,
            "theme_mode": self.theme_mode, "next_theme": self.next_theme,
            "lastfm_user": self.lastfm_user, "has_lastfm_api_key": bool(self.lastfm_api_key),
            "env_locked": self.env_locked,
        }


def _coerce(name: str, value: Any) -> Any:
    if name in ("enabled", "order_by_sound"):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    if name == "track_count":
        return max(5, min(100, int(value)))
    if name == "max_per_artist":
        return max(1, min(5, int(value)))
    if name == "new_artist_share":
        return max(0.0, min(1.0, float(value)))
    if name == "theme_mode":
        mode = str(value).strip().lower()
        if mode not in ("auto", "off"):
            raise ValueError("theme_mode must be auto or off")
        return mode
    if name == "day":
        day = str(value).strip().lower()
        if day not in DAYS:
            raise ValueError(f"day must be one of {', '.join(DAYS)}")
        return day
    if name == "time":
        datetime.strptime(str(value).strip(), "%H:%M")
        return str(value).strip()
    return str(value).strip()


def load_settings() -> Settings:
    """YAML section discover_weekly in config.user.yaml, overridden by environment variables."""
    yaml_section = (load_user_yaml_config() or {}).get("discover_weekly") or {}
    settings = Settings()
    for name, env_key in ENV.items():
        env_value = os.environ.get(env_key)
        raw = env_value if env_value else yaml_section.get(name)
        if raw is None or raw == "":
            continue
        try:
            setattr(settings, name, _coerce(name, raw))
        except (ValueError, TypeError) as e:
            logger.warning("Discover Weekly: ignoring invalid %s=%r (%s)", name, raw, e)
            continue
        if env_value:
            settings.env_locked.append(name)
    return settings


def _replace_section(section: dict[str, Any]) -> None:
    """Rewrite the discover_weekly section without merging (save_user_config merges and cannot delete keys)."""
    import yaml
    from backend.config import USER_CONFIG_PATH
    data = dict(load_user_yaml_config() or {})
    data["discover_weekly"] = section
    with open(USER_CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


def save_settings(updates: dict[str, Any]) -> Settings:
    """Validate and save settings (fields locked by environment variables are ignored)."""
    current = load_settings()
    clean: dict[str, Any] = {}
    for name, value in updates.items():
        if name not in ENV or value is None or name in current.env_locked:
            continue
        clean[name] = _coerce(name, value)
    if clean.get("next_theme") == "":   # an empty value is dropped by save_user_config: delete the key instead
        clean.pop("next_theme")
        section = dict((load_user_yaml_config() or {}).get("discover_weekly") or {})
        if section.pop("next_theme", None) is not None:
            _replace_section(section)
    if clean:
        save_user_config({"discover_weekly": clean})
    return load_settings()


# =============================================================================
# Run records
# =============================================================================

def _ensure_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discover_weekly_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            trigger TEXT,
            error TEXT,
            playlist_id TEXT,
            title TEXT,
            intro TEXT,
            picks TEXT,
            result_id TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE discover_weekly_runs ADD COLUMN theme TEXT")
    except Exception:
        pass    # column exists
    for col in ("archived_at", "archive_playlist_id"):
        try:
            conn.execute(f"ALTER TABLE discover_weekly_runs ADD COLUMN {col} TEXT")
        except Exception:
            pass    # column exists


def _record_start(trigger: str) -> int:
    conn = library_cache.ensure_db_initialized()
    try:
        _ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO discover_weekly_runs (started_at, status, trigger) VALUES (?, 'running', ?)",
            (_now().isoformat(), trigger),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _record_end(run_id: int, **fields: Any) -> None:
    fields["finished_at"] = _now().isoformat()
    if "picks" in fields:
        fields["picks"] = json.dumps(fields["picks"])
    if isinstance(fields.get("theme"), dict):
        fields["theme"] = json.dumps(fields["theme"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = library_cache.ensure_db_initialized()
    try:
        conn.execute(f"UPDATE discover_weekly_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
        conn.commit()
    finally:
        conn.close()


def _row_to_run(row) -> dict[str, Any]:
    run = dict(row)
    run["picks"] = json.loads(run["picks"]) if run.get("picks") else []
    run["theme"] = json.loads(run["theme"]) if run.get("theme") else None
    return run


def list_runs(limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
    conn = library_cache.ensure_db_initialized()
    try:
        _ensure_tables(conn)
        sql = "SELECT * FROM discover_weekly_runs"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [_row_to_run(r) for r in rows]
    finally:
        conn.close()


def _mark_interrupted_runs() -> None:
    """A run still 'running' in the DB but not in this process died with the previous process."""
    conn = library_cache.ensure_db_initialized()
    try:
        _ensure_tables(conn)
        conn.execute(
            "UPDATE discover_weekly_runs SET status = 'error', error = 'interrupted (restart)', finished_at = ? "
            "WHERE status = 'running'", (_now().isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Schedule
# =============================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def last_scheduled_at(settings: Settings, now: datetime | None = None) -> datetime:
    """The most recent scheduled time (local container time zone) at or before now."""
    local_now = (now or _now()).astimezone()
    hour, minute = (int(x) for x in settings.time.split(":"))
    days_back = (local_now.weekday() - DAYS.index(settings.day)) % 7
    slot = (local_now - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local_now:
        slot -= timedelta(days=7)
    return slot


def next_scheduled_at(settings: Settings, now: datetime | None = None) -> datetime:
    return last_scheduled_at(settings, now) + timedelta(days=7)


def is_due(settings: Settings, now: datetime | None = None) -> bool:
    """Enabled, past this week's slot, no successful run since, and not inside the retry backoff."""
    if not settings.enabled or _running:
        return False
    now = now or _now()
    slot = last_scheduled_at(settings, now)
    for run in list_runs(limit=1, status="success"):
        if datetime.fromisoformat(run["started_at"]) >= slot:
            return False
    attempt_at = _last_attempt["at"]
    if attempt_at and _last_attempt["error"] and attempt_at >= slot:
        if now - attempt_at < timedelta(minutes=RETRY_MINUTES):
            return False
    return True


def status() -> dict[str, Any]:
    settings = load_settings()
    runs = list_runs(limit=1)
    successes = list_runs(limit=1, status="success")
    last_success = successes[0] if successes else None
    return {
        "settings": settings.public(),
        "running": _running,
        "next_run_at": next_scheduled_at(settings).isoformat() if settings.enabled else None,
        "last_run": runs[0] if runs else None,
        "last_success_at": last_success["finished_at"] if last_success else None,
        "playlist_id": last_success["playlist_id"] if last_success else None,
        "recent_themes": [r["theme"]["name"] for r in list_runs(limit=8, status="success") if r.get("theme")][:6],
    }


# =============================================================================
# Library data
# =============================================================================

def _artist_key(name: str) -> str:
    return simplify_string(name or "")


def _credit_keys(name: str) -> set[str]:
    """Keys of every artist in a credit ('Chris Lake;Grimes;NPC' -> {chris lake, grimes, npc})."""
    return {k for k in (_artist_key(part) for part in (name or "").split(";")) if k}


def _primary_artist(name: str) -> str:
    """'Sonic Boom;Panda Bear' -> 'Sonic Boom' (multi-artist credits are joined with ';')."""
    return (name or "").split(";")[0].strip()


@dataclass
class LibraryView:
    """One pass over the cache: per-artist stats and every unplayed track."""
    artist_plays: Counter = field(default_factory=Counter)          # artist -> total plays
    artist_recent: Counter = field(default_factory=Counter)         # artist -> plays of tracks played recently
    artist_names: dict[str, str] = field(default_factory=dict)      # key -> display name
    artist_genres: dict[str, Counter] = field(default_factory=dict)
    artist_years: dict[str, list[int]] = field(default_factory=dict)
    genre_weight: Counter = field(default_factory=Counter)          # genres of played tracks, by plays
    unplayed: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # artist -> unplayed tracks
    album_plays: Counter = field(default_factory=Counter)           # (artist, album) -> plays
    recent_tracks: list[dict[str, Any]] = field(default_factory=list)
    loved_tracks: list[dict[str, Any]] = field(default_factory=list)
    ratings: dict[str, int] = field(default_factory=dict)           # rating_key -> user rating
    views: dict[str, int] = field(default_factory=dict)             # rating_key -> view count (picked tracks)


def load_library(now: datetime | None = None, watch_keys: set[str] | None = None) -> LibraryView:
    view = LibraryView()
    recent_cutoff = ((now or _now()) - timedelta(days=RECENT_DAYS)).isoformat()
    watch_keys = watch_keys or set()
    conn = library_cache.ensure_db_initialized()
    try:
        rows = conn.execute(
            "SELECT rating_key, title, artist, album, duration_ms, year, genres, user_rating, is_live, "
            "view_count, last_viewed_at FROM tracks"
        )
        recent: list[dict[str, Any]] = []
        for r in rows:
            artist = r["artist"] or ""
            key = _artist_key(artist)
            if not key:
                continue
            view.artist_names.setdefault(key, artist)
            plays = r["view_count"] or 0
            genres = json.loads(r["genres"]) if r["genres"] else []
            gcount = view.artist_genres.setdefault(key, Counter())
            gcount.update(genres)
            if r["year"]:
                view.artist_years.setdefault(key, []).append(r["year"])
            if r["rating_key"] in watch_keys:
                view.views[r["rating_key"]] = plays
                if r["user_rating"]:
                    view.ratings[r["rating_key"]] = r["user_rating"]
            if plays:
                view.artist_plays[key] += plays
                view.album_plays[(key, r["album"])] += plays
                view.genre_weight.update({g: plays for g in genres})
                last = r["last_viewed_at"] or ""
                if last >= recent_cutoff:
                    view.artist_recent[key] += plays
                    recent.append({"artist": artist, "title": r["title"], "last": last})
                if (r["user_rating"] or 0) >= 8:
                    view.loved_tracks.append({"artist": artist, "title": r["title"]})
                continue
            if r["is_live"]:
                continue
            duration = r["duration_ms"] or 0
            if duration and not (MIN_TRACK_MS <= duration <= MAX_TRACK_MS):
                continue
            view.unplayed.setdefault(key, []).append({
                "rating_key": r["rating_key"], "title": r["title"], "artist": artist, "album": r["album"],
                "duration_ms": duration, "year": r["year"], "genres": genres,
            })
        recent.sort(key=lambda t: t["last"], reverse=True)
        view.recent_tracks = recent[:40]
        view.loved_tracks = view.loved_tracks[:30]
    finally:
        conn.close()
    return view


# =============================================================================
# Taste profile
# =============================================================================

@dataclass
class Profile:
    top_artists: list[str]
    recent_artists: list[str]
    lastfm_recent: list[str]
    lastfm_year: list[str]
    lastfm_overall: list[str]
    genres: list[str]
    recent_tracks: list[str]
    loved_tracks: list[str]
    liked_picks: list[str]          # artists from earlier weeks that got played
    known: set[str]                 # artist keys the user knows (plays in the library or on Last.fm)
    blocked: set[str]               # artist keys rated down in earlier weeks
    recently_picked: set[str]       # artist keys used in the last few weeks
    similar: dict[str, tuple[float, str]]  # artist key -> (Last.fm match, similar to whom)

    def describe(self) -> str:
        parts = []
        if self.recent_artists:
            parts.append("Most played lately: " + ", ".join(self.recent_artists[:25]))
        if self.top_artists:
            parts.append("Most played overall: " + ", ".join(self.top_artists[:25]))
        if self.lastfm_recent:
            parts.append("Last.fm, last 3 months: " + ", ".join(self.lastfm_recent[:25]))
        if self.lastfm_year:
            parts.append("Last.fm, last 12 months: " + ", ".join(self.lastfm_year[:25]))
        if self.lastfm_overall:
            parts.append("Last.fm, all time: " + ", ".join(self.lastfm_overall[:25]))
        if self.genres:
            parts.append("Favourite genres: " + ", ".join(self.genres[:15]))
        if self.recent_tracks:
            parts.append("Recently played tracks: " + "; ".join(self.recent_tracks[:20]))
        if self.loved_tracks:
            parts.append("Highly rated tracks: " + "; ".join(self.loved_tracks[:15]))
        if self.liked_picks:
            parts.append("Discoveries from earlier weeks that they went on to play: " + ", ".join(self.liked_picks))
        return "\n".join(parts)


def _previous_picks(weeks: int = 8, before: datetime | None = None) -> list[dict[str, Any]]:
    """Picks of earlier weeks. Runs since `before` (this week's slot) are left out: a regeneration replaces this
    week's playlist, so its artists must not be rested as if they were last week's."""
    picks = []
    for run in list_runs(limit=weeks + 5, status="success"):
        if before and datetime.fromisoformat(run["started_at"]) >= before:
            continue
        picks.extend(run["picks"])
    return picks


def build_profile(view: LibraryView, settings: Settings, lastfm: LastFmClient | None,
                  previous: list[dict[str, Any]]) -> Profile:
    names = view.artist_names
    top = [names[k] for k, _ in view.artist_plays.most_common(60) if k not in SKIP_ARTISTS]
    recent = [names[k] for k, _ in view.artist_recent.most_common(40) if k not in SKIP_ARTISTS]
    known: set[str] = set()
    for k, plays in view.artist_plays.items():
        if plays > 0:
            known |= {k} | _credit_keys(names[k])

    lf_recent: list[str] = []
    lf_year: list[str] = []
    lf_overall: list[str] = []
    similar: dict[str, tuple[float, str]] = {}
    if lastfm:
        try:
            lf_recent = [a for a, _ in lastfm.top_artists("3month", 50)]
            lf_year = [a for a, _ in lastfm.top_artists("12month", 50)]
            lf_overall = [a for a, _ in lastfm.top_artists("overall", 50)]
            known |= {_artist_key(a) for a in lastfm.known_artists()}
            seeds = list(dict.fromkeys(lf_recent[:12] + recent[:8] + lf_year[:8]))[:25]
            for seed in seeds:
                for name, match in lastfm.similar_artists(_primary_artist(seed), 60):
                    key = _artist_key(name)
                    if key and match > similar.get(key, (0.0, ""))[0]:
                        similar[key] = (match, seed)
        except LastFmError as e:
            logger.warning("Discover Weekly: Last.fm unavailable, continuing without it (%s)", e)

    liked, blocked = [], set()
    for pick in previous:
        key = _artist_key(pick.get("artist", ""))
        rating = view.ratings.get(pick.get("rating_key", ""))
        if rating is not None and rating <= 4:
            blocked.add(key)
        elif view.views.get(pick.get("rating_key", ""), 0) > 0:
            liked.append(pick["artist"])
    recently_picked = {_artist_key(p.get("artist", "")) for p in previous[: settings.track_count * 4]}

    return Profile(
        top_artists=top, recent_artists=recent, lastfm_recent=lf_recent, lastfm_year=lf_year,
        lastfm_overall=lf_overall, genres=[g for g, _ in view.genre_weight.most_common(20)],
        recent_tracks=[f"{t['artist']} - {t['title']}" for t in view.recent_tracks],
        loved_tracks=[f"{t['artist']} - {t['title']}" for t in view.loved_tracks],
        liked_picks=list(dict.fromkeys(liked))[:20], known=known, blocked=blocked,
        recently_picked=recently_picked, similar=similar,
    )


# =============================================================================
# Candidates
# =============================================================================

@dataclass
class ArtistCandidate:
    id: str
    key: str
    name: str
    kind: str                       # "new" or "deep"
    album: str | None               # deep cuts: the unplayed album
    genres: list[str]
    decades: str
    tracks: list[dict[str, Any]]
    score: float = 0.0
    similar_to: str = ""
    match: float = 0.0              # Last.fm similarity to an artist the listener plays (0 = none known)
    fit: float = 0.0                # fit to this week's theme (0 without a theme)

    def line(self) -> str:
        what = f"{self.name} - album '{self.album}'" if self.album else self.name
        extra = f" | similar to {self.similar_to}" if self.similar_to else ""
        return f"{self.id} | {what} | {', '.join(self.genres[:3]) or 'no genre'} | {self.decades} | " \
               f"{len(self.tracks)} tracks{extra}"


def _decades(years: list[int]) -> str:
    decades = sorted({(y // 10) * 10 for y in years if y})
    if not decades:
        return "unknown era"
    return f"{decades[0]}s" if len(decades) == 1 else f"{decades[0]}s-{decades[-1]}s"


def artist_candidates(view: LibraryView, profile: Profile, budget_lines: int, new_share: float,
                      rng: random.Random, exclude_recent: bool = True, fit_by_key: dict[str, float] | None = None,
                      tags_by_key: dict[str, list[tuple[str, int]]] | None = None) -> list[ArtistCandidate]:
    """Never-played artists ("new") and unplayed albums of known artists ("deep"), best first.

    With a theme (fit_by_key), only artists that fit it are candidates, ranked by fit; their Last.fm tags replace the
    broad library genres in what the AI sees.
    """
    fav_genres = set(profile.genres[:15])
    new: list[ArtistCandidate] = []
    deep: list[ArtistCandidate] = []
    for key, tracks in view.unplayed.items():
        if key in SKIP_ARTISTS or key in profile.blocked or (exclude_recent and key in profile.recently_picked):
            continue
        genres = [g for g, _ in view.artist_genres.get(key, Counter()).most_common(4)]
        overlap = len(fav_genres.intersection(genres))
        match, seed = profile.similar.get(key, (0.0, ""))
        fit = 0.0
        if fit_by_key is not None:
            fit = fit_by_key.get(key, 0.0)
            if fit < FIT_MIN:
                continue
            overlap = fit * 6          # theme fit replaces the broad genre overlap in the ranking
        if tags_by_key and tags_by_key.get(key):
            genres = [t for t, _ in tags_by_key[key][:4]]
        if not (_credit_keys(view.artist_names[key]) | {key}) & profile.known:
            score = match * 3 + overlap * 0.5 + rng.random() * 0.5
            new.append(ArtistCandidate("", key, view.artist_names[key], "new", None, genres,
                                       _decades(view.artist_years.get(key, [])), tracks, score, seed, match, fit))
            continue
        # Known artist: albums with no plays at all are deep cuts
        by_album: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in tracks:
            if view.album_plays.get((key, t["album"]), 0) == 0:
                by_album[t["album"]].append(t)
        plays = view.artist_plays.get(key, 0)
        for album, album_tracks in by_album.items():
            if len(album_tracks) < 3:
                continue    # singles and stray tracks make poor deep cuts
            years = [t["year"] for t in album_tracks if t["year"]]
            score = min(plays, 200) / 50 + overlap * 0.5 + rng.random() * 0.5
            deep.append(ArtistCandidate("", key, view.artist_names[key], "deep", album, genres,
                                        _decades(years), album_tracks, score, fit=fit))
    new.sort(key=lambda c: c.score, reverse=True)
    deep.sort(key=lambda c: c.score, reverse=True)
    n_new = int(budget_lines * new_share)
    chosen = new[:n_new] + deep[: budget_lines - min(n_new, len(new))]
    for i, cand in enumerate(chosen, 1):
        cand.id = f"A{i}"
    return chosen


def top_up_artists(chosen: list[ArtistCandidate], candidates: list[ArtistCandidate],
                   targets: dict[str, int]) -> list[ArtistCandidate]:
    """Fill each kind up to its target with the best candidates the AI didn't pick (local models often return fewer
    ids than asked). New artists with Last.fm similarity to what the listener plays come first, strongest match
    first; broad genre overlap alone (tags like "Pop/Rock" match almost anything) is the last resort. Deep cuts keep
    their play-history ranking."""
    out = list(chosen)
    candidates = sorted(candidates, key=lambda c: (c.match > 0, c.match, c.score), reverse=True)
    used_ids = {c.id for c in out}
    used_artists = {c.key for c in out}
    for kind, target in targets.items():
        have = sum(1 for c in out if c.kind == kind)
        for cand in candidates:
            if have >= target:
                break
            if cand.kind == kind and cand.id not in used_ids and cand.key not in used_artists:
                out.append(cand)
                used_ids.add(cand.id)
                used_artists.add(cand.key)
                have += 1
    return out


def track_candidates(chosen: list[ArtistCandidate], lastfm: LastFmClient | None,
                     rng: random.Random) -> list[dict[str, Any]]:
    """Per chosen artist/album, up to TRACKS_PER_ARTIST unplayed tracks, best-known first when Last.fm says so."""
    out: list[dict[str, Any]] = []
    for cand in chosen:
        seen_titles: set[str] = set()
        tracks = []
        for t in cand.tracks:
            title_key = simplify_string(t["title"])
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            tracks.append(t)
        rank: dict[str, int] = {}
        if lastfm:
            try:
                top = lastfm.artist_top_tracks(_primary_artist(cand.name), 50)
                rank = {simplify_string(title): i for i, title in enumerate(top)}
            except LastFmError as e:
                logger.info("Discover Weekly: no Last.fm top tracks for %s (%s)", cand.name, e)
        rng.shuffle(tracks)
        tracks.sort(key=lambda t: rank.get(simplify_string(t["title"]), 999))
        for t in tracks[:TRACKS_PER_ARTIST]:
            # the cap counts the main artist: "Sampha" and "Sampha;Little Simz" are the same artist
            out.append({**t, "artist_id": cand.id, "artist_key": _artist_key(_primary_artist(t["artist"])),
                        "kind": cand.kind})
    for i, t in enumerate(out, 1):
        t["id"] = f"T{i}"
    return out


# =============================================================================
# LLM stages
# =============================================================================

SYSTEM = (
    "You are a music curator building a personal 'Discover Weekly' playlist from the listener's own music "
    "library: songs they own but have never played. You know artists' sound, scenes and influences well. "
    "Respond with JSON only."
)


def _stage_a_prompt(profile: Profile, candidates: list[ArtistCandidate], n_new: int, n_deep: int,
                    theme: Theme | None = None) -> str:
    new_lines = "\n".join(c.line() for c in candidates if c.kind == "new") or "(none)"
    theme_text = (theme.prompt_text() + "\nEvery pick must clearly fit this theme.\n\n") if theme else ""
    deep_lines = "\n".join(c.line() for c in candidates if c.kind == "deep") or "(none)"
    return f"""The listener's taste:
{profile.describe()}

{theme_text}NEW ARTISTS - in their library, never played (id | artist | styles | era | tracks | Last.fm similarity):
{new_lines}

DEEP CUTS - albums they have never played by artists they already know:
{deep_lines}

Pick exactly {n_new} ids from NEW ARTISTS and {n_deep} ids from DEEP CUTS, all by different artists, that this
listener is most likely to love - close to their taste, with a few adventurous picks that still fit. Prefer variety
across genres and eras within their taste. Base each reason on the genres shown and what you know of the artist; if you don't know an
artist, rely on its genres and similarity instead of guessing.
Avoid comedy, spoken word, children's music, karaoke/tribute/cover compilations, sound effects and ambient filler.

Return JSON: {{"artists": [{{"id": "A12", "reason": "one short sentence on why it fits"}}]}}"""


def _stage_b_prompt(profile: Profile, chosen: list[ArtistCandidate], reasons: dict[str, str],
                    tracks: list[dict[str, Any]], quotas: dict[str, int], max_per_artist: int = 1,
                    theme: Theme | None = None) -> str:
    by_id = {c.id: c for c in chosen}
    lines = []
    for t in tracks:
        cand = by_id[t["artist_id"]]
        kind = "new artist" if cand.kind == "new" else "deep cut"
        genres = ", ".join((cand.genres if theme else (t["genres"] or cand.genres))[:3]) or "no genre"
        extra = f" | similar to {cand.similar_to}" if cand.similar_to else ""
        lines.append(f"{t['id']} | {kind} | {t['artist']} - {t['title']} ({t['album']}, {t['year'] or '?'}) | "
                     f"{genres}{extra}")
    track_lines = "\n".join(lines)
    artist_reasons = "\n".join(f"- {c.name}{' (' + c.album + ')' if c.album else ''}: {reasons[c.id]}"
                               for c in chosen if reasons.get(c.id)) or "(none)"
    count = sum(quotas.values())
    theme_text = (theme.prompt_text() + "\nKeep the whole playlist inside this theme; title it after the theme.\n\n"
                  if theme else "")
    per_artist_rule = ("exactly one track per artist - never two songs by the same artist (featured credits count "
                       "as that artist)" if max_per_artist == 1 else f"at most {max_per_artist} per artist")
    return f"""The listener's taste:
{profile.describe()}

{theme_text}Why each artist was picked:
{artist_reasons}

Never-played tracks from their library (id | kind | artist - title (album, year) | genres | similarity):
{track_lines}

Build this week's playlist of exactly {count} tracks - {quotas.get("new", 0)} by new artists and
{quotas.get("deep", 0)} deep cuts:
- the strongest, most characteristic songs for this listener; {per_artist_rule};
- ordered as a satisfying listen (an opening that draws them in, a varied middle, a strong close);
- one short, specific reason per track (sound, mood, or link to an artist they love), consistent with the genres
  shown - do not invent a sound the genres contradict.

Return JSON: {{"title": "a short evocative title for this week's mix", "intro": "2-3 sentences introducing the
week", "tracks": [{{"id": "T3", "reason": "..."}}]}}"""


def _parse(llm, response) -> Any:
    data = llm.parse_json_response(response)
    if not isinstance(data, dict):
        raise ValueError("the AI did not return a JSON object")
    return data


def split_quotas(count: int, new_share: float, available: dict[str, int]) -> dict[str, int]:
    """Tracks per kind ("new" / "deep"), shifted to the other kind when one has too few candidates."""
    new = min(round(count * new_share), available.get("new", 0))
    deep = min(count - new, available.get("deep", 0))
    new = min(count - deep, available.get("new", 0))
    return {"new": new, "deep": deep}


def select_tracks(picked: list[dict[str, Any]], tracks: list[dict[str, Any]], quotas: dict[str, int],
                  fallback_order: list[str], max_per_artist: int = 1) -> list[tuple[dict[str, Any], str]]:
    """Map the AI's picks to tracks (ids only, max per artist, per-kind quotas, no repeats), then top up.

    The playlist keeps the AI's order; top-up tracks go at the end.
    """
    count = sum(quotas.values())
    by_id = {t["id"]: t for t in tracks}
    per_artist: Counter = Counter()
    per_kind: Counter = Counter()
    used: set[str] = set()
    result: list[tuple[dict[str, Any], str]] = []

    def take(t: dict[str, Any], reason: str) -> None:
        used.add(t["id"])
        per_artist[t["artist_key"]] += 1
        per_kind[t["kind"]] += 1
        result.append((t, reason))

    def fits(t: dict[str, Any]) -> bool:
        return (t["id"] not in used and per_artist[t["artist_key"]] < max_per_artist
                and per_kind[t["kind"]] < quotas.get(t["kind"], 0))

    for item in picked:
        if len(result) >= count:
            return result
        t = by_id.get(str(item.get("id", "")).strip()) if isinstance(item, dict) else None
        if t and fits(t):
            take(t, str(item.get("reason") or "").strip())
    # Top up: one track per artist in the order the AI ranked the artists, then a second round
    for _ in range(max_per_artist):
        for artist_id in fallback_order:
            if len(result) >= count:
                return result
            spare = next((t for t in tracks if t["artist_id"] == artist_id and fits(t)), None)
            if spare:
                take(spare, "")
    return result


# =============================================================================
# Run
# =============================================================================

MIN_THEME_POOL = 50        # never-played artists a theme needs (with 30 tracks, one per artist)
THEME_POOL_FACTOR = 1.5    # ... and at least this many times the week's new-artist slots


def pick_theme(llm: Any, view: LibraryView, profile: Profile, lastfm: LastFmClient, settings: Settings,
               requested: str | None, recent: list[str], budget: int, rng: random.Random,
               say: Callable[[str], None]) -> tuple[Theme | None, dict[str, float], dict[str, list], Any]:
    """Choose this week's theme and return (theme, fit per artist key, Last.fm tags per key, AI response).

    Tags come from Last.fm (cached for 60 days). Themes are tried best first; the first whose never-played pool is
    big enough wins. No usable theme -> (None, ...) and the week runs without one.
    """
    names = view.artist_names
    weights: dict[str, float] = {}
    for key, plays in view.artist_plays.items():
        if key not in SKIP_ARTISTS:
            weights[key] = view.artist_recent.get(key, 0) * 3 + min(plays, 200)
    try:
        for name, plays in lastfm.top_artists("3month", 50):
            key = _artist_key(name)
            names.setdefault(key, name)
            weights[key] = weights.get(key, 0) + plays
    except LastFmError as e:
        logger.info("Discover Weekly: no Last.fm 3-month artists (%s)", e)
    lane_keys = [k for k, _ in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:LANE_ARTISTS]]
    candidate_keys = [k for k in view.unplayed if k not in SKIP_ARTISTS]
    say(f"Last.fm tags for {len(set(lane_keys) | set(candidate_keys))} artists (cached after the first run)")
    tags_by_key = fetch_tags(lastfm, [names[k] for k in dict.fromkeys(lane_keys + candidate_keys)],
                             _artist_key, _primary_artist, say)
    specificity = tag_specificity(tags_by_key)
    lanes = listening_lanes({k: weights[k] for k in lane_keys}, names, tags_by_key, specificity=specificity)
    if not lanes:
        return None, {}, tags_by_key, None
    vocabulary = {t for tags in tags_by_key.values() for t, _ in tags}
    known = {names[k].lower(): names[k] for k in lane_keys}
    themes, response = choose_themes(llm, lanes, recent, requested, vocabulary, known, tag_share(tags_by_key))
    # never-played artists the week needs (new-artist slots / per-artist cap), with room for the AI to choose
    needed_new = math.ceil(settings.track_count * settings.new_artist_share / settings.max_per_artist)
    need = max(MIN_THEME_POOL, math.ceil(needed_new * THEME_POOL_FACTOR))
    for theme in themes[:6]:
        anchor_match: dict[str, float] = {}
        for anchor in theme.anchors:
            try:
                for name, match in lastfm.similar_artists(_primary_artist(anchor), 100):
                    key = _artist_key(name)
                    anchor_match[key] = max(anchor_match.get(key, 0.0), match)
            except LastFmError:
                pass
        fit = {k: theme_fit(tags_by_key.get(k, []), theme, anchor_match.get(k, 0.0), specificity)
               for k in candidate_keys}
        pool = artist_candidates(view, profile, budget, settings.new_artist_share, rng, fit_by_key=fit,
                                 tags_by_key=tags_by_key)
        n_new = sum(1 for c in pool if c.kind == "new")
        say(f"theme '{theme.name}' ({', '.join(theme.tags)}): {n_new} new artists, "
            f"{len(pool) - n_new} unplayed albums fit")
        if n_new >= need:
            return theme, fit, tags_by_key, response
    say("no theme has enough unplayed music this week - running without a theme")
    return None, {}, tags_by_key, response


def _fallback_reason(cand: ArtistCandidate | None, theme: Theme | None) -> str:
    """A short reason for a track the AI didn't explain (topped up from the ranking)."""
    if not cand:
        return ""
    styles = ", ".join(cand.genres[:2])
    if cand.kind == "deep":
        base = f"An unplayed album by {cand.name}, an artist you already play"
    elif cand.similar_to:
        base = f"Last.fm puts {cand.name} close to {cand.similar_to}"
    else:
        base = f"{cand.name}, new to you"
    tail = f" - {styles}" if styles else ""
    return f"{base}{tail}{', for this week' + chr(39) + 's ' + theme.name if theme else ''}."


def _lastfm_client(settings: Settings) -> LastFmClient | None:
    return LastFmClient(settings.lastfm_api_key, settings.lastfm_user) if settings.lastfm_enabled else None


def generate(settings: Settings, media_client: Any, now: datetime | None = None,
             rng: random.Random | None = None, on_progress: Callable[[str], None] | None = None,
             theme_request: str | None = None) -> dict[str, Any]:
    """Build this week's playlist and publish it. Returns the run's fields (raises on failure)."""
    rng = rng or random.Random()
    say = on_progress or (lambda msg: logger.info("Discover Weekly: %s", msg))
    llm = get_llm_client()
    if not llm:
        raise RuntimeError("no AI provider configured")
    if not library_cache.has_cached_tracks():
        raise RuntimeError("the library cache is empty")

    slot = last_scheduled_at(settings, now)
    previous = _previous_picks(before=slot)
    say("reading the library")
    view = load_library(now, watch_keys={p["rating_key"] for p in previous if p.get("rating_key")})
    lastfm = _lastfm_client(settings)
    say("building the taste profile" + (" (with Last.fm)" if lastfm else ""))
    profile = build_profile(view, settings, lastfm, previous)

    context = get_model_context_limit(llm.config.model_analysis, llm.config)
    budget = max(100, min(2500, int(context * 0.5) // TOKENS_PER_LINE))
    theme, fit, tags_by_key, resp_theme = None, None, None, None
    requested = (theme_request or settings.next_theme or "").strip() or None
    if lastfm and (settings.theme_mode == "auto" or requested):
        recent = [r["theme"]["name"] for r in list_runs(limit=RECENT_THEMES + 5, status="success")
                  if r.get("theme") and datetime.fromisoformat(r["started_at"]) < slot][:RECENT_THEMES]
        theme, fit, tags_by_key, resp_theme = pick_theme(llm, view, profile, lastfm, settings, requested, recent,
                                                         budget, rng, say)
        if not theme:
            fit = None
    candidates = artist_candidates(view, profile, budget, settings.new_artist_share, rng, fit_by_key=fit,
                                   tags_by_key=tags_by_key)
    if len(candidates) < STAGE_A_ARTISTS:   # small library: artists from recent weeks may come back
        candidates = artist_candidates(view, profile, budget, settings.new_artist_share, rng, exclude_recent=False,
                                       fit_by_key=fit, tags_by_key=tags_by_key)
    if not candidates:
        raise RuntimeError("no unplayed artists left to choose from")
    # enough artists that the playlist fills up with max_per_artist tracks each, plus spares
    stage_a = max(STAGE_A_ARTISTS, math.ceil(settings.track_count / settings.max_per_artist * 1.4))
    n_new = round(stage_a * settings.new_artist_share)
    say(f"stage A: {len(candidates)} candidate artists/albums, picking {stage_a}")
    resp_a = llm.analyze(_stage_a_prompt(profile, candidates, n_new, stage_a - n_new, theme), SYSTEM)
    data_a = _parse(llm, resp_a)
    by_id = {c.id: c for c in candidates}
    reasons: dict[str, str] = {}
    chosen: list[ArtistCandidate] = []
    for item in data_a.get("artists") or []:
        cid = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
        if cid in by_id and cid not in reasons:
            chosen.append(by_id[cid])
            reasons[cid] = str(item.get("reason") or "").strip()
    if len(chosen) < min(5, len(candidates)):
        raise RuntimeError(f"the AI picked only {len(chosen)} usable artists")
    n_ai = len(chosen)
    chosen = top_up_artists(chosen, candidates, {"new": n_new, "deep": stage_a - n_new})
    say(f"stage A: the AI picked {n_ai}, topped up {len(chosen) - n_ai} from the ranking")

    tracks = track_candidates(chosen, lastfm, rng)
    say(f"stage B: {len(tracks)} candidate tracks from {len(chosen)} artists/albums")
    # what each kind can supply under the per-artist cap (distinct main artists x cap, at most its tracks)
    artists_by_kind: dict[str, set[str]] = defaultdict(set)
    for t in tracks:
        artists_by_kind[t["kind"]].add(t["artist_key"])
    available = {kind: min(n, len(artists_by_kind[kind]) * settings.max_per_artist)
                 for kind, n in Counter(t["kind"] for t in tracks).items()}
    quotas = split_quotas(settings.track_count, settings.new_artist_share, available)
    resp_b = llm.generate(_stage_b_prompt(profile, chosen, reasons, tracks, quotas, settings.max_per_artist, theme),
                          SYSTEM)
    data_b = _parse(llm, resp_b)
    selected = select_tracks(data_b.get("tracks") or [], tracks, quotas, [c.id for c in chosen],
                             settings.max_per_artist)
    if not selected:
        raise RuntimeError("the AI returned no usable tracks")
    if settings.order_by_sound:
        say("ordering by sound")
        try:
            by_key = {t["rating_key"]: (t, reason) for t, reason in selected}
            ordered, stats = order_by_sound(media_client, list(by_key))
            selected = [by_key[k] for k in ordered]
            say(f"sound ordering: {stats}")
        except Exception as e:      # ordering is a refinement; keep the AI's order
            logger.warning("Discover Weekly: ordering by sound failed, keeping the AI's order: %s", e)

    local = (now or _now()).astimezone()
    week = f"{local.day} {local:%b %Y}"
    title = str(data_b.get("title") or data_b.get("playlist_title") or data_b.get("name") or "").strip()
    title = title or f"Week of {week}"
    intro = str(data_b.get("intro") or data_b.get("description") or data_b.get("narrative") or "").strip()
    chosen_by_id = {c.id: c for c in chosen}
    picks = []
    for t, reason in selected:
        artist_reason = reasons.get(t["artist_id"], "") or _fallback_reason(chosen_by_id.get(t["artist_id"]), theme)
        picks.append({
            "rating_key": t["rating_key"], "artist": t["artist"], "title": t["title"], "album": t["album"],
            "year": t["year"], "kind": t["kind"], "reason": reason or artist_reason,
        })
    theme_part = f" - this week: {theme.name}" if theme else ""
    description = f"{title}{theme_part} - week of {week}. {intro}".strip()
    if settings.archive_title:
        say("archiving last week's playlist")
        archive_previous(media_client, settings, now)
    say(f"publishing {len(picks)} tracks")
    playlist_id = publish(media_client, settings.playlist_title, [p["rating_key"] for p in picks], description)

    model_tracks = [Track(rating_key=t["rating_key"], title=t["title"], artist=t["artist"], album=t["album"],
                          duration_ms=t["duration_ms"] or 0, year=t["year"], genres=t["genres"],
                          art_url=f"/api/art/{t['rating_key']}") for t, _ in selected]
    responses = [r for r in (resp_theme, resp_a, resp_b) if r is not None]
    tokens = sum(r.total_tokens for r in responses)
    cost = sum(r.estimated_cost() for r in responses)
    snapshot = GenerateResponse(
        tracks=model_tracks, token_count=tokens, estimated_cost=cost, playlist_title=title, narrative=intro,
        track_reasons={p["rating_key"]: p["reason"] for p in picks if p["reason"]},
    )
    result_id = None
    try:
        result_id = library_cache.save_result(
            result_type=RESULT_TYPE, title=title, prompt=f"{settings.playlist_title} - week of {week}",
            snapshot=snapshot.model_dump(mode="json"), track_count=len(picks),
            art_rating_key=picks[0]["rating_key"],
            subtitle=f"{settings.playlist_title} · {(theme.name + ' · ') if theme else ''}{week} · {len(picks)} tracks",
        )
    except Exception as e:      # history is a nicety; the playlist is already published
        logger.warning("Discover Weekly: could not save to history: %s", e)
    return {"playlist_id": playlist_id, "title": title, "intro": intro, "picks": picks, "result_id": result_id,
            "tokens": tokens, "theme": theme.to_dict() if theme else None}


def _archive_playlist_id() -> str | None:
    conn = library_cache.ensure_db_initialized()
    try:
        _ensure_tables(conn)
        row = conn.execute("SELECT archive_playlist_id FROM discover_weekly_runs WHERE archive_playlist_id IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def archive_previous(media_client: Any, settings: Settings, now: datetime | None = None) -> int:
    """Before an earlier week's playlist is replaced, append its tracks to the archive playlist (created when missing).

    The tracks come from the media server's playlist as it is now, so tracks the listener removed during the week
    stay out; the run's saved picks are the fallback. A regeneration in the same week archives nothing. Raises when the
    archive can't be written, so the old week is never replaced before it is kept. Returns the tracks handed over.
    """
    prev = next((r for r in list_runs(limit=5, status="success") if r["playlist_id"]), None)
    if not prev or prev.get("archived_at"):
        return 0
    if datetime.fromisoformat(prev["started_at"]) >= last_scheduled_at(settings, now):
        return 0        # this week's playlist (a regeneration replaces it) - not an earlier week
    keys = None
    getter = getattr(media_client, "get_playlist_track_keys", None)
    if getter:
        keys = getter(prev["playlist_id"])
    if keys is None:
        keys = [p["rating_key"] for p in prev["picks"]]
    archive_id = None
    if keys:
        description = f"Every earlier {settings.playlist_title} playlist, oldest first."
        archive_id = _archive_playlist_id()
        result = (media_client.update_playlist(archive_id, keys, mode="append", description=description)
                  if archive_id else {"success": False})
        if not result.get("success"):
            created = media_client.create_playlist(settings.archive_title, keys, description)
            if not created.get("success"):
                raise RuntimeError(f"could not write the archive playlist: {created.get('error')}")
            archive_id = str(created.get("playlist_id"))
    conn = library_cache.ensure_db_initialized()
    try:
        conn.execute("UPDATE discover_weekly_runs SET archived_at = ?, archive_playlist_id = ? WHERE id = ?",
                     (_now().isoformat(), archive_id, prev["id"]))
        conn.commit()
    finally:
        conn.close()
    logger.info("Discover Weekly: archived %d tracks of '%s' to playlist %s", len(keys), prev.get("title"), archive_id)
    return len(keys)


def publish(media_client: Any, playlist_title: str, rating_keys: list[str], description: str) -> str:
    """Replace the tracks of the Discover Weekly playlist, creating it when it doesn't exist (any more)."""
    previous_id = next((r["playlist_id"] for r in list_runs(limit=5, status="success") if r["playlist_id"]), None)
    if previous_id:
        result = media_client.update_playlist(previous_id, rating_keys, mode="replace", description=description)
        if result.get("success"):
            return previous_id
        logger.info("Discover Weekly: previous playlist %s not updated (%s) - creating a new one",
                    previous_id, result.get("error"))
    result = media_client.create_playlist(playlist_title, rating_keys, description)
    if not result.get("success"):
        raise RuntimeError(f"could not create the playlist: {result.get('error')}")
    return str(result.get("playlist_id"))


def run(media_client: Any, trigger: str = "schedule", theme: str | None = None) -> dict[str, Any]:
    """Run once (blocking). Only one run at a time; the outcome is recorded either way.

    theme: the listener's wish for this run's theme (otherwise the saved next_theme, otherwise automatic).
    """
    global _running
    with _run_lock:
        if _running:
            return {"success": False, "error": "Discover Weekly is already running"}
        _running = True
    run_id = _record_start(trigger)
    _last_attempt.update(at=_now(), error=None)
    try:
        settings = load_settings()
        if not media_client or not media_client.is_connected():
            raise RuntimeError("media server not connected")
        out = generate(settings, media_client, theme_request=theme)
        _record_end(run_id, status="success", playlist_id=out["playlist_id"], title=out["title"],
                    intro=out["intro"], picks=out["picks"], result_id=out["result_id"], theme=out["theme"])
        if settings.next_theme and not theme:
            save_settings({"next_theme": ""})      # a wished-for theme is used once
        logger.info("Discover Weekly: published '%s' (%d tracks, %d tokens)", out["title"], len(out["picks"]),
                    out["tokens"])
        return {"success": True, **out}
    except Exception as e:
        logger.exception("Discover Weekly failed")
        _last_attempt["error"] = str(e)
        _record_end(run_id, status="error", error=str(e))
        return {"success": False, "error": str(e)}
    finally:
        with _run_lock:
            _running = False


def startup() -> None:
    _mark_interrupted_runs()
