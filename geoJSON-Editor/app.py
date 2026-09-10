# Aim: Serve the standalone GeoJSON point editor and its country reference APIs.
# Author: Benjamin Turnbull

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from flask import Response, Flask, abort, render_template


PORT = 9101
ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
HIGH_RES_COUNTRY_DIR = DATA_DIR / "High-Resolution" / "Country"
LOW_RES_COUNTRY_DIR = DATA_DIR / "Low-Resolution" / "Country"
BASEMAP_SOURCE = ROOT_DIR / "data_storage" / "geojson-world-master" / "countries.geojson"
BASEMAP_TOLERANCE_DEGREES = 0.08

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html", port=PORT)


@app.get("/api/world-basemap.geojson")
def world_basemap():
    return Response(get_world_basemap_json(), mimetype="application/geo+json")


@app.get("/api/countries")
def countries():
    return Response(get_country_catalog_json(), mimetype="application/json")


@app.get("/api/countries/<country_id>.geojson")
def country_geojson(country_id):
    country_paths = get_country_paths()
    path = country_paths.get(country_id)
    if not path:
        abort(404)
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return Response(
        json.dumps(normalize_geojson(data), separators=(",", ":")),
        mimetype="application/geo+json",
    )


@lru_cache(maxsize=1)
def get_country_catalog_json():
    return json.dumps(get_country_catalog(), separators=(",", ":"))


@lru_cache(maxsize=1)
def get_country_catalog():
    catalog = []
    for source_key, group, directory, pattern in country_sources():
        for path in sorted(directory.glob(pattern)):
            if should_skip_country_file(path):
                continue
            country_id = f"{source_key}__{path.stem}"
            catalog.append({
                "group": group,
                "id": country_id,
                "name": country_display_name(path.stem),
            })
    return catalog


@lru_cache(maxsize=1)
def get_country_paths():
    paths = {}
    for source_key, group, directory, pattern in country_sources():
        for path in sorted(directory.glob(pattern)):
            if should_skip_country_file(path):
                continue
            paths[f"{source_key}__{path.stem}"] = path
    return paths


def country_sources():
    if HIGH_RES_COUNTRY_DIR.exists():
        return [("high_resolution_country", "High-Resolution Countries", HIGH_RES_COUNTRY_DIR, "*")]
    if LOW_RES_COUNTRY_DIR.exists():
        return [("low_resolution_country", "Low-Resolution Countries", LOW_RES_COUNTRY_DIR, "*")]
    return []


def should_skip_country_file(path):
    return (
        path.name.startswith(".")
        or path.suffix.lower() not in {".geojson", ".json"}
        or path.stem.lower().startswith("capital_city")
    )


def country_display_name(stem):
    return stem.replace("_", " ").replace(" s ", "'s ").title().replace("'S", "'s")


def normalize_geojson(data):
    if data.get("type") == "FeatureCollection":
        return data
    if data.get("type") == "Feature":
        return {"type": "FeatureCollection", "features": [data]}
    if data.get("type") and (data.get("coordinates") or data.get("geometries")):
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {},
                "geometry": data,
            }],
        }
    return {"type": "FeatureCollection", "features": []}


@lru_cache(maxsize=1)
def get_world_basemap_json():
    features = []
    for path, data in basemap_sources():
        for feature in iter_features(data):
            geometry = simplify_geometry(feature.get("geometry"))
            if not geometry:
                continue
            properties = {
                "name": feature.get("properties", {}).get("name") or country_display_name(path.stem)
            }
            features.append({
                "type": "Feature",
                "properties": properties,
                "geometry": geometry,
            })

    return json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }, separators=(",", ":"))


def basemap_sources():
    if BASEMAP_SOURCE.exists():
        with BASEMAP_SOURCE.open(encoding="utf-8") as file:
            yield BASEMAP_SOURCE, json.load(file)
        return

    country_dir = LOW_RES_COUNTRY_DIR if LOW_RES_COUNTRY_DIR.exists() else HIGH_RES_COUNTRY_DIR
    for path in sorted(country_dir.glob("*")):
        if should_skip_country_file(path):
            continue
        with path.open(encoding="utf-8") as file:
            yield path, json.load(file)


def iter_features(data):
    if data.get("type") == "FeatureCollection":
        yield from data.get("features", [])
    elif data.get("type") == "Feature":
        yield data
    elif data.get("type") and (data.get("coordinates") or data.get("geometries")):
        yield {
            "type": "Feature",
            "properties": {},
            "geometry": data,
        }


def simplify_geometry(geometry):
    if not geometry:
        return None

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")

    if geometry_type in {"Point", "MultiPoint"}:
        return geometry
    if geometry_type == "LineString":
        line = simplify_line(coordinates or [], closed=False)
        if has_antimeridian_jump(line):
            return None
        return {"type": geometry_type, "coordinates": line} if len(line) >= 2 else None
    if geometry_type == "MultiLineString":
        lines = [
            line for line in (simplify_line(line or [], closed=False) for line in coordinates or [])
            if len(line) >= 2 and not has_antimeridian_jump(line)
        ]
        return {"type": geometry_type, "coordinates": lines} if lines else None
    if geometry_type == "Polygon":
        polygon = simplify_polygon(coordinates or [])
        return {"type": geometry_type, "coordinates": polygon} if polygon else None
    if geometry_type == "MultiPolygon":
        polygons = [
            polygon for polygon in (simplify_polygon(polygon or []) for polygon in coordinates or [])
            if polygon
        ]
        return {"type": geometry_type, "coordinates": polygons} if polygons else None
    if geometry_type == "GeometryCollection":
        geometries = [
            simplified for simplified in (
                simplify_geometry(child_geometry) for child_geometry in geometry.get("geometries", [])
            )
            if simplified
        ]
        return {"type": geometry_type, "geometries": geometries} if geometries else None
    return None


def simplify_polygon(rings):
    simplified_rings = []
    for ring in rings:
        simplified_ring = simplify_line(ring or [], closed=True)
        if len(simplified_ring) >= 4 and not has_antimeridian_jump(simplified_ring):
            simplified_rings.append(simplified_ring)
    return simplified_rings


def simplify_line(points, closed):
    clean_points = [point for point in points if is_position(point)]
    if len(clean_points) <= 2:
        return clean_points

    was_closed = closed and positions_match(clean_points[0], clean_points[-1])
    working_points = clean_points[:-1] if was_closed else clean_points
    if len(working_points) <= 2:
        simplified = working_points
    elif was_closed:
        simplified = radial_simplify_closed_ring(working_points, BASEMAP_TOLERANCE_DEGREES)
    else:
        simplified = douglas_peucker(working_points, BASEMAP_TOLERANCE_DEGREES)

    if was_closed:
        if not positions_match(simplified[0], simplified[-1]):
            simplified = [*simplified, simplified[0]]
        if len(simplified) < 4:
            return clean_points
    return simplified


def radial_simplify_closed_ring(points, tolerance):
    simplified = [points[0]]
    for point in points[1:]:
        if point_distance(point, simplified[-1]) >= tolerance:
            simplified.append(point)
    if len(simplified) < 3:
        return points
    return simplified


def douglas_peucker(points, tolerance):
    if len(points) <= 2:
        return points

    keep = [False] * len(points)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        max_distance = 0
        max_index = None
        for index in range(start + 1, end):
            distance = perpendicular_distance(points[index], points[start], points[end])
            if distance > max_distance:
                max_distance = distance
                max_index = index
        if max_index is not None and max_distance > tolerance:
            keep[max_index] = True
            stack.append((start, max_index))
            stack.append((max_index, end))

    return [point for point, should_keep in zip(points, keep) if should_keep]


def perpendicular_distance(point, start, end):
    x, y = point[:2]
    x1, y1 = start[:2]
    x2, y2 = end[:2]
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return point_distance(point, start)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / ((dx * dx + dy * dy) ** 0.5)


def point_distance(first, second):
    return ((first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2) ** 0.5


def has_antimeridian_jump(points):
    return any(abs(first[0] - second[0]) > 180 for first, second in zip(points, points[1:]))


def is_position(value):
    return (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], int | float)
        and isinstance(value[1], int | float)
    )


def positions_match(first, second):
    return first[:2] == second[:2]


if __name__ == "__main__":
    app.run(debug=True, port=PORT)
