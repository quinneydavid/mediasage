"""Tests for ordering a playlist by sound."""

from backend import sonic_order as so


def neighbours_for(groups):
    """groups: {key: style}; tracks of the same style share neighbour artists."""
    pools = {"house": {"a", "b", "c", "d"}, "ambient": {"x", "y", "z", "w"}, "rap": {"p", "q", "r", "s"}}
    return {k: pools[style] | {f"own-{k}"} for k, style in groups.items()}


def test_groups_similar_tracks_and_keeps_ends():
    styles = {"1": "house", "2": "ambient", "3": "rap", "4": "house", "5": "ambient", "6": "rap", "7": "house"}
    keys = list(styles)
    order = so.order_keys(keys, neighbours_for(styles), {})
    assert order[0] == "1" and order[-1] == "7" and sorted(order) == sorted(keys)
    runs = sum(1 for i in range(len(order) - 1) if styles[order[i]] != styles[order[i + 1]])
    assert runs <= 3                    # styles come in blocks instead of alternating (6 changes before)
    cost = lambda a, b: so.transition_cost(a, b, neighbours_for(styles), {})  # noqa: E731
    assert so.path_cost(order, cost) < so.path_cost(keys, cost)


def test_loudness_breaks_ties():
    styles = {k: "house" for k in "12345"}
    loud = {"1": -8.0, "2": -30.0, "3": -9.0, "4": -29.0, "5": -10.0}
    order = so.order_keys(list(styles), neighbours_for(styles), loud)
    jumps = [abs(loud[order[i]] - loud[order[i + 1]]) for i in range(len(order) - 1)]
    assert sum(j > 15 for j in jumps) <= 2      # quiet tracks grouped, not alternating loud/quiet (4 jumps before)


def test_too_little_sonic_data_keeps_order():
    keys = ["1", "2", "3", "4", "5"]
    assert so.order_keys(keys, {"1": {"a"}}, {}) == keys


class FakePlex:
    def __init__(self, styles):
        self.n = neighbours_for(styles)

    def sonic_neighbor_artists(self, key):
        return self.n[key]

    def track_loudness(self, keys):
        raise RuntimeError("no loudness")


def test_order_by_sound_with_client():
    styles = {"1": "house", "2": "ambient", "3": "house", "4": "ambient", "5": "house"}
    order, stats = so.order_by_sound(FakePlex(styles), list(styles))
    assert stats["applied"] and stats["cost_after"] < stats["cost_before"] and stats["with_loudness"] == 0
    assert order[0] == "1" and order[-1] == "5"


def test_server_without_sonic_analysis():
    order, stats = so.order_by_sound(object(), ["1", "2", "3", "4"])
    assert order == ["1", "2", "3", "4"] and not stats["applied"]
