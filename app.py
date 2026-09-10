# Aim: Serve DATE Mapper views, discover geospatial layers, and build map and globe API responses.
# Author: Benjamin Turnbull

import hashlib
import json
import math
import os
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask, render_template, jsonify, abort, request, send_from_directory
import geopandas as gpd
import fiona  # installed via Fiona dependency
from pyproj import Geod
from shapely.geometry import Point, box
from shapely.ops import nearest_points
from shapely.strtree import STRtree

from Utilities.geojson_simplify_folder import simplify_raw_geometry
from Utilities.prioritize_date_country_boundaries import (
    raw_geometry_polygon_parts,
    trim_feature_geometry,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CACHE_DIR = APP_DIR / "cache"
STORAGE_DIR = APP_DIR / "storage"
MARKERS_FILE = STORAGE_DIR / "user_markers.geojson"
GLOBE_DATA_SUBDIR = "Low-Resolution"
MAX_MARKER_ICON_BYTES = 256 * 1024
MAP_DATA_SUBDIR = "High-Resolution"

PORT = 9099

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

USER_MARKERS_LAYER_ID = "user_defined_markers"
LAYER_CATEGORIES = ("Country", "Place", "Infrastructure", "User-Defined")
CATEGORY_DIRECTORY_ALIASES = {
    "underseacables": "Infrastructure",
    "underseainternetcables": "Infrastructure",
    "submarinecables": "Infrastructure",
}
UNDERSEA_CABLE_DIRECTORY_KEYS = frozenset(CATEGORY_DIRECTORY_ALIASES)
UNDERSEA_CABLE_FILE_KEYS = frozenset({
    "cables",
    "underseacables",
    "underseainternetcables",
})
REMOVED_DATA_DIRECTORIES = {"region"}
CAPITAL_CITIES_SUBCATEGORY = "Capital Cities"
DATE_CAPITAL_CITIES_SUBCATEGORY = "DATE Capital Cities"
SHAPEFILE_TEXT_ENCODING_FALLBACKS = ("cp1252", "latin1")
JSON_TEXT_ENCODINGS = ("utf-8-sig", "cp1252", "latin1")
GLOBE_SIMPLIFICATION_TOLERANCE = 0.05
GLOBE_SIMPLIFICATION_ANGLE_TOLERANCE = 6.0
GLOBE_COUNTRIES_CACHE_VERSION = 6
GLOBE_CABLES_CACHE_VERSION = 1
CABLE_TERMINAL_COUNTRY_CACHE_VERSION = 1
CABLE_TERMINAL_MAX_DISTANCE_KM = 50.0
WGS84_GEOD = Geod(ellps="WGS84")

LAYER_CATALOG = [
    {
        "id": USER_MARKERS_LAYER_ID,
        "title": "User Defined Markers",
        "fit_on_load": False,
        "source": {"type": "user_markers"},
    },
    {"id": "gadm41_AUS_1", "title": "Australia Admin Level 1 (States/Territories)", "fit_on_load": False},
    {
        "id": "gadm41_GBR_1",
        "title": "United Kingdom Admin Level 1",
        "fit_on_load": False,
        "source": {"type": "geojson", "file": "gadm41_GBR_1.json"},
    },
    {
        "id": "gadm41_NZL_1",
        "title": "New Zealand Admin Level 1",
        "fit_on_load": False,
        "source": {"type": "geojson", "file": "gadm41_NZL_1.json"},
    },
]

app = Flask(__name__)


# ---------- shared helpers ----------

def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^0-9a-z]+", "_", s)
    return s.strip("_") or "layer"

def _key_for_name(name: str) -> str:
    """Stable key to avoid collisions."""
    crc = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    return f"{_slug(name)}_{crc:08x}"

def _title_from_stem(stem: str) -> str:
    return stem.replace("_", " ").strip() or "GeoJSON Layer"

def _title_case_from_stem(stem: str) -> str:
    return _title_from_stem(stem).title()

def _prettify_label(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", str(value).replace(".", " ")).strip().title()

def _subcategory_label(category: str | None, value: str | None) -> str | None:
    if value is None:
        return None

    normalized = re.sub(r"[^a-z0-9]+", "", str(value).lower())
    if category == "Place" and normalized == "capitalcity":
        return DATE_CAPITAL_CITIES_SUBCATEGORY
    if category == "Place" and normalized in {"capitalcities", "worldcapitalcities"}:
        return CAPITAL_CITIES_SUBCATEGORY
    return _prettify_label(value)

def _default_layer_visibility(metadata: dict) -> bool:
    return (
        metadata.get("category") != "Infrastructure"
        and metadata.get("subcategory") != CAPITAL_CITIES_SUBCATEGORY
    )

def _normalize_category(value: str | None) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    if normalized in CATEGORY_DIRECTORY_ALIASES:
        return CATEGORY_DIRECTORY_ALIASES[normalized]
    for category in LAYER_CATEGORIES:
        if normalized == re.sub(r"[^a-z0-9]+", "", category.lower()):
            return category
    return None

def _infer_category_from_text(value: str) -> str:
    text = str(value or "").lower()
    if "user" in text and "defined" in text:
        return "User-Defined"
    if any(term in text for term in ("undersea cable", "internet cable", "submarine cable")):
        return "Infrastructure"
    if any(term in text for term in ("capital", "city", "place", "marker", "point")):
        return "Place"
    if any(term in text for term in (
        "country",
        "admin level 0",
        "admin level 1",
        "boundary",
        "boundaries",
        "states",
        "world",
    )):
        return "Country"
    return "User-Defined"

def _display_layer_name(
    value: str,
    category: str | None = None,
    subcategory: str | None = None,
    section: str | None = None,
) -> str:
    name = _prettify_label(value)
    prefixes = [
        section,
        subcategory,
        category,
        "Capital City",
        "Cache Geojson",
        "Geojson",
        "Gpkg",
    ]

    for prefix in prefixes:
        clean_prefix = _prettify_label(prefix or "")
        if not clean_prefix:
            continue
        pattern = rf"^{re.escape(clean_prefix)}\s+"
        name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()

    return name or _prettify_label(value) or "Layer"

def _category_path_index(parts: tuple[str, ...]) -> int | None:
    for index, part in enumerate(parts[:-1]):
        if _normalize_category(part):
            return index
    return None

def layer_metadata_from_data_path(path: Path) -> dict:
    rel = path.relative_to(DATA_DIR)
    parts = rel.parts
    category_index = _category_path_index(parts)
    subcategory = None
    section = None

    if category_index is not None:
        category = _normalize_category(parts[category_index])
        category_directory_key = re.sub(r"[^a-z0-9]+", "", parts[category_index].lower())
        if category_directory_key in UNDERSEA_CABLE_DIRECTORY_KEYS:
            subcategory = _prettify_label(parts[category_index])
        elif len(parts) - category_index > 2:
            subcategory = _subcategory_label(category, parts[category_index + 1])
            if len(parts) - category_index > 3:
                section = _prettify_label(parts[category_index + 2])
        elif category == "Infrastructure" and _slug(path.stem).replace("_", "") in UNDERSEA_CABLE_FILE_KEYS:
            subcategory = "Undersea Cables"
    else:
        category = _infer_category_from_text(rel.as_posix())
        if len(parts) > 1:
            subcategory = _prettify_label(parts[0])

    return {
        "category": category,
        "subcategory": subcategory,
        "section": section,
        "name": _display_layer_name(path.stem, category, subcategory, section),
    }

def layer_metadata_from_cache_path(path: Path) -> dict:
    rel = path.relative_to(CACHE_DIR)
    parts = rel.parts
    category_index = _category_path_index(parts)
    category = (
        _normalize_category(parts[category_index])
        if category_index is not None
        else _infer_category_from_text(rel.as_posix())
    )
    subcategory = None
    section = None

    if category_index is not None:
        if len(parts) - category_index > 2:
            subcategory = _subcategory_label(category, parts[category_index + 1])
            if len(parts) - category_index > 3:
                section = _prettify_label(parts[category_index + 2])
    elif category == "Place" and "capital_city" in _slug(path.stem):
        subcategory = CAPITAL_CITIES_SUBCATEGORY
    elif len(parts) > 1:
        subcategory = _prettify_label(parts[0])

    return {
        "category": category,
        "subcategory": subcategory,
        "section": section,
        "name": _display_layer_name(path.stem, category, subcategory, section),
    }

def layer_metadata_from_title(title: str) -> dict:
    category = _infer_category_from_text(title)
    subcategory = None
    if category == "Place" and "capital" in title.lower():
        subcategory = CAPITAL_CITIES_SUBCATEGORY

    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", str(title or "Layer")).strip()
    return {
        "category": category,
        "subcategory": subcategory,
        "section": None,
        "name": _display_layer_name(cleaned, category, subcategory),
    }

def _cache_name(layer_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", layer_id)

def cache_path(layer_id: str) -> Path:
    return CACHE_DIR / f"{_cache_name(layer_id)}.geojson"

def world_dir() -> Path:
    return CACHE_DIR / "World"

def globe_data_dir() -> Path:
    return DATA_DIR / GLOBE_DATA_SUBDIR

def globe_countries_cache_path() -> Path:
    return CACHE_DIR / f"_globe_countries_v{GLOBE_COUNTRIES_CACHE_VERSION}.geojson"

def globe_cables_cache_path() -> Path:
    return CACHE_DIR / f"_globe_cables_v{GLOBE_CABLES_CACHE_VERSION}.geojson"

def map_data_dir() -> Path:
    return DATA_DIR / MAP_DATA_SUBDIR


def globe_country_data_dir() -> Path:
    return map_data_dir() / "Country"

def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_discoverable_data_path(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False

    return all(
        not part.startswith(".") and part.casefold() != "__macosx"
        and part.casefold() not in REMOVED_DATA_DIRECTORIES
        for part in relative_parts
    )


def is_readable_source_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as source:
            source.read(1)
    except OSError:
        return False
    return True


def discover_source_paths(root: Path, suffixes: set[str]) -> list[Path]:
    """Find readable source files without failing on inaccessible folders."""
    if not root.is_dir():
        return []

    normalized_suffixes = {suffix.casefold() for suffix in suffixes}
    candidates = []

    for directory, directory_names, filenames in os.walk(root, onerror=lambda _error: None):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if is_discoverable_data_path(directory_path / name, root)
        )

        for filename in sorted(filenames):
            path = directory_path / filename
            if path.suffix.casefold() not in normalized_suffixes:
                continue
            if not is_discoverable_data_path(path, root):
                continue
            if is_readable_source_file(path):
                candidates.append(path)

    return sorted(candidates)


def read_json_file(path: Path) -> dict:
    raw = path.read_bytes()
    decode_errors = []

    # Reading with Path.read_text() was tried first, but that made decoding depend
    # on one assumed encoding and failed for otherwise valid Windows-authored data.
    # text = path.read_text(encoding="utf-8")
    for encoding in JSON_TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as error:
            decode_errors.append(f"{encoding}: {error}")
            continue

        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{path} decoded as {encoding}, but is not valid JSON: "
                f"{error.msg} at line {error.lineno}, column {error.colno}"
            ) from error

    raise ValueError(
        f"{path} could not be decoded as JSON text ({'; '.join(decode_errors)})"
    )


# ---------- SHP ----------

def shp_path(layer_id: str) -> Path:
    return DATA_DIR / f"{layer_id}.shp"

def is_text_decode_error(error: Exception) -> bool:
    return isinstance(error, UnicodeError) or "codec can't decode" in str(error).lower()

def has_undecoded_text_values(gdf) -> bool:
    """Fiona may return invalid DBF text as bytes instead of raising."""
    for column_name in gdf.columns:
        column = gdf[column_name]
        if getattr(column.dtype, "kind", None) != "O":
            continue
        if any(isinstance(value, (bytes, bytearray)) for value in column):
            return True
    return False

def read_shapefile(path: Path):
    last_decode_error = None
    for encoding in (None, *SHAPEFILE_TEXT_ENCODING_FALLBACKS):
        try:
            if encoding is None:
                gdf = gpd.read_file(path)
            else:
                gdf = gpd.read_file(path, encoding=encoding)
        except Exception as error:
            if not is_text_decode_error(error):
                raise
            last_decode_error = error
            continue

        if has_undecoded_text_values(gdf):
            attempted_encoding = encoding or "the declared/default encoding"
            last_decode_error = ValueError(
                f"{path.name} contains DBF text that could not be decoded with "
                f"{attempted_encoding}"
            )
            continue

        return gdf

    raise last_decode_error

def build_geojson_for_shp(layer_id: str) -> dict:
    path = shp_path(layer_id)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path.name} in {DATA_DIR}")

    gdf = read_shapefile(path)
    if gdf.crs is None:
        raise ValueError(f"{path.name} has no CRS. Ensure the .prj exists and is readable.")

    gdf = gdf.to_crs(epsg=4326)
    return json.loads(gdf.to_json())


def discover_shapefile_paths(root: Path | None = None) -> list[Path]:
    root = root or map_data_dir()
    return discover_source_paths(root, {".shp"})


def is_critical_infrastructure_source(path: Path) -> bool:
    try:
        parts = path.relative_to(map_data_dir()).parts[:-1]
    except ValueError:
        return False

    return any(
        re.sub(r"[^a-z0-9]+", "", part.lower()) == "criticalinfrastructure"
        for part in parts
    )


def shapefile_feature_count(path: Path) -> int | None:
    try:
        with fiona.open(path) as source:
            feature_count = len(source)
            if feature_count <= 0 or not (source.crs or source.crs_wkt):
                return None
            return feature_count
    except (OSError, ValueError, fiona.errors.FionaError):
        return None


def shapefile_representation_key(path: Path) -> str:
    stem = _slug(path.stem)
    for suffix in ("_point", "_polygon"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    if is_critical_infrastructure_source(path):
        scope = path.parent.parent.relative_to(map_data_dir()).as_posix()
        return f"{scope}/{stem}"

    return path.relative_to(map_data_dir()).as_posix()


def selected_shapefile_paths(root: Path | None = None) -> list[tuple[Path, int]]:
    root = root or map_data_dir()
    grouped = {}

    for path in discover_shapefile_paths(root):
        feature_count = shapefile_feature_count(path)
        if feature_count is None:
            continue
        grouped.setdefault(shapefile_representation_key(path), []).append((path, feature_count))

    selected = []
    for candidates in grouped.values():
        selected.append(max(
            candidates,
            key=lambda item: (
                item[1],
                item[0].stem.endswith("_polygon"),
            ),
        ))

    return sorted(selected, key=lambda item: item[0].as_posix())


def shapefile_layer_name(path: Path) -> str:
    stem = re.sub(r"_(point|polygon)$", "", path.stem, flags=re.IGNORECASE)
    return _prettify_label(stem)


def discover_shapefile_files(root: Path | None = None) -> list[dict]:
    root = root or map_data_dir()
    items = []

    for path, feature_count in selected_shapefile_paths(root):
        rel = path.relative_to(DATA_DIR).as_posix()
        metadata = layer_metadata_from_data_path(path)
        metadata["name"] = shapefile_layer_name(path)
        items.append({
            "id": f"shp__{_key_for_name(rel)}",
            "title": f"{rel.replace('/', ' — ')} (Shapefile)",
            "fit_on_load": False,
            "available": True,
            "default_visible": _default_layer_visibility(metadata),
            "feature_count": feature_count,
            **metadata,
        })

    return items


def resolve_shapefile_from_id(layer_id: str, root: Path | None = None) -> Path:
    parts = layer_id.split("__", 1)
    if len(parts) != 2 or parts[0] != "shp":
        raise ValueError("Not an auto-discovered shapefile layer id")

    wanted_key = parts[1]
    for path, _ in selected_shapefile_paths(root):
        rel = path.relative_to(DATA_DIR).as_posix()
        if _key_for_name(rel) == wanted_key:
            return path

    raise ValueError(f"Shapefile not found for id: {layer_id}")


def build_geojson_for_discovered_shp(layer_id: str) -> dict:
    path = resolve_shapefile_from_id(layer_id)
    gdf = read_shapefile(path)
    if gdf.crs is None:
        raise ValueError(f"{path.name} has no CRS. Ensure the .prj exists and is readable.")

    gdf = gdf.to_crs(epsg=4326)
    return json.loads(gdf.to_json())


def shapefile_source_mtime(path: Path) -> float:
    sidecars = path.parent.glob(f"{path.stem}.*")
    return max((candidate.stat().st_mtime for candidate in sidecars), default=path.stat().st_mtime)


# ---------- GPKG (auto-discovered, multi-layer) ----------

def discover_gpkg_layers():
    """
    Returns list of overlay entries for every layer in every *.gpkg in DATA_DIR.
    """
    items = []
    for gpkg in discover_gpkg_paths():
        try:
            layers = fiona.listlayers(gpkg)
        except Exception:
            continue

        rel = gpkg.relative_to(DATA_DIR).as_posix()
        file_key = _key_for_name(rel)
        metadata = layer_metadata_from_data_path(gpkg)
        for layer_name in layers:
            key = _key_for_name(layer_name)
            layer_id = f"gpkg__{file_key}__{key}"
            items.append({
                "id": layer_id,
                "title": f"{rel} — {layer_name} (GPKG)",
                "fit_on_load": False,
                "available": True,
                **metadata,
                "name": _display_layer_name(layer_name, metadata["category"], metadata["subcategory"]),
            })
    return items

def resolve_gpkg_from_id(layer_id: str):
    """
    layer_id: gpkg__<file_stem>__<layer_key>
    -> (gpkg_path, actual_layer_name)
    """
    parts = layer_id.split("__", 2)
    if len(parts) != 3 or parts[0] != "gpkg":
        raise ValueError("Not a GeoPackage layer id")

    file_key = parts[1]
    wanted_key = parts[2]
    candidates = discover_gpkg_paths()

    legacy_gpkg = DATA_DIR / f"{file_key}.gpkg"
    if legacy_gpkg.exists():
        candidates.append(legacy_gpkg)

    for gpkg in candidates:
        rel = gpkg.relative_to(DATA_DIR).as_posix()
        if _key_for_name(rel) != file_key and gpkg.stem != file_key:
            continue

        layers = fiona.listlayers(gpkg)
        for layer_name in layers:
            if _key_for_name(layer_name) == wanted_key:
                return gpkg, layer_name

    raise ValueError("GeoPackage layer not found")


def discover_gpkg_paths() -> list[Path]:
    return discover_source_paths(DATA_DIR, {".gpkg"})

def build_geojson_for_gpkg(layer_id: str) -> dict:
    gpkg_path, layer_name = resolve_gpkg_from_id(layer_id)
    gdf = gpd.read_file(gpkg_path, layer=layer_name)

    if gdf.crs is None:
        raise ValueError(f"{gpkg_path.name}:{layer_name} has no CRS.")

    gdf = gdf.to_crs(epsg=4326)
    return json.loads(gdf.to_json())


# ---------- GeoJSON (auto-discovered, one layer per file) ----------

def discover_geojson_paths(root: Path) -> list[Path]:
    return discover_source_paths(root, {".geojson", ".json"})

def discover_geojson_files(root: Path | None = None):
    """
    Returns list of overlay entries for *.geojson and *.json in a data folder,
    including subfolders. The main map defaults to High-Resolution data only.
    """
    items = []
    root = root or map_data_dir()

    for p in discover_geojson_paths(root):
        rel = p.relative_to(DATA_DIR).as_posix()
        # Use relative-path key so files with same name in subfolders don't collide
        key = _key_for_name(rel)
        title = rel.replace("/", " — ")
        metadata = layer_metadata_from_data_path(p)
        items.append({
            "id": f"geojson__{key}",
            "title": f"{title} (GeoJSON)",
            "fit_on_load": False,
            "available": True,
            "default_visible": _default_layer_visibility(metadata),
            **metadata,
        })
    return items

def resolve_geojson_from_id(layer_id: str, root: Path | None = None) -> Path:
    """
    layer_id: geojson__<relative_file_key>
    -> actual file path (either .geojson or .json)
    """
    parts = layer_id.split("__", 1)
    if len(parts) != 2 or parts[0] != "geojson":
        raise ValueError("Not a GeoJSON layer id")

    wanted_key = parts[1]
    root = root or map_data_dir()
    for p in discover_geojson_paths(root):
        rel = p.relative_to(DATA_DIR).as_posix()
        if _key_for_name(rel) == wanted_key:
            return p

    raise ValueError(f"GeoJSON file not found for id: {layer_id}")

def build_geojson_for_geojson(layer_id: str) -> dict:
    """
    Reads a GeoJSON file as source data.
    GeoJSON files are assumed to be WGS84; close any polygon rings that are
    missing their final point before serving to Leaflet.
    """
    path = resolve_geojson_from_id(layer_id)
    payload = read_json_file(path)
    return close_geojson_polygon_rings(payload)


def close_geojson_polygon_rings(payload: dict) -> dict:
    def close_ring(ring):
        if ring and ring[0] != ring[-1]:
            return ring + [ring[0]]
        return ring

    def close_geometry(geometry):
        if not geometry:
            return geometry

        gtype = geometry.get("type")
        if gtype == "Polygon":
            return {**geometry, "coordinates": [close_ring(ring) for ring in geometry["coordinates"]]}
        if gtype == "MultiPolygon":
            return {
                **geometry,
                "coordinates": [
                    [close_ring(ring) for ring in polygon]
                    for polygon in geometry["coordinates"]
                ],
            }
        if gtype == "GeometryCollection":
            return {
                **geometry,
                "geometries": [close_geometry(item) for item in geometry.get("geometries", [])],
            }
        return geometry

    if payload.get("type") == "FeatureCollection":
        return {
            **payload,
            "features": [
                {**feature, "geometry": close_geometry(feature.get("geometry"))}
                for feature in payload.get("features", [])
            ],
        }

    if payload.get("type") == "Feature":
        return {**payload, "geometry": close_geometry(payload.get("geometry"))}

    return close_geometry(payload)


def simplify_geometry_for_globe(geometry):
    if not geometry:
        return geometry

    return simplify_raw_geometry(
        geometry,
        tolerance=GLOBE_SIMPLIFICATION_TOLERANCE,
        angle_tolerance=GLOBE_SIMPLIFICATION_ANGLE_TOLERANCE,
        method="local",
    )


def discover_capital_city_sources():
    sources = []
    seen = set()

    root = map_data_dir() / "Place"
    for path in discover_geojson_paths(root):
        if not is_discoverable_data_path(path, root):
            continue
        metadata = layer_metadata_from_data_path(path)
        if metadata.get("subcategory") not in {
            CAPITAL_CITIES_SUBCATEGORY,
            DATE_CAPITAL_CITIES_SUBCATEGORY,
        }:
            continue
        rel = path.relative_to(root).as_posix()
        key = _slug(rel)
        if key in seen:
            continue
        sources.append(path)
        seen.add(key)

    return sources


def capital_record_from_feature(path: Path, feature: dict) -> dict | None:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")

    if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
        return None

    properties = feature.get("properties") or {}
    city = str(
        properties.get("name")
        or properties.get("NAME_EN")
        or properties.get("NAME")
        or _title_case_from_stem(path.stem.replace("Capital_City_", ""))
    )
    country = str(properties.get("country") or properties.get("ADM0NAME") or "").strip()
    region = str(properties.get("region") or "").strip()
    dataset = str(properties.get("DATASET") or "").strip().upper()
    if dataset not in {"DATE", "WORLD"}:
        metadata = layer_metadata_from_data_path(path)
        dataset = (
            "DATE"
            if metadata.get("subcategory") == DATE_CAPITAL_CITIES_SUBCATEGORY
            else "WORLD"
        )

    source_key = path.relative_to(DATA_DIR).as_posix()
    feature_key = (
        feature.get("id")
        or properties.get("NE_ID")
        or f"{city}|{country}|{coordinates[0]}|{coordinates[1]}"
    )

    return {
        "id": _key_for_name(f"{source_key}::{feature_key}"),
        "name": city,
        "country": country,
        "region": region,
        "dataset": dataset,
        "longitude": float(coordinates[0]),
        "latitude": float(coordinates[1]),
    }


def build_globe_capitals() -> dict:
    capitals = []

    for path in discover_capital_city_sources():
        try:
            payload = read_json_file(path)
        except (OSError, ValueError):
            continue

        features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]
        for feature in features:
            record = capital_record_from_feature(path, feature)
            if record:
                capitals.append(record)

    capitals.sort(key=lambda item: (
        item["dataset"],
        item["region"],
        item["country"],
        item["name"],
    ))
    return {
        "type": "GlobeCapitalCollection",
        "count": len(capitals),
        "capitals": capitals,
    }


def globe_marker_record_from_feature(feature: dict) -> dict | None:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates")

    if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
        return None

    properties = feature.get("properties") or {}
    marker_id = str(properties.get("marker_id") or _key_for_name(json.dumps(coordinates)))

    return {
        "id": marker_id,
        "name": str(properties.get("name") or "Marker"),
        "description": str(properties.get("description") or ""),
        "longitude": float(coordinates[0]),
        "latitude": float(coordinates[1]),
        "icon_url": properties.get("icon_url") or None,
    }


def build_globe_markers() -> dict:
    collection = read_markers_collection()
    markers = []

    for feature in collection.get("features", []):
        record = globe_marker_record_from_feature(feature)
        if record:
            markers.append(record)

    markers.sort(key=lambda item: item["name"])
    return {
        "type": "GlobeMarkerCollection",
        "count": len(markers),
        "markers": markers,
    }


def flat_map_layer_items(include_cache_geojson: bool = True):
    result = []

    catalog_geojson_files = {
        item["source"]["file"]
        for item in LAYER_CATALOG
        if item.get("source", {}).get("type") == "geojson"
    }

    for item in LAYER_CATALOG:
        layer_id = item["id"]
        src = item.get("source")
        metadata = layer_metadata_from_title(item["title"])

        if not src:
            available = shapefile_feature_count(shp_path(layer_id)) is not None
            origin = "data"
        elif src.get("type") == "geojson":
            path = geojson_file_path(src["file"])
            available = is_readable_source_file(path)
            if available:
                metadata = layer_metadata_from_data_path(path)
            origin = "data"
        elif src.get("type") == "user_markers":
            ensure_markers_store()
            available = True
            origin = "storage"
            metadata = {
                "category": "User-Defined",
                "subcategory": None,
                "section": None,
                "name": "Markers",
            }
        else:
            available = False
            origin = "unknown"

        if not available:
            continue

        result.append({
            "id": layer_id,
            "title": item["title"],
            "fit_on_load": bool(item.get("fit_on_load", False)),
            "available": available,
            "origin": origin,
            **metadata,
        })

    for item in discover_gpkg_layers():
        result.append({**item, "origin": "data"})

    for item in discover_shapefile_files():
        result.append({**item, "origin": "data"})

    for item in discover_geojson_files():
        try:
            p = resolve_geojson_from_id(item["id"])
            rel = p.relative_to(DATA_DIR).as_posix()
            if rel in catalog_geojson_files:
                continue
        except Exception:
            pass
        result.append({**item, "origin": "data"})

    if include_cache_geojson:
        for item in discover_cache_geojson_files():
            result.append({**item, "origin": "cache"})

    return result


def data_path_for_layer_item(item: dict) -> Path | None:
    layer_id = item.get("id")
    if not layer_id:
        return None

    try:
        if layer_id in catalog_map():
            catalog_item = catalog_map()[layer_id]
            source = catalog_item.get("source")
            if source and source.get("type") == "geojson":
                return geojson_file_path(source["file"])
            if source:
                return None
            return shp_path(layer_id)

        if layer_id.startswith("geojson__"):
            return resolve_geojson_from_id(layer_id)

        if layer_id.startswith("shp__"):
            return resolve_shapefile_from_id(layer_id)

        if layer_id.startswith("gpkg__"):
            gpkg_path, _ = resolve_gpkg_from_id(layer_id)
            return gpkg_path
    except Exception:
        return None

    return None


def globe_country_layer_items():
    root = globe_country_data_dir()
    for item in discover_geojson_files(root):
        if not item.get("available"):
            continue

        try:
            path = resolve_geojson_from_id(item["id"], root=root)
        except Exception:
            continue

        if path and path_is_relative_to(path, root):
            yield item, path


def globe_name_from_layer(item: dict) -> str:
    if item.get("name"):
        return str(item["name"])

    title = str(item.get("title") or item.get("id") or "Layer")
    title = re.sub(r"\s*\((Cache\s+)?GeoJSON\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(GPKG\)\s*$", "", title, flags=re.IGNORECASE)
    title = title.split(" — ")[-1]
    title = re.sub(r"\.[^.]+$", "", title)
    return _title_case_from_stem(title)


def is_surface_geometry(geometry: dict | None) -> bool:
    if not geometry:
        return False

    gtype = geometry.get("type")
    if gtype in ("Polygon", "MultiPolygon"):
        return True

    if gtype == "GeometryCollection":
        return any(is_surface_geometry(item) for item in geometry.get("geometries", []))

    return False


def prioritize_globe_date_geometries(countries: list[dict]) -> list[dict]:
    protected_parts = []
    for country in countries:
        if not country.get("is_date_dataset"):
            continue
        for geometry in country.get("geometries", []):
            protected_parts.extend(raw_geometry_polygon_parts(geometry))

    if not protected_parts:
        return countries

    protected_tree = STRtree(protected_parts)
    output = []
    for country in countries:
        if country.get("is_date_dataset"):
            output.append(country)
            continue

        geometries = []
        for geometry in country.get("geometries", []):
            (
                trimmed_geometry,
                _removed_area,
                _removed_residual_parts,
                _removed_residual_area_km2,
            ) = trim_feature_geometry(
                geometry,
                protected_tree,
                protected_parts,
            )
            if trimmed_geometry:
                geometries.append(trimmed_geometry)

        if geometries:
            country["geometries"] = geometries
            output.append(country)

    return output


def build_globe_countries() -> dict:
    countries = []

    for item, path in globe_country_layer_items():
        try:
            payload = close_geojson_polygon_rings(read_json_file(path))
        except Exception:
            continue

        features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]
        geometries = []
        is_date_dataset = False
        for feature in features:
            geometry = feature.get("geometry") if feature.get("type") == "Feature" else feature
            properties = feature.get("properties") or {} if feature.get("type") == "Feature" else {}
            if str(properties.get("DATASET") or "").strip().upper() == "DATE":
                is_date_dataset = True
            if is_surface_geometry(geometry):
                geometries.append(simplify_geometry_for_globe(geometry))

        if geometries:
            name = globe_name_from_layer(item)
            countries.append({
                "id": _key_for_name(item["id"]),
                "name": name,
                "is_date_dataset": is_date_dataset,
                "geometries": geometries,
            })

    countries = prioritize_globe_date_geometries(countries)
    return {
        "type": "GlobeCountryCollection",
        "count": len(countries),
        "countries": countries,
    }


def globe_country_source_signature(source_paths: list[Path]) -> str:
    root = globe_country_data_dir()
    digest = hashlib.sha256()
    for path in sorted(source_paths, key=lambda item: item.name.casefold()):
        stat_result = path.stat()
        relative_path = path.relative_to(root).as_posix()
        digest.update(
            (
                f"{relative_path}\0{stat_result.st_size}\0"
                f"{stat_result.st_mtime_ns}\0{stat_result.st_ctime_ns}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def ensure_globe_countries_cached() -> dict:
    source_paths = discover_geojson_paths(globe_country_data_dir())
    source_signature = globe_country_source_signature(source_paths)
    cached_path = globe_countries_cache_path()

    if cached_path.exists():
        try:
            cached_payload = read_json_file(cached_path)
            if cached_payload.get("source_signature") == source_signature:
                return cached_payload
        except (OSError, ValueError):
            pass

    payload = build_globe_countries()
    payload["source_signature"] = source_signature
    cached_path.write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )
    return payload


def is_undersea_cable_source(path: Path) -> bool:
    try:
        parts = path.relative_to(map_data_dir()).parts[:-1]
    except ValueError:
        return False

    directory_match = any(
        re.sub(r"[^a-z0-9]+", "", part.lower()) in UNDERSEA_CABLE_DIRECTORY_KEYS
        for part in parts
    )
    file_key = _slug(path.stem).replace("_", "")
    infrastructure_parent = any(_normalize_category(part) == "Infrastructure" for part in parts)
    return directory_match or (infrastructure_parent and file_key in UNDERSEA_CABLE_FILE_KEYS)


def discover_undersea_cable_sources() -> list[Path]:
    return [
        path
        for path in discover_geojson_paths(map_data_dir())
        if is_undersea_cable_source(path)
    ]


def is_line_geometry(geometry: dict | None) -> bool:
    if not geometry:
        return False

    geometry_type = geometry.get("type")
    if geometry_type in ("LineString", "MultiLineString"):
        return True

    if geometry_type == "GeometryCollection":
        return any(is_line_geometry(item) for item in geometry.get("geometries", []))

    return False


def cable_terminal_positions(geometry: dict | None) -> list[tuple[float, float]]:
    if not geometry:
        return []

    geometry_type = geometry.get("type")
    coordinate_lines = []
    if geometry_type == "LineString":
        coordinate_lines = [geometry.get("coordinates") or []]
    elif geometry_type == "MultiLineString":
        coordinate_lines = geometry.get("coordinates") or []
    elif geometry_type == "GeometryCollection":
        positions = []
        for item in geometry.get("geometries") or []:
            positions.extend(cable_terminal_positions(item))
        return list(dict.fromkeys(positions))

    positions = []
    for line in coordinate_lines:
        if not isinstance(line, list) or not line:
            continue
        for coordinate in (line[0], line[-1]):
            if (
                isinstance(coordinate, list)
                and len(coordinate) >= 2
                and isinstance(coordinate[0], (int, float))
                and isinstance(coordinate[1], (int, float))
            ):
                positions.append((float(coordinate[0]), float(coordinate[1])))

    return list(dict.fromkeys(positions))


def cable_country_spatial_index():
    countries_payload = ensure_globe_countries_cached()
    layer_id_by_globe_id = {
        _key_for_name(item["id"]): item["id"]
        for item, _path in globe_country_layer_items()
    }
    polygon_parts = []
    polygon_country_ids = []

    for country in countries_payload.get("countries") or []:
        layer_id = layer_id_by_globe_id.get(country.get("id"))
        if not layer_id:
            continue
        for geometry in country.get("geometries") or []:
            for polygon in raw_geometry_polygon_parts(geometry):
                if polygon.is_empty:
                    continue
                polygon_parts.append(polygon)
                polygon_country_ids.append(layer_id)

    if not polygon_parts:
        return None, [], []
    return STRtree(polygon_parts), polygon_parts, polygon_country_ids


def _wrapped_longitude(longitude: float) -> float:
    return ((longitude + 180.0) % 360.0) - 180.0


def terminal_country_ids_for_position(
    position: tuple[float, float],
    country_tree,
    country_polygons: list,
    polygon_country_ids: list[str],
) -> list[str]:
    if country_tree is None:
        return []

    longitude, latitude = position
    points = [Point(longitude + offset, latitude) for offset in (0.0, -360.0, 360.0)]
    covering_country_ids = set()
    for point in points:
        for raw_index in country_tree.query(point):
            index = int(raw_index)
            if country_polygons[index].covers(point):
                covering_country_ids.add(polygon_country_ids[index])

    if covering_country_ids:
        return sorted(covering_country_ids)

    latitude_delta = CABLE_TERMINAL_MAX_DISTANCE_KM / 110.574 + 0.1
    longitude_scale = max(0.05, math.cos(math.radians(latitude)))
    longitude_delta = min(
        10.0,
        CABLE_TERMINAL_MAX_DISTANCE_KM / (111.320 * longitude_scale) + 0.1,
    )
    nearest_country_id = None
    nearest_distance_km = math.inf

    for point in points:
        search_bounds = box(
            point.x - longitude_delta,
            max(-90.0, latitude - latitude_delta),
            point.x + longitude_delta,
            min(90.0, latitude + latitude_delta),
        )
        for raw_index in country_tree.query(search_bounds):
            index = int(raw_index)
            nearest_boundary_point = nearest_points(point, country_polygons[index])[1]
            distance_metres = WGS84_GEOD.inv(
                _wrapped_longitude(point.x),
                point.y,
                _wrapped_longitude(nearest_boundary_point.x),
                nearest_boundary_point.y,
            )[2]
            distance_km = abs(distance_metres) / 1000.0
            if distance_km < nearest_distance_km:
                nearest_distance_km = distance_km
                nearest_country_id = polygon_country_ids[index]

    if nearest_distance_km <= CABLE_TERMINAL_MAX_DISTANCE_KM:
        return [nearest_country_id]
    return []


def add_cable_terminal_country_ids(payload: dict, country_source_signature: str) -> dict:
    country_tree, country_polygons, polygon_country_ids = cable_country_spatial_index()
    features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]

    for feature in features:
        if feature.get("type") != "Feature" or not is_line_geometry(feature.get("geometry")):
            continue
        terminal_country_ids = set()
        for position in cable_terminal_positions(feature.get("geometry")):
            terminal_country_ids.update(terminal_country_ids_for_position(
                position,
                country_tree,
                country_polygons,
                polygon_country_ids,
            ))
        feature["terminal_country_ids"] = sorted(terminal_country_ids)

    payload["date_mapper_terminal_country_version"] = CABLE_TERMINAL_COUNTRY_CACHE_VERSION
    payload["date_mapper_country_source_signature"] = country_source_signature
    return payload


def build_globe_cables() -> dict:
    cables = []
    sources = discover_undersea_cable_sources()

    for path in sources:
        try:
            payload = read_json_file(path)
        except (OSError, ValueError):
            continue

        features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]
        source_key = path.relative_to(DATA_DIR).as_posix()
        for index, feature in enumerate(features):
            geometry = feature.get("geometry") if feature.get("type") == "Feature" else feature
            if not is_line_geometry(geometry):
                continue

            properties = feature.get("properties") or {} if feature.get("type") == "Feature" else {}
            source_id = properties.get("id") or index
            cables.append({
                "id": _key_for_name(f"{source_key}:{source_id}"),
                "name": str(properties.get("name") or f"Cable {index + 1}"),
                "color": str(properties.get("color") or ""),
                "length_km": properties.get("length_km"),
                "rfs_year": properties.get("rfs_year") or "",
                "owners": properties.get("owners") or [],
                "geometry": simplify_geometry_for_globe(geometry),
            })

    return {
        "type": "GlobeCableCollection",
        "count": len(cables),
        "source_count": len(sources),
        "cables": cables,
    }


def ensure_globe_cables_cached() -> dict:
    source_paths = discover_undersea_cable_sources()
    if not source_paths:
        return build_globe_cables()

    latest_source_mtime = max(path.stat().st_mtime for path in source_paths)
    cached_path = globe_cables_cache_path()

    if cached_path.exists() and cached_path.stat().st_mtime >= latest_source_mtime:
        try:
            return read_json_file(cached_path)
        except (OSError, ValueError):
            pass

    payload = build_globe_cables()
    cached_path.write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )
    return payload


# ---------- Cache GeoJSON (auto-discovered, nested cache subfolders) ----------

def discover_cache_geojson_paths():
    """
    Returns nested *.geojson and *.json files in CACHE_DIR.
    Top-level cache files are generated layer caches, so only subfolder files
    are treated as source layers.
    """
    candidates = discover_geojson_paths(CACHE_DIR)
    return [p for p in candidates if p.parent != CACHE_DIR]

def discover_cache_geojson_files():
    """
    Returns list of overlay entries for nested GeoJSON files in CACHE_DIR.
    """
    items = []
    for p in discover_cache_geojson_paths():
        rel = p.relative_to(CACHE_DIR).as_posix()
        folder = _title_from_stem(p.parent.relative_to(CACHE_DIR).as_posix().replace("/", " — "))
        title = f"{folder} — {_title_from_stem(p.stem)} (Cache GeoJSON)"
        layer_id = f"cache_geojson__{_key_for_name(rel)}"
        items.append({
            "id": layer_id,
            "title": title,
            "fit_on_load": False,
            "available": True,
            **layer_metadata_from_cache_path(p),
        })
    return items

def resolve_cache_geojson_from_id(layer_id: str) -> Path:
    """
    layer_id: cache_geojson__<relative_file_key>
    -> actual nested cache GeoJSON path
    """
    parts = layer_id.split("__", 1)
    if len(parts) != 2 or parts[0] != "cache_geojson":
        raise ValueError("Not a cache GeoJSON layer id")

    wanted_key = parts[1]
    for p in discover_cache_geojson_paths():
        rel = p.relative_to(CACHE_DIR).as_posix()
        if _key_for_name(rel) == wanted_key:
            return p

    raise ValueError(f"Cache GeoJSON file not found for id: {layer_id}")

def build_geojson_for_cache_geojson(layer_id: str) -> dict:
    """
    Reads a nested cache GeoJSON file as GeoJSON source data.
    These files are already GeoJSON, so avoid GeoPandas' stricter geometry
    repair path and close any polygon rings that are missing their final point.
    """
    path = resolve_cache_geojson_from_id(layer_id)
    payload = read_json_file(path)
    return close_geojson_polygon_rings(payload)






def catalog_map():
    return {item["id"]: item for item in LAYER_CATALOG}

def geojson_file_path(filename: str) -> Path:
    return DATA_DIR / filename

def build_geojson_for_catalog_geojson(filename: str) -> dict:
    """
    Read a GeoJSON file from DATA_DIR.
    GeoJSON files are assumed to be WGS84; close any polygon rings that are
    missing their final point before serving to Leaflet.
    """
    path = geojson_file_path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path.name} in {DATA_DIR}")

    payload = read_json_file(path)
    return close_geojson_polygon_rings(payload)





def empty_feature_collection() -> dict:
    return {"type": "FeatureCollection", "features": []}


def ensure_markers_store() -> None:
    if MARKERS_FILE.exists():
        return
    MARKERS_FILE.write_text(json.dumps(empty_feature_collection(), indent=2), encoding="utf-8")


def marker_icons_dir() -> Path:
    return STORAGE_DIR / "marker_icons"


def save_marker_icon(file_storage, marker_id: str) -> dict | None:
    if not file_storage or not file_storage.filename:
        return None

    payload = file_storage.read()
    if not payload:
        return None

    if len(payload) > MAX_MARKER_ICON_BYTES:
        raise ValueError("Marker icon PNG must be 256 KB or smaller.")

    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Marker icon must be a PNG file.")

    marker_icons_dir().mkdir(parents=True, exist_ok=True)
    filename = f"{marker_id}.png"
    path = marker_icons_dir() / filename
    path.write_bytes(payload)
    return {
        "icon_filename": filename,
        "icon_url": f"/storage/marker-icons/{filename}",
    }


def read_markers_collection() -> dict:
    ensure_markers_store()
    payload = read_json_file(MARKERS_FILE)

    if payload.get("type") != "FeatureCollection":
        raise ValueError("User marker store must be a GeoJSON FeatureCollection.")

    if not isinstance(payload.get("features"), list):
        raise ValueError("User marker store has an invalid features list.")

    return payload


def write_markers_collection(collection: dict) -> None:
    MARKERS_FILE.write_text(json.dumps(collection, indent=2), encoding="utf-8")

    marker_cache = cache_path(USER_MARKERS_LAYER_ID)
    if marker_cache.exists():
        marker_cache.unlink()


def build_geojson_for_user_markers() -> dict:
    return read_markers_collection()


def _float_field(value, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc


def add_user_marker(payload: dict, icon_file=None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Marker payload must be a JSON object.")

    collection = read_markers_collection()
    lat = _float_field(payload.get("lat"), "Latitude")
    lng = _float_field(payload.get("lng"), "Longitude")

    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not -180 <= lng <= 180:
        raise ValueError("Longitude must be between -180 and 180.")

    name = str(payload.get("name", "")).strip() or f"Marker {len(collection['features']) + 1}"
    description = str(payload.get("description", "")).strip()
    marker_id = uuid4().hex
    icon_properties = save_marker_icon(icon_file, marker_id) or {}

    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {
            "marker_id": marker_id,
            "name": name,
            "description": description,
            "latitude": round(lat, 6),
            "longitude": round(lng, 6),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **icon_properties,
        },
    }

    collection["features"].append(feature)
    write_markers_collection(collection)
    return feature


# ---------- caching ----------


def ensure_cached(layer_id: str) -> dict:
    out = cache_path(layer_id)
    cat = catalog_map().get(layer_id)
    cache_validator = lambda _payload: True

    # Source mtime + builder selection
    if cat and "source" in cat:
        stype = cat["source"].get("type")
        if stype == "geojson":
            src = geojson_file_path(cat["source"]["file"])
            src_mtime = src.stat().st_mtime
            builder = lambda: build_geojson_for_catalog_geojson(cat["source"]["file"])
            if is_undersea_cable_source(src):
                country_signature = globe_country_source_signature(
                    discover_geojson_paths(globe_country_data_dir())
                )
                base_builder = builder
                builder = lambda: add_cable_terminal_country_ids(
                    base_builder(), country_signature
                )
                cache_validator = lambda payload: (
                    payload.get("date_mapper_terminal_country_version")
                    == CABLE_TERMINAL_COUNTRY_CACHE_VERSION
                    and payload.get("date_mapper_country_source_signature")
                    == country_signature
                )
        elif stype == "user_markers":
            ensure_markers_store()
            src = MARKERS_FILE
            src_mtime = src.stat().st_mtime
            builder = build_geojson_for_user_markers
        else:
            raise ValueError(f"Unsupported catalog source type: {stype}")

    elif layer_id.startswith("gpkg__"):
        gpkg_path, _ = resolve_gpkg_from_id(layer_id)
        src_mtime = gpkg_path.stat().st_mtime
        builder = lambda: build_geojson_for_gpkg(layer_id)

    elif layer_id.startswith("shp__"):
        shapefile_path = resolve_shapefile_from_id(layer_id)
        src_mtime = shapefile_source_mtime(shapefile_path)
        builder = lambda: build_geojson_for_discovered_shp(layer_id)

    elif layer_id.startswith("geojson__"):
        source_path = resolve_geojson_from_id(layer_id)
        src_mtime = source_path.stat().st_mtime
        builder = lambda: build_geojson_for_geojson(layer_id)
        if is_undersea_cable_source(source_path):
            country_signature = globe_country_source_signature(
                discover_geojson_paths(globe_country_data_dir())
            )
            base_builder = builder
            builder = lambda: add_cable_terminal_country_ids(
                base_builder(), country_signature
            )
            cache_validator = lambda payload: (
                payload.get("date_mapper_terminal_country_version")
                == CABLE_TERMINAL_COUNTRY_CACHE_VERSION
                and payload.get("date_mapper_country_source_signature")
                == country_signature
            )

    elif layer_id.startswith("cache_geojson__"):
        src_mtime = resolve_cache_geojson_from_id(layer_id).stat().st_mtime
        builder = lambda: build_geojson_for_cache_geojson(layer_id)

    else:
        # default = curated shapefile by basename
        src_mtime = shp_path(layer_id).stat().st_mtime
        builder = lambda: build_geojson_for_shp(layer_id)

    # Cache hit?
    if out.exists() and out.stat().st_mtime >= src_mtime:
        try:
            cached_payload = json.loads(out.read_text(encoding="utf-8"))
            if cache_validator(cached_payload):
                return cached_payload
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Older Windows-created caches may not be UTF-8. Rebuild them from
            # the source instead of bypassing the source encoding fallbacks.
            pass

    geojson = builder()
    out.write_text(json.dumps(geojson), encoding="utf-8")
    return geojson




# ---------- routes ----------

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/globe")
def globe():
    return render_template("globe.html")

@app.get("/settings")
def settings():
    return render_template("settings.html")

@app.get("/api/layers")
def api_layers():
    return jsonify(flat_map_layer_items())


@app.get("/api/globe/countries")
def api_globe_countries():
    return jsonify(ensure_globe_countries_cached())


@app.get("/api/globe/capitals")
def api_globe_capitals():
    return jsonify(build_globe_capitals())


@app.get("/api/globe/cables")
def api_globe_cables():
    return jsonify(ensure_globe_cables_cached())


@app.get("/api/globe/markers")
def api_globe_markers():
    return jsonify(build_globe_markers())



@app.get("/api/layer/<layer_id>")
def api_layer(layer_id: str):
    is_in_catalog = layer_id in catalog_map()
    is_gpkg = layer_id.startswith("gpkg__")
    is_shapefile_auto = layer_id.startswith("shp__")
    is_geojson_auto = layer_id.startswith("geojson__")
    is_cache_geojson_auto = layer_id.startswith("cache_geojson__")

    if not any((is_in_catalog, is_gpkg, is_shapefile_auto, is_geojson_auto, is_cache_geojson_auto)):
        abort(404, f"Unknown layer '{layer_id}'")

    try:
        return jsonify(ensure_cached(layer_id))
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/markers")
def api_markers():
    try:
        return jsonify(read_markers_collection())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/markers")
def api_markers_create():
    try:
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            payload = request.form.to_dict()
            icon_file = request.files.get("icon")
        else:
            payload = request.get_json(silent=True) or {}
            icon_file = None

        feature = add_user_marker(payload, icon_file=icon_file)
        return jsonify(feature), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/storage/marker-icons/<path:filename>")
def marker_icon(filename: str):
    if "/" in filename or "\\" in filename or not filename.endswith(".png"):
        abort(404)
    return send_from_directory(marker_icons_dir(), filename, mimetype="image/png")



if __name__ == "__main__":
    app.run(debug=True, port=PORT)
