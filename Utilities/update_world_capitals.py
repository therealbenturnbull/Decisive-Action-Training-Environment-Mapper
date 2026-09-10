#!/usr/bin/env python3
# Aim: Rebuild the bundled world-capital layer from Natural Earth populated places.
# Author: Benjamin Turnbull

"""Build the bundled world-capital layer from Natural Earth populated places."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.request
from pathlib import Path

from shapely.geometry import Point
from shapely.strtree import STRtree


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Utilities.prioritize_date_country_boundaries import (
    dataset_for_payload,
    discover_country_files,
    raw_geometry_polygon_parts,
    read_geojson,
)


NATURAL_EARTH_VERSION = "5.1.2"
SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    f"v{NATURAL_EARTH_VERSION}/geojson/ne_10m_populated_places.geojson"
)
SOURCE_SHA256 = "9b8e3de09048ef00dfc70357dbb9fa324493f214b5e0ae4daf1aa79a8d10116b"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "High-Resolution"
    / "Place"
    / "Capital Cities"
    / "World_Capital_Cities.geojson"
)
DEFAULT_DATE_COUNTRY_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "High-Resolution"
    / "Country"
)


def first_text(properties: dict, *keys: str) -> str:
    for key in keys:
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def is_admin_zero_capital(properties: dict) -> bool:
    value = properties.get("ADM0CAP")
    return value is True or str(value).strip() == "1"


def normalized_capital_feature(feature: dict) -> dict | None:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")

    if not is_admin_zero_capital(properties):
        return None
    if geometry.get("type") != "Point" or not isinstance(coordinates, list):
        return None
    if len(coordinates) < 2 or not all(isinstance(value, (int, float)) for value in coordinates[:2]):
        return None

    longitude = float(coordinates[0])
    latitude = float(coordinates[1])
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None

    name = first_text(properties, "NAME_EN", "NAMEASCII", "NAME")
    country = first_text(properties, "ADM0NAME")
    natural_earth_id = properties.get("NE_ID")
    if not name or not country or natural_earth_id is None:
        return None

    population = properties.get("POP_MAX")
    if not isinstance(population, (int, float)) or population < 0:
        population = None

    capital_type = first_text(properties, "CAPIN", "FEATURECLA")
    normalized_properties = {
        "name": name,
        "country": country,
        "sovereign": first_text(properties, "SOV0NAME"),
        "code": first_text(properties, "ADM0_A3"),
        "iso_a2": first_text(properties, "ISO_A2"),
        "population": population,
        "wikidata_id": first_text(properties, "WIKIDATAID"),
        "DATASET": "WORLD",
        "source": f"Natural Earth {NATURAL_EARTH_VERSION}",
    }
    if capital_type and capital_type.casefold() != "admin-0 capital":
        normalized_properties["capital_type"] = capital_type

    return {
        "type": "Feature",
        "id": f"natural-earth-{natural_earth_id}",
        "properties": normalized_properties,
        "geometry": {
            "type": "Point",
            "coordinates": [longitude, latitude],
        },
    }


def load_date_country_polygons(country_dir: Path) -> list:
    polygons = []
    for path in discover_country_files(country_dir):
        payload = read_geojson(path)
        if dataset_for_payload(payload, path) != "DATE":
            continue
        for feature in payload.get("features", []):
            polygons.extend(raw_geometry_polygon_parts(feature.get("geometry")))

    if not polygons:
        raise ValueError(f"No DATASET=DATE country polygons found in {country_dir}")
    return polygons


def capital_is_covered_by_date_country(
    capital: dict,
    date_country_polygons: list,
    spatial_index: STRtree,
) -> bool:
    coordinates = capital["geometry"]["coordinates"]
    point = Point(float(coordinates[0]), float(coordinates[1]))
    return any(
        date_country_polygons[int(index)].covers(point)
        for index in spatial_index.query(point)
    )


def build_world_capitals(
    source_payload: dict,
    date_country_polygons: list | None = None,
) -> dict:
    if source_payload.get("type") != "FeatureCollection":
        raise ValueError("Natural Earth source must be a GeoJSON FeatureCollection")

    date_country_polygons = list(date_country_polygons or [])
    spatial_index = STRtree(date_country_polygons) if date_country_polygons else None
    features = []
    excluded_capitals = []
    for source_feature in source_payload.get("features", []):
        capital = normalized_capital_feature(source_feature)
        if not capital:
            continue
        if spatial_index and capital_is_covered_by_date_country(
            capital,
            date_country_polygons,
            spatial_index,
        ):
            excluded_capitals.append({
                "id": capital["id"],
                "name": capital["properties"]["name"],
                "country": capital["properties"]["country"],
            })
            continue
        features.append(capital)

    features.sort(key=lambda feature: (
        feature["properties"]["country"].casefold(),
        feature["properties"]["name"].casefold(),
    ))

    ids = [feature["id"] for feature in features]
    if not features:
        raise ValueError("Natural Earth source contains no admin-0 capitals")
    if len(ids) != len(set(ids)):
        raise ValueError("Natural Earth source contains duplicate capital identifiers")

    return {
        "type": "FeatureCollection",
        "name": "World Capital Cities",
        "source": {
            "name": "Natural Earth",
            "dataset": "1:10m Populated Places",
            "version": NATURAL_EARTH_VERSION,
            "url": SOURCE_URL,
            "license": "Public domain",
            "filter": "ADM0CAP = 1",
            "exclusion": (
                "Remove WORLD capital points covered by any DATASET=DATE "
                "country polygon"
            ),
            "source_sha256": SOURCE_SHA256,
        },
        "date_country_exclusion": {
            "excluded_count": len(excluded_capitals),
            "excluded_capitals": excluded_capitals,
        },
        "features": features,
    }


def read_source(source_file: Path | None) -> dict:
    if source_file:
        source_bytes = source_file.read_bytes()
    else:
        request = urllib.request.Request(
            SOURCE_URL,
            headers={"User-Agent": "DATE-Mapper-capital-importer/1.0"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            source_bytes = response.read()

    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != SOURCE_SHA256:
        raise ValueError(
            "Natural Earth source checksum did not match the pinned 5.1.2 dataset: "
            f"expected {SOURCE_SHA256}, received {digest}"
        )

    return json.loads(source_bytes.decode("utf-8"))


def write_geojson(payload: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-file",
        type=Path,
        help="Use an already-downloaded Natural Earth GeoJSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for the filtered world-capital GeoJSON.",
    )
    parser.add_argument(
        "--date-country-dir",
        type=Path,
        default=DEFAULT_DATE_COUNTRY_DIR,
        help="Country directory containing the DATASET=DATE polygons to exclude.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    date_country_polygons = load_date_country_polygons(args.date_country_dir)
    result = build_world_capitals(
        read_source(args.source_file),
        date_country_polygons,
    )
    write_geojson(result, args.output)
    excluded_count = result["date_country_exclusion"]["excluded_count"]
    print(
        f"Wrote {len(result['features'])} world capitals to {args.output}; "
        f"excluded {excluded_count} inside DATE countries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
