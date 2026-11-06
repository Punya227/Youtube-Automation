import json
from types import SimpleNamespace

import pytest

from metadata_generator import (MetadataGenerator, SidecarError, VideoMetadata, compose_description,
                                humanize_filename, load_sidecar, sanitize)


def test_humanize_filename_strips_noise():
    assert humanize_filename("IMG_20240101_hidden-places_in_kashmir_final.mp4") == "Hidden Places In Kashmir"
    assert humanize_filename("myTripToGoa.mp4") == "My Trip To Goa"


def test_template_fallback_without_api_key():
    meta = MetadataGenerator().generate("hidden_places_in_kashmir.mp4")
    assert meta.source == "template"
    assert meta.title == "Hidden Places In Kashmir"
    assert "#Shorts" in meta.hashtags
    assert meta.category_id == "19"  # travel keyword
    # Template must not claim to have seen the video content
    for phrase in ("in this video you", "watch as", "we see", "shows"):
        assert phrase not in meta.description.lower()


def test_topic_takes_precedence_over_filename():
    meta = MetadataGenerator().generate("clip001.mp4", topic="Cricket batting tips")
    assert meta.title == "Cricket batting tips"
    assert meta.category_id == "17"


def test_devanagari_detected_as_hindi():
    meta = MetadataGenerator().generate("कश्मीर_की_सुंदरता.mp4")
    assert meta.language == "hi"


def test_full_sidecar_override_is_used_verbatim():
    sidecar = {"title": "Amazing Hidden Places in Kashmir", "description": "Explore the breathtaking beauty of Kashmir.",
               "tags": ["Kashmir", "Travel", "Nature"], "hashtags": ["#Kashmir", "#Travel", "#Shorts"], "language": "en"}
    meta = MetadataGenerator().generate("whatever.mp4", sidecar=sidecar)
    assert meta.source == "override"
    assert meta.title == sidecar["title"]
    assert meta.tags == sidecar["tags"]
    assert meta.hashtags == sidecar["hashtags"]


def test_partial_sidecar_merges_with_generated():
    meta = MetadataGenerator().generate("goa_beach.mp4", sidecar={"title": "Sunset at Goa"})
    assert meta.title == "Sunset at Goa"
    assert meta.source == "mixed"
    assert meta.description  # generated part still present


def test_load_sidecar_validation(tmp_path):
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"title": "T", "tags": ["a"], "category_id": 19}), encoding="utf-8")
    assert load_sidecar(good)["category_id"] == "19"
    bad = tmp_path / "b.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SidecarError):
        load_sidecar(bad)
    wrong = tmp_path / "c.json"
    wrong.write_text(json.dumps({"tags": "not-a-list"}), encoding="utf-8")
    with pytest.raises(SidecarError):
        load_sidecar(wrong)


def test_sanitize_enforces_youtube_limits():
    meta = sanitize(VideoMetadata(
        title="<b>" + "x" * 150 + "</b>",
        description="Hello <world>" + "y" * 6000,
        tags=["tag number %d with words" % i for i in range(60)],
        hashtags=["one", "#two", "three four", "#five", "#six", "#seven"],
    ))
    assert len(meta.title) <= 100 and "<" not in meta.title
    assert "<" not in meta.description and len(meta.description.encode()) <= 5000
    assert sum(len(t) + (2 if " " in t else 0) for t in meta.tags) + len(meta.tags) - 1 <= 500
    assert len(meta.hashtags) <= 5 and "#Shorts" in meta.hashtags
    assert all(" " not in h and h.startswith("#") for h in meta.hashtags)


def test_compose_description_appends_missing_hashtags():
    text = compose_description("Body text #Travel", ["#Travel", "#Shorts"])
    assert text.endswith("#Shorts") and text.count("#Travel") == 1


class FakeClient:
    def __init__(self, payload=None, error=None, stop_reason="end_turn"):
        self.payload, self.error, self.stop_reason = payload, error, stop_reason
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        block = SimpleNamespace(type="text", text=json.dumps(self.payload))
        return SimpleNamespace(content=[block], stop_reason=self.stop_reason)


def test_anthropic_generation_uses_structured_output():
    client = FakeClient({"title": "Kashmir's Hidden Valleys", "description": "A short on Kashmir travel.",
                         "tags": ["Kashmir", "travel"], "hashtags": ["#Kashmir", "#Shorts"],
                         "category_id": "19", "language": "en"})
    meta = MetadataGenerator(client=client).generate("kashmir.mp4", topic="Kashmir travel")
    assert meta.source == "anthropic" and meta.title == "Kashmir's Hidden Valleys"
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "have NOT seen" in call["system"]


def test_anthropic_failure_falls_back_to_template():
    meta = MetadataGenerator(client=FakeClient(error=ConnectionError("offline"))).generate("goa_trip.mp4")
    assert meta.source == "template" and meta.title == "Goa Trip"
    assert any("AI generation unavailable" in w for w in meta.warnings)


def test_anthropic_refusal_falls_back_to_template():
    client = FakeClient({"title": "x"}, stop_reason="refusal")
    assert MetadataGenerator(client=client).generate("goa_trip.mp4").source == "template"
