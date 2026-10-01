#!/usr/bin/env python3
"""Build src/sorto/data/cities.tsv.gz from the GeoNames dumps.

    curl -O https://download.geonames.org/export/dump/cities15000.zip && unzip cities15000.zip
    curl -O https://download.geonames.org/export/dump/countryInfo.txt
    python scripts/build_cities.py cities15000.txt countryInfo.txt

One line per city of at least 15,000 people (districts of cities left out): name, country,
latitude, longitude, population.
The data is from GeoNames (https://www.geonames.org/), licensed CC BY 4.0.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "sorto" / "data" / "cities.tsv.gz"


def main(cities: str, countries: str) -> None:
    names: dict[str, str] = {}
    for line in Path(countries).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            names[cols[0]] = cols[4]
    rows = []
    for line in Path(cities).read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if cols[7] == "PPLX":
            continue  # a district of a city ("Paris 04", a ward of Tokyo): the city itself says more
        name, lat, lon, country = cols[1], float(cols[4]), float(cols[5]), cols[8]
        rows.append((name, names.get(country, country), f"{lat:.3f}", f"{lon:.3f}", cols[14] or "0"))
    rows.sort()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-identical between builds of the same dump
    with gzip.GzipFile(OUT, "wb", compresslevel=9, mtime=0) as f:
        f.write("".join("\t".join(r) + "\n" for r in rows).encode("utf-8"))
    print(f"{len(rows)} cities -> {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
