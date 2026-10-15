"""Order a playlist so each track flows into the next: similar sound, no big loudness jumps.

Similarity comes from the media server's sonic analysis (Plex): the overlap of two tracks' nearest-neighbour artists.
The first and last track stay where they are (the curator's opening and closing); the tracks in between are
arranged into the cheapest path - greedy nearest neighbour, then improved with 2-opt.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

LOUDNESS_STEP_DB = 6.0      # louder/quieter than this between neighbours starts to cost
LOUDNESS_WEIGHT = 0.05      # cost per dB beyond the step (a 10 dB jump ~ one "unrelated" pair x 0.2)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def transition_cost(a: str, b: str, neighbours: dict[str, set[str]], loudness: dict[str, float]) -> float:
    cost = 1.0 - jaccard(neighbours.get(a, set()), neighbours.get(b, set()))
    if a in loudness and b in loudness:
        cost += LOUDNESS_WEIGHT * max(0.0, abs(loudness[a] - loudness[b]) - LOUDNESS_STEP_DB)
    return cost


def path_cost(order: list[str], cost) -> float:
    return sum(cost(order[i], order[i + 1]) for i in range(len(order) - 1))


def order_keys(keys: list[str], neighbours: dict[str, set[str]], loudness: dict[str, float]) -> list[str]:
    """Reorder keys (first and last fixed) to minimise the summed transition cost."""
    if len(keys) < 4 or sum(1 for k in keys if neighbours.get(k)) < len(keys) // 2:
        return list(keys)       # too short, or too little sonic data to do better than the given order
    memo: dict[tuple[str, str], float] = {}

    def cost(a: str, b: str) -> float:
        if (a, b) not in memo:
            memo[(a, b)] = memo[(b, a)] = transition_cost(a, b, neighbours, loudness)
        return memo[(a, b)]

    start, end = keys[0], keys[-1]
    remaining = list(keys[1:-1])
    order = [start]
    while remaining:
        nxt = min(remaining, key=lambda k: cost(order[-1], k))
        order.append(nxt)
        remaining.remove(nxt)
    order.append(end)

    improved = True
    while improved:      # 2-opt: reverse any inner segment that makes the path cheaper
        improved = False
        for i in range(1, len(order) - 2):
            for j in range(i + 1, len(order) - 1):
                before = cost(order[i - 1], order[i]) + cost(order[j], order[j + 1])
                after = cost(order[i - 1], order[j]) + cost(order[i], order[j + 1])
                if after < before - 1e-9:
                    order[i : j + 1] = reversed(order[i : j + 1])
                    improved = True
    return order


def order_by_sound(media_client: Any, keys: list[str], workers: int = 4) -> tuple[list[str], dict[str, Any]]:
    """Fetch sonic neighbours + loudness from the media server and return (ordered keys, stats).

    Media servers without sonic analysis (no sonic_neighbor_artists) keep the given order.
    """
    if not hasattr(media_client, "sonic_neighbor_artists") or len(keys) < 4:
        return list(keys), {"applied": False, "reason": "no sonic analysis"}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        neighbours = dict(zip(keys, pool.map(media_client.sonic_neighbor_artists, keys)))
    try:
        loudness = media_client.track_loudness(keys)
    except Exception as e:      # loudness is a refinement; ordering by sound alone still helps
        logger.warning("Loudness unavailable for ordering: %s", e)
        loudness = {}
    ordered = order_keys(keys, neighbours, loudness)

    def total(order):
        return path_cost(order, lambda a, b: transition_cost(a, b, neighbours, loudness))

    stats = {"applied": ordered != list(keys), "with_sonic": sum(1 for k in keys if neighbours.get(k)),
             "with_loudness": len(loudness), "cost_before": round(total(keys), 3), "cost_after": round(total(ordered), 3)}
    return ordered, stats
