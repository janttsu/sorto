"""Where a GPS position is, without asking anyone: the nearest city from a list shipped with sorto.

A language model handed bare coordinates guesses, and guesses towards
whatever place is on its mind: with an ID for a trip to one city in the
outline, it calls a position 2,000 km away "the area" of that city. So
sorto works the place out itself and gives the model the answer.

The list is every city of at least 15,000 people from GeoNames
(https://www.geonames.org/, CC BY 4.0), built by ``scripts/build_cities.py``.
Nothing is looked up online.
"""

from __future__ import annotations

import gzip
import importlib.resources
import math
from functools import lru_cache

FAR_KM = 300.0  # further than this from every city: open sea, desert, or a broken position
METRO_DEG = 0.18  # about 20 km: within this, the biggest city names the place, not the nearest suburb


@lru_cache(maxsize=1)
def _cities() -> tuple[tuple[str, ...], tuple[str, ...], tuple[float, ...], tuple[float, ...], tuple[int, ...]]:
    try:
        raw = importlib.resources.files("sorto").joinpath("data/cities.tsv.gz").read_bytes()
    except OSError:
        return (), (), (), (), ()
    names, countries, lats, lons, people = [], [], [], [], []
    for line in gzip.decompress(raw).decode("utf-8").splitlines():
        name, country, lat, lon, population = line.split("\t")
        names.append(name)
        countries.append(country)
        lats.append(float(lat))
        lons.append(float(lon))
        people.append(int(population))
    return tuple(names), tuple(countries), tuple(lats), tuple(lons), tuple(people)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


@lru_cache(maxsize=4096)
def nearest_city(lat: float, lon: float) -> tuple[str, str, float] | None:
    """(city, country, km) of the city the position is at, or None without a list.

    That is the biggest city within about 20 km ("Tokyo", not the ward the
    photo was taken in), and otherwise the closest one.
    """
    names, countries, lats, lons, people = _cities()
    if not names or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    # Flat-map distance is enough to pick the candidate; the real distance is computed once.
    shrink = math.cos(math.radians(lat))
    best, best_d, near = 0, float("inf"), []
    for i in range(len(names)):
        dlat = lats[i] - lat
        dlon = abs(lons[i] - lon)
        if dlon > 180.0:
            dlon = 360.0 - dlon
        d = dlat * dlat + (dlon * shrink) ** 2
        if d < best_d:
            best, best_d = i, d
        if d < METRO_DEG * METRO_DEG:
            near.append(i)
    if near:
        best = max(near, key=lambda i: people[i])
    return names[best], countries[best], distance_km((lat, lon), (lats[best], lons[best]))


def place_of(lat: float | str | None, lon: float | str | None) -> str:
    """One phrase for the model and the user: "Oslo, Norway (3 km away)". "" if unknown."""
    try:
        found = nearest_city(round(float(lat), 3), round(float(lon), 3))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if found is None:
        return ""
    city, country, km = found
    if km > FAR_KM:
        return f"far from any city (the nearest is {city}, {country}, {km:.0f} km away)"
    return f"{city}, {country}" if km < 1.5 else f"{city}, {country} ({km:.0f} km away)"
