"""Tests for Discover Weekly (settings, schedule, candidates, selection, a full run with a fake AI)."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from backend import discover_weekly as dw
from backend import library_cache
from backend.llm_client import LLMResponse


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(library_cache, "DB_PATH", tmp_path / "library_cache.db")
    monkeypatch.setattr(library_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(library_cache, "_schema_initialized", False)
    library_cache.ensure_db_initialized().close()
    monkeypatch.setattr(dw, "_running", False)
    monkeypatch.setattr(dw, "_last_attempt", {"at": None, "error": None})
    return tmp_path


@pytest.fixture
def user_yaml(monkeypatch):
    """In-memory config.user.yaml for the settings functions."""
    store: dict = {}

    def save(updates):
        for key, value in updates.items():
            store.setdefault(key, {}).update(value) if isinstance(value, dict) else store.update({key: value})

    monkeypatch.setattr(dw, "load_user_yaml_config", lambda: store)
    monkeypatch.setattr(dw, "save_user_config", save)
    for env in dw.ENV.values():
        monkeypatch.delenv(env, raising=False)
    return store


def add_tracks(rows):
    """rows: (key, title, artist, album, year, genres, view_count, user_rating, is_live, last_viewed_at)"""
    conn = library_cache.get_db_connection()
    conn.executemany(
        "INSERT INTO tracks (rating_key, title, artist, album, duration_ms, year, genres, user_rating, is_live, "
        "view_count, last_viewed_at) VALUES (?, ?, ?, ?, 200000, ?, ?, ?, ?, ?, ?)",
        [(k, t, a, al, y, json.dumps(g), r, live, v, last) for k, t, a, al, y, g, v, r, live, last in rows],
    )
    conn.execute("UPDATE sync_state SET track_count = ? WHERE id = 1", (len(rows),))
    conn.commit()
    conn.close()


def sample_library():
    recent = datetime.now(timezone.utc).isoformat()
    rows = []
    # Played: Radiohead (rock), Bicep (electronic)
    for i in range(3):
        rows.append((f"rh{i}", f"RH {i}", "Radiohead", "OK Computer", 1997, ["Rock"], 10, None, False, recent))
        rows.append((f"bi{i}", f"Bicep {i}", "Bicep", "Isles", 2021, ["Electronic"], 5, None, False, recent))
    # Radiohead album never played -> deep cut
    for i in range(4):
        rows.append((f"kida{i}", f"Kid A {i}", "Radiohead", "Kid A", 2000, ["Rock"], 0, None, False, None))
    # New artists, never played
    for artist, genre in [("Overmono", "Electronic"), ("Portishead", "Trip Hop"), ("Slowdive", "Rock"),
                          ("Four Tet", "Electronic"), ("Burial", "Electronic"), ("Mogwai", "Rock")]:
        for i in range(3):
            rows.append((f"{artist}{i}", f"{artist} song {i}", artist, f"{artist} LP", 2010, [genre], 0, None,
                         False, None))
    # Skipped: compilations, live tracks, a credit that includes a known artist
    rows.append(("va1", "Comp", "Various Artists", "Comp", 2000, ["Pop"], 0, None, False, None))
    rows.append(("live1", "Live", "Mogwai", "Live at X", 2005, ["Rock"], 0, None, True, None))
    rows.append(("feat1", "Collab", "Bicep;Clara La San", "Collab", 2022, ["Electronic"], 0, None, False, None))
    return rows


# ---------------------------------------------------------------------------- settings


class TestSettings:
    def test_defaults_off(self, user_yaml):
        s = dw.load_settings()
        assert s.enabled is False and s.day == "monday" and s.track_count == 30 and not s.lastfm_enabled

    def test_save_and_reload(self, user_yaml):
        dw.save_settings({"enabled": True, "day": "Sunday", "time": "21:30", "track_count": 20,
                          "lastfm_user": "me", "lastfm_api_key": "k"})
        s = dw.load_settings()
        assert (s.enabled, s.day, s.time, s.track_count, s.lastfm_enabled) == (True, "sunday", "21:30", 20, True)
        assert s.public()["has_lastfm_api_key"] is True and "lastfm_api_key" not in s.public()

    def test_env_overrides_and_locks(self, user_yaml, monkeypatch):
        monkeypatch.setenv("DISCOVER_WEEKLY_ENABLED", "true")
        monkeypatch.setenv("DISCOVER_WEEKLY_TRACKS", "40")
        dw.save_settings({"track_count": 10})
        s = dw.load_settings()
        assert s.enabled is True and s.track_count == 40
        assert set(s.env_locked) == {"enabled", "track_count"}

    def test_invalid_values_rejected(self, user_yaml):
        with pytest.raises(ValueError):
            dw.save_settings({"day": "someday"})
        with pytest.raises(ValueError):
            dw.save_settings({"time": "25:99"})


# ---------------------------------------------------------------------------- schedule


class TestSchedule:
    def test_last_and_next_slot(self):
        s = dw.Settings(enabled=True, day="monday", time="06:00")
        now = datetime(2026, 10, 15, 12, 0).astimezone()          # a Thursday
        slot = dw.last_scheduled_at(s, now)
        assert slot.weekday() == 0 and (slot.hour, slot.minute) == (6, 0) and slot <= now
        assert now - slot < timedelta(days=7)
        assert dw.next_scheduled_at(s, now) - slot == timedelta(days=7)

    def test_slot_later_today_counts_last_week(self):
        s = dw.Settings(enabled=True, day="thursday", time="23:00")
        now = datetime(2026, 10, 15, 12, 0).astimezone()          # Thursday noon
        assert dw.last_scheduled_at(s, now).date() == (now - timedelta(days=7)).date()

    def test_due_until_success(self, db):
        s = dw.Settings(enabled=True)
        assert dw.is_due(s)
        run_id = dw._record_start("test")
        dw._record_end(run_id, status="success", picks=[])
        assert not dw.is_due(s)

    def test_disabled_never_due(self, db):
        assert not dw.is_due(dw.Settings(enabled=False))

    def test_backoff_after_failure(self, db, monkeypatch):
        s = dw.Settings(enabled=True)
        monkeypatch.setattr(dw, "_last_attempt", {"at": dw._now(), "error": "boom"})
        assert not dw.is_due(s)
        monkeypatch.setattr(dw, "_last_attempt", {"at": dw._now() - timedelta(hours=2), "error": "boom"})
        assert dw.is_due(s)

    def test_interrupted_runs_marked(self, db):
        dw._record_start("schedule")
        dw.startup()
        assert dw.list_runs(1)[0]["status"] == "error"


# ---------------------------------------------------------------------------- candidates and selection


class TestCandidates:
    def test_new_deep_and_exclusions(self, db):
        add_tracks(sample_library())
        view = dw.load_library()
        profile = dw.build_profile(view, dw.Settings(), None, [])
        cands = dw.artist_candidates(view, profile, 100, 0.7, random.Random(0))
        new = {c.name for c in cands if c.kind == "new"}
        deep = {(c.name, c.album) for c in cands if c.kind == "deep"}
        assert new == {"Overmono", "Portishead", "Slowdive", "Four Tet", "Burial", "Mogwai"}
        assert deep == {("Radiohead", "Kid A")}                    # OK Computer has plays
        all_tracks = [t["rating_key"] for c in cands for t in c.tracks]
        assert "va1" not in all_tracks and "live1" not in all_tracks
        assert "feat1" not in all_tracks                           # credit includes Bicep (known), 1-track album

    def test_blocked_and_recent_artists_excluded(self, db):
        add_tracks(sample_library())
        view = dw.load_library(watch_keys={"Burial0"})
        previous = [{"rating_key": "Overmono0", "artist": "Overmono"}]
        profile = dw.build_profile(view, dw.Settings(), None, previous)
        names = {c.name for c in dw.artist_candidates(view, profile, 100, 0.7, random.Random(0))}
        assert "Overmono" not in names                             # picked in the last weeks

    def test_feedback(self, db):
        rows = sample_library()
        rows = [r if r[0] != "Burial0" else (*r[:6], 3, 2, False, None) for r in rows]   # played, rated 1 star
        rows = [r if r[0] != "Slowdive0" else (*r[:6], 2, None, False, None) for r in rows]  # played
        add_tracks(rows)
        previous = [{"rating_key": "Burial0", "artist": "Burial"}, {"rating_key": "Slowdive0", "artist": "Slowdive"}]
        view = dw.load_library(watch_keys={"Burial0", "Slowdive0"})
        profile = dw.build_profile(view, dw.Settings(), None, previous)
        assert "burial" in profile.blocked
        assert profile.liked_picks == ["Slowdive"]


def _tracks(spec):
    """spec: list of (artist_id, artist_key, kind, n)"""
    out = []
    for artist_id, key, kind, n in spec:
        for i in range(n):
            out.append({"id": f"T{len(out) + 1}", "artist_id": artist_id, "artist_key": key, "kind": kind,
                        "rating_key": f"{artist_id}-{i}"})
    return out


class TestSelection:
    def test_one_per_artist_by_default(self):
        tracks = _tracks([("A1", "x", "new", 3), ("A2", "y", "new", 3), ("A3", "x", "deep", 3), ("A4", "z", "deep", 3)])
        picked = [{"id": t["id"]} for t in tracks]
        out = dw.select_tracks(picked, tracks, {"new": 3, "deep": 2}, ["A1", "A2", "A3", "A4"])
        keys = [t["artist_key"] for t, _ in out]
        assert len(keys) == len(set(keys)) == 3                    # x, y, z once each; no fourth artist exists

    def test_featured_credit_counts_as_main_artist(self, db):
        cand = dw.ArtistCandidate("A1", "sampha", "Sampha", "new", None, [], "2010s", [
            {"rating_key": "1", "title": "One", "artist": "Sampha", "album": "P", "year": 2017, "genres": []},
            {"rating_key": "2", "title": "Two", "artist": "Sampha;Little Simz", "album": "P", "year": 2017,
             "genres": []},
        ])
        tracks = dw.track_candidates([cand], None, random.Random(0))
        assert {t["artist_key"] for t in tracks} == {"sampha"}
        out = dw.select_tracks([{"id": t["id"]} for t in tracks], tracks, {"new": 2, "deep": 0}, ["A1"])
        assert len(out) == 1

    def test_quotas_split(self):
        assert dw.split_quotas(30, 0.7, {"new": 100, "deep": 100}) == {"new": 21, "deep": 9}
        assert dw.split_quotas(30, 0.7, {"new": 100, "deep": 3}) == {"new": 27, "deep": 3}
        assert dw.split_quotas(30, 0.7, {"new": 5, "deep": 100}) == {"new": 5, "deep": 25}

    def test_ai_picks_respect_caps_and_ignore_junk(self):
        tracks = _tracks([("A1", "x", "new", 4), ("A2", "y", "new", 4), ("A3", "x", "deep", 4)])
        picked = [{"id": "T1"}, {"id": "T2"}, {"id": "T3"}, {"id": "T99"}, "junk", {"id": "T1"},
                  {"id": "T9", "reason": "deep"}, {"id": "T5", "reason": "y"}]
        out = dw.select_tracks(picked, tracks, {"new": 3, "deep": 1}, ["A1", "A2", "A3"], max_per_artist=2)
        ids = [t["id"] for t, _ in out]
        assert ids[:2] == ["T1", "T2"]                             # third A1 track exceeds the artist cap
        assert "T9" not in ids                                     # artist x (via A1) is already at its cap
        assert len(out) == 3 or all(t["artist_key"] != "x" for t, _ in out[2:])
        assert sum(1 for t, _ in out if t["artist_key"] == "x") <= 2

    def test_top_up_when_ai_returns_too_few(self):
        tracks = _tracks([("A1", "a", "new", 3), ("A2", "b", "new", 3), ("A3", "c", "deep", 3)])
        out = dw.select_tracks([{"id": "T1", "reason": "r"}], tracks, {"new": 4, "deep": 1}, ["A1", "A2", "A3"],
                               max_per_artist=2)
        assert len(out) == 5
        assert out[0] == (tracks[0], "r")
        assert sum(1 for t, _ in out if t["kind"] == "deep") == 1


# ---------------------------------------------------------------------------- full run


class FakeLLM:
    """Stage A: picks every candidate; stage B: returns ids in reverse with reasons."""

    def __init__(self, config):
        self.config = config
        self.prompts = []

    def _resp(self, content):
        return LLMResponse(content=content, input_tokens=100, output_tokens=50, model="fake")

    def analyze(self, prompt, system):
        self.prompts.append(prompt)
        ids = [line.split(" | ")[0] for line in prompt.splitlines() if line.startswith("A")]
        return self._resp(json.dumps({"artists": [{"id": i, "reason": f"why {i}"} for i in ids]}))

    def generate(self, prompt, system):
        self.prompts.append(prompt)
        ids = [line.split(" | ")[0] for line in prompt.splitlines() if line.startswith("T")]
        return self._resp(json.dumps({"title": "Test Week", "intro": "Hello.",
                                      "tracks": [{"id": i, "reason": f"track {i}"} for i in reversed(ids)]}))

    def parse_json_response(self, response):
        return json.loads(response.content)


class FakeMedia:
    def __init__(self, update_ok=True):
        self.update_ok = update_ok
        self.created, self.updated = [], []

    def is_connected(self):
        return True

    def create_playlist(self, name, keys, description=""):
        self.created.append((name, keys, description))
        return {"success": True, "playlist_id": f"pl{len(self.created)}"}

    def update_playlist(self, playlist_id, keys, mode="replace", description=""):
        self.updated.append((playlist_id, keys, mode))
        return {"success": self.update_ok, "error": None if self.update_ok else "not found"}


@pytest.fixture
def fake_llm(monkeypatch):
    from backend.models import LLMConfig
    llm = FakeLLM(LLMConfig(provider="custom", model_analysis="m", model_generation="m", custom_context_window=8000))
    monkeypatch.setattr(dw, "get_llm_client", lambda: llm)
    return llm


class TestRun:
    def test_first_run_creates_then_replaces(self, db, user_yaml, fake_llm):
        add_tracks(sample_library())
        dw.save_settings({"enabled": True, "track_count": 8, "max_per_artist": 2})
        media = FakeMedia()

        first = dw.run(media, "manual")
        assert first["success"], first
        assert len(first["picks"]) == 8
        assert media.created[0][0] == "Discover Weekly"
        assert all(p["reason"] for p in first["picks"])
        assert {p["kind"] for p in first["picks"]} == {"new", "deep"}
        assert not any(k in {p["rating_key"] for p in first["picks"]} for k in ("va1", "live1", "rh0"))
        assert dw.status()["playlist_id"] == "pl1"
        results, _ = library_cache.list_results(result_type="discover_weekly")
        assert results and results[0]["title"] == "Test Week"

        second = dw.run(media, "manual")
        assert second["success"], second
        assert media.updated[-1][0] == "pl1" and media.updated[-1][2] == "replace"
        assert len(media.created) == 1

    def test_recreates_playlist_deleted_on_the_server(self, db, user_yaml, fake_llm):
        add_tracks(sample_library())
        dw.save_settings({"enabled": True, "track_count": 5})
        assert dw.run(FakeMedia(), "manual")["success"]
        media = FakeMedia(update_ok=False)
        out = dw.run(media, "manual")
        assert out["success"] and media.created and out["playlist_id"] == "pl1"

    def test_failure_is_recorded(self, db, user_yaml, monkeypatch):
        add_tracks(sample_library())
        monkeypatch.setattr(dw, "get_llm_client", lambda: None)
        out = dw.run(FakeMedia(), "manual")
        assert out["success"] is False and "AI provider" in out["error"]
        assert dw.list_runs(1)[0]["status"] == "error"
        assert dw._last_attempt["error"]

    def test_one_per_artist_run(self, db, user_yaml, fake_llm):
        add_tracks(sample_library())
        dw.save_settings({"enabled": True, "track_count": 7})
        out = dw.run(FakeMedia(), "manual")
        assert out["success"], out
        artists = [p["artist"] for p in out["picks"]]
        assert len(artists) == len(set(artists)) == 7               # 6 new artists + Radiohead's deep cut

    def test_disconnected_media_server(self, db, user_yaml, fake_llm):
        class Down(FakeMedia):
            def is_connected(self):
                return False
        assert dw.run(Down(), "manual")["success"] is False


class TestTopUp:
    def test_fills_each_kind_with_best_ranked_unpicked(self):
        def c(i, key, kind):
            return dw.ArtistCandidate(f"A{i}", key, key, kind, None, [], "", [])
        cands = [c(1, "a", "new"), c(2, "b", "new"), c(3, "c", "new"), c(4, "d", "deep"), c(5, "a", "deep"),
                 c(6, "e", "deep")]
        out = dw.top_up_artists([cands[1]], cands, {"new": 2, "deep": 2})
        assert [x.id for x in out] == ["A2", "A1", "A4", "A6"]      # A5 skipped: artist a already in


class TestRegeneration:
    def test_same_week_picks_are_not_rested(self, db):
        slot = dw.last_scheduled_at(dw.Settings())
        old = dw._record_start("schedule")
        dw._record_end(old, status="success", picks=[{"artist": "Old", "rating_key": "1"}])
        conn = library_cache.get_db_connection()
        conn.execute("UPDATE discover_weekly_runs SET started_at = ? WHERE id = ?",
                     ((slot - timedelta(days=7)).isoformat(), old))
        conn.commit()
        conn.close()
        this_week = dw._record_start("manual")
        dw._record_end(this_week, status="success", picks=[{"artist": "New", "rating_key": "2"}])
        artists = {p["artist"] for p in dw._previous_picks(before=slot)}
        assert artists == {"Old"}

    def test_top_up_prefers_lastfm_similarity(self):
        def c(i, key, match, score):
            return dw.ArtistCandidate(f"A{i}", key, key, "new", None, [], "", [], score, "", match)
        cands = [c(1, "genre-only", 0.0, 9.0), c(2, "similar", 0.8, 1.0), c(3, "less-similar", 0.3, 5.0)]
        out = dw.top_up_artists([], cands, {"new": 2})
        assert [x.key for x in out] == ["similar", "less-similar"]
