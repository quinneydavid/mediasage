"""Weekly themes for Discover Weekly: one coherent style lane per week, chosen from what the listener plays.

Library genre tags are usually too broad for this ("Pop/Rock", "Electronic"), so lanes are built from Last.fm artist
tags ("deep house", "trip-hop", "neo-soul"). The AI picks the week's lane from the listener's recent lanes, avoiding the
themes of the last few weeks; candidates then have to fit the theme's tags or be similar to its anchor artists.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from backend.lastfm import LastFmClient, LastFmError

logger = logging.getLogger(__name__)

LANE_ARTISTS = 300          # most-played artists whose tags make up the listening lanes
FIT_MIN = 0.3               # minimum theme fit for a candidate (sum of matching tag weights / 100)
ANCHOR_WEIGHT = 1.0         # fit added per unit of Last.fm similarity to an anchor artist
RECENT_THEMES = 4           # themes of this many earlier weeks are avoided
BROAD_TAG_SHARE = 0.2       # a tag on more than this share of artists is too broad to define a theme on its own
SPECIFIC_IDF = 3.0          # tags at least this rare (idf; ~5 % of artists) count fully, broader ones less


def tag_specificity(tags_by_key: dict[str, list[tuple[str, int]]]) -> dict[str, float]:
    """0-1 per tag: how distinctive it is in this library. "electronic" on a third of all artists says little
    (~0.4); "deep house" or "trip-hop" on 2-3 % says a lot (1.0)."""
    df: Counter = Counter()
    for tags in tags_by_key.values():
        df.update({t for t, _ in tags})
    n = max(1, sum(1 for tags in tags_by_key.values() if tags))
    return {tag: min(1.0, math.log(n / count) / SPECIFIC_IDF) for tag, count in df.items()}


def tag_share(tags_by_key: dict[str, list[tuple[str, int]]]) -> dict[str, float]:
    df: Counter = Counter()
    for tags in tags_by_key.values():
        df.update({t for t, _ in tags})
    n = max(1, sum(1 for tags in tags_by_key.values() if tags))
    return {tag: count / n for tag, count in df.items()}


@dataclass
class Theme:
    name: str
    description: str
    tags: list[str]
    anchors: list[str] = field(default_factory=list)
    source: str = "auto"            # auto | requested | fallback

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Theme | None:
        if not d or not d.get("tags"):
            return None
        return cls(name=str(d.get("name") or ""), description=str(d.get("description") or ""),
                   tags=[str(t) for t in d["tags"]], anchors=[str(a) for a in d.get("anchors") or []],
                   source=str(d.get("source") or "auto"))

    def prompt_text(self) -> str:
        return f"THIS WEEK'S THEME: {self.name} - {self.description} (styles: {', '.join(self.tags)})"


def fetch_tags(lastfm: LastFmClient, names: Iterable[str], key: Callable[[str], str], primary: Callable[[str], str],
               on_progress: Callable[[str], None] | None = None) -> dict[str, list[tuple[str, int]]]:
    """Last.fm tags per artist key (cached for 60 days; the first run for a big library takes a while)."""
    out: dict[str, list[tuple[str, int]]] = {}
    names = list(dict.fromkeys(names))
    for i, name in enumerate(names, 1):
        k = key(name)
        if k in out:
            continue
        try:
            out[k] = lastfm.artist_top_tags(primary(name))
        except LastFmError as e:
            logger.info("No Last.fm tags for %s (%s)", name, e)
            out[k] = []
        if on_progress and i % 250 == 0:
            on_progress(f"Last.fm tags {i}/{len(names)}")
    return out


def listening_lanes(artist_weights: dict[str, float], names: dict[str, str],
                    tags_by_key: dict[str, list[tuple[str, int]]], top: int = 25,
                    specificity: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Style lanes of the listener: tag -> weight (artist listening weight x tag strength x tag specificity) and
    its top artists. Specificity keeps broad tags ("electronic", "indie") from swamping distinct styles."""
    weight: Counter = Counter()
    artists: dict[str, Counter] = {}
    for key, w in artist_weights.items():
        for tag, strength in tags_by_key.get(key, []):
            contribution = w * strength / 100 * (specificity.get(tag, 1.0) if specificity else 1.0)
            weight[tag] += contribution
            artists.setdefault(tag, Counter())[names.get(key, key)] += contribution
    return [{"tag": tag, "weight": round(w, 1), "artists": [a for a, _ in artists[tag].most_common(6)]}
            for tag, w in weight.most_common(top)]


def theme_fit(tags: list[tuple[str, int]], theme: Theme, anchor_match: float = 0.0,
              specificity: dict[str, float] | None = None) -> float:
    """How well an artist fits the theme: matching tag strengths (x specificity) + similarity to an anchor."""
    theme_tags = set(theme.tags)
    spec = specificity or {}
    return (sum(strength * spec.get(tag, 1.0) for tag, strength in tags if tag in theme_tags) / 100
            + ANCHOR_WEIGHT * anchor_match)


THEME_SYSTEM = "You are a music curator planning a personal weekly playlist. Respond with JSON only."


def _theme_prompt(lanes: list[dict[str, Any]], recent: list[str], requested: str | None,
                  vocabulary: list[str]) -> str:
    lane_lines = "\n".join(f"{lane['tag']} | {lane['weight']} | {', '.join(lane['artists'])}" for lane in lanes)
    parts = [f"""The listener's recent listening as style lanes (tag | weight | artists they play in it):
{lane_lines}"""]
    if recent:
        parts.append("Themes of the last weeks (pick something different): " + "; ".join(recent))
    if requested:
        parts.append(f"""The listener asked for this week's theme: "{requested}". Build the themes around that request,
using tags from this vocabulary (exact spelling): {', '.join(vocabulary)}""")
        rule = "built around the listener's request"
    else:
        rule = "a coherent lane from their listening above, weighted toward what they play most lately"
    parts.append(f"""Propose 3 different themes for this week's playlist, best first. Each theme is {rule}: one
style a whole playlist can stay in, made of 2-6 closely related tags (exact spelling from the tags above), plus 2-5
of the listener's artists that best represent it. Prefer specific styles ("deep house", "trip-hop", "neo-soul",
"jazz rap") over broad umbrella tags ("electronic", "indie", "rock", "pop", "dance"), which say little on their own.

Return JSON: {{"themes": [{{"name": "short evocative name", "description": "one sentence on the sound",
"tags": ["tag", "..."], "anchors": ["artist", "..."]}}]}}""")
    return "\n\n".join(parts)


def choose_themes(llm: Any, lanes: list[dict[str, Any]], recent: list[str], requested: str | None,
                  vocabulary: set[str], known_artists: dict[str, str],
                  shares: dict[str, float] | None = None) -> tuple[list[Theme], Any]:
    """Ranked themes from the AI (validated against the tag vocabulary), with a deterministic fallback.

    known_artists maps a lowercased artist name to its display name (anchors must be artists the listener plays).
    Returns (themes, llm response or None).
    """
    vocab_list = [lane["tag"] for lane in lanes] + sorted(vocabulary - {lane["tag"] for lane in lanes})[:200]
    themes: list[Theme] = []
    response = None
    try:
        response = llm.analyze(_theme_prompt(lanes, recent, requested, vocab_list), THEME_SYSTEM)
        data = llm.parse_json_response(response)
        for item in (data.get("themes") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            tags = [t for t in (str(x).strip().lower() for x in item.get("tags") or []) if t in vocabulary]
            specific = [t for t in tags if (shares or {}).get(t, 0) <= BROAD_TAG_SHARE]
            tags = specific or tags     # broad umbrella tags only count when nothing more specific was given
            if not tags:
                continue
            anchors = [known_artists[a.lower()] for a in (str(x).strip() for x in item.get("anchors") or [])
                       if a.lower() in known_artists]
            themes.append(Theme(name=str(item.get("name") or tags[0].title()).strip(),
                                description=str(item.get("description") or "").strip(), tags=list(dict.fromkeys(tags)),
                                anchors=anchors, source="requested" if requested else "auto"))
    except Exception as e:     # the theme is a refinement; fall back to the listener's top lanes
        logger.warning("Theme choice by the AI failed, using the top lanes: %s", e)
    recent_lower = {r.lower() for r in recent}
    for lane in lanes:       # fallbacks: top lanes not used recently
        if lane["tag"] not in recent_lower:
            themes.append(Theme(name=lane["tag"].title(), description=f"{lane['tag']} from your library",
                                tags=[lane["tag"]], anchors=lane["artists"][:3], source="fallback"))
    return themes, response


def themes_json(themes: list[Theme]) -> str:
    return json.dumps([t.to_dict() for t in themes])
