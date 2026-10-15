"""Tests for weekly themes."""

import json

from backend import themes as th
from backend.llm_client import LLMResponse


def test_lanes_weight_tags_by_listening():
    weights = {"bicep": 100, "dj seinfeld": 50, "radiohead": 30}
    names = {"bicep": "Bicep", "dj seinfeld": "DJ Seinfeld", "radiohead": "Radiohead"}
    tags = {"bicep": [("house", 100), ("electronic", 80)], "dj seinfeld": [("house", 100), ("lo-fi house", 90)],
            "radiohead": [("alternative", 100), ("electronic", 30)]}
    lanes = th.listening_lanes(weights, names, tags)
    assert lanes[0]["tag"] == "house" and lanes[0]["weight"] == 150.0
    assert lanes[0]["artists"] == ["Bicep", "DJ Seinfeld"]
    assert {lane["tag"] for lane in lanes} == {"house", "electronic", "lo-fi house", "alternative"}


def test_theme_fit():
    theme = th.Theme("House", "", ["house", "deep house"])
    assert th.theme_fit([("house", 100), ("techno", 50)], theme) == 1.0
    assert th.theme_fit([("folk", 100)], theme) == 0.0
    assert th.theme_fit([("folk", 100)], theme, anchor_match=0.5) == 0.5


class FakeLLM:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.prompts = payload, fail, []

    def analyze(self, prompt, system):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("down")
        return LLMResponse(content=json.dumps(self.payload), input_tokens=1, output_tokens=1, model="m")

    def parse_json_response(self, r):
        return json.loads(r.content)


LANES = [{"tag": "house", "weight": 100, "artists": ["Bicep", "DJ Seinfeld"]},
         {"tag": "trip-hop", "weight": 50, "artists": ["Massive Attack"]}]


def test_choose_themes_validates_tags_and_anchors():
    llm = FakeLLM({"themes": [
        {"name": "Late Night House", "description": "d", "tags": ["House", "made-up tag"],
         "anchors": ["bicep", "Nobody"]},
        {"name": "Junk", "tags": ["not a tag"]},
    ]})
    themes, _ = th.choose_themes(llm, LANES, [], None, {"house", "deep house", "trip-hop"},
                                 {"bicep": "Bicep", "dj seinfeld": "DJ Seinfeld"})
    assert themes[0].tags == ["house"] and themes[0].anchors == ["Bicep"] and themes[0].source == "auto"
    assert [t.name for t in themes[1:]] == ["House", "Trip-Hop"]          # fallbacks follow, junk dropped


def test_choose_themes_falls_back_and_skips_recent():
    themes, _ = th.choose_themes(FakeLLM(fail=True), LANES, ["house"], None, {"house", "trip-hop"}, {})
    assert [t.tags for t in themes] == [["trip-hop"]] and themes[0].source == "fallback"


def test_requested_theme_goes_into_the_prompt():
    llm = FakeLLM({"themes": [{"name": "Jazz", "tags": ["jazz"]}]})
    themes, _ = th.choose_themes(llm, LANES, [], "some jazz please", {"jazz", "house"}, {})
    assert "some jazz please" in llm.prompts[0] and themes[0].source == "requested"


def test_theme_roundtrip():
    t = th.Theme("A", "b", ["house"], ["Bicep"], "auto")
    assert th.Theme.from_dict(t.to_dict()) == t and th.Theme.from_dict(None) is None


def test_specificity_and_broad_tags():
    tags = {f"a{i}": [("electronic", 100)] for i in range(40)}
    tags.update({f"b{i}": [("electronic", 100), ("deep house", 90)] for i in range(2)})
    spec = th.tag_specificity(tags)
    assert spec["deep house"] == 1.0 and spec["electronic"] < 0.1
    llm = FakeLLM({"themes": [{"name": "X", "tags": ["electronic", "deep house"]}]})
    themes, _ = th.choose_themes(llm, LANES, [], None, {"electronic", "deep house"}, {}, th.tag_share(tags))
    assert themes[0].tags == ["deep house"]                     # the umbrella tag is dropped
    theme = th.Theme("House", "", ["electronic"])
    assert th.theme_fit([("electronic", 100)], theme, specificity=spec) < 0.1
