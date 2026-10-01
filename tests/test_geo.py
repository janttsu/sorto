"""The place of a GPS position comes from a city list shipped with sorto, not from the model."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_engine
from sorto.config import packaged_prompt
from sorto.geo import distance_km, nearest_city, place_of
from sorto.llm import FakeLLMClient
from sorto.media import MediaEvidence, media_note

OSLO = {"GPSLatitude": "59.9139", "GPSLongitude": "10.7522"}


@pytest.mark.parametrize(
    ("lat", "lon", "place"),
    [
        (59.9139, 10.7522, "Oslo, Norway"),
        (60.3913, 5.3221, "Bergen, Norway"),
        (59.94, 10.77, "Oslo, Norway (3 km away)"),
        (59.93, 10.96, "Oslo, Norway (12 km away)"),  # in a suburb: the big city names the area
        (60.9, 10.1, "Lillehammer, Norway (31 km away)"),
        (48.8566, 2.3522, "Paris, France"),
        (35.6762, 139.6503, "Tokyo, Japan (4 km away)"),  # not the ward
        (40.7128, -74.0060, "New York City, United States"),
    ],
)
def test_place_of_a_position(lat: float, lon: float, place: str) -> None:
    assert place_of(lat, lon) == place
    assert place_of(str(lat), str(lon)) == place  # EXIF values arrive as text


def test_place_far_from_everything_and_unusable_positions() -> None:
    assert place_of(0, -30).startswith("far from any city (the nearest is ")
    assert place_of(None, None) == "" and place_of("x", "1") == "" and place_of(95, 0) == ""
    city, country, km = nearest_city(41.9028, 12.4964)
    assert (city, country) == ("Rome", "Italy") and km < 3
    # two capitals a model could mix up when it only sees numbers: they are 2,000 km apart
    assert 1950 < distance_km((59.9139, 10.7522), (41.9028, 12.4964)) < 2050


def test_the_model_is_told_the_place_and_the_user_sees_it(inbox: Path, target: Path, cfg, monkeypatch) -> None:
    exif = {"DateTimeOriginal": "2024:05:01 12:00:00", "Model": "Pixel 8", **OSLO}
    monkeypatch.setattr("sorto.media.read_exif", lambda path, **kw: dict(exif))
    (inbox / "photo-20240501_120000.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 64)
    llm = FakeLLMClient(routes={"photo": "51.11"})
    snap = make_engine(cfg, llm=llm, vision=False).run_until_idle(timeout=30)
    packet = llm.calls[0]
    assert packet.gps_place == "Oslo, Norway"
    assert packet.to_llm_dict()["gps_place"] == "Oslo, Norway"
    assert "GPS 59.9139,10.7522 = Oslo, Norway" in snap.history[0].media_note
    from conftest import packet as plain_packet

    assert "gps_place" not in plain_packet("notes.txt").to_llm_dict()  # nothing is said without a position


def test_media_note_without_a_usable_position() -> None:
    note = media_note(MediaEvidence(kind="image", exif={"GPSLatitude": "north", "GPSLongitude": "east"}))
    assert "GPS north,east" in note and "=" not in note


def test_prompt_makes_gps_place_the_only_source_of_a_place() -> None:
    prompt = packaged_prompt()
    assert '"gps_place" tells you where the photo was taken' in prompt
    assert "Never read a place out of the latitude and longitude yourself" in prompt
    # an example phrase about a named place in the prompt gets repeated by the model as a fact
    assert "points to roughly" not in prompt
    assert "say nothing about what the ID is about" in prompt
