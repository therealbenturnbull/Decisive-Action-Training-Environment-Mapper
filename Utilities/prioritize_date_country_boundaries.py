#!/usr/bin/env python3
# Aim: Give DATE countries geometric priority over overlapping WORLD country boundaries.
# Author: Benjamin Turnbull

"""Give DATE countries precedence over overlapping WORLD country geometry.

The utility keeps every DATE source file untouched and subtracts the combined
DATE footprint from every other country file. Multiple country directories can
be processed in one validated run, which keeps the map and globe datasets in
sync even when only the reference directory contains DATASET metadata.

The default mode is a dry run. Pass --apply to replace changed WORLD files.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from math import pi
from pathlib import Path

from pyproj import Geod
from shapely.affinity import translate
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.strtree import STRtree
from shapely.validation import make_valid


GEOJSON_SUFFIXES = {".geojson", ".json"}
CENTRAL_WORLD = box(-180, -90, 180, 90)
EAST_WORLD = box(180, -90, 540, 90)
WEST_WORLD = box(-540, -90, -180, 90)
# GEOS differences can leave sub-centimetre numerical remnants at shared edges.
AREA_EPSILON = 1e-14
WGS84_GEOD = Geod(ellps="WGS84")
# DATE cuts can leave tiny islands or long, narrow strips whose fill is invisible
# but whose country stroke remains prominent. Only newly cut pieces are filtered.
MIN_RESIDUAL_AREA_KM2 = 250.0
MAX_SLIVER_AREA_KM2 = 2500.0
MAX_SLIVER_COMPACTNESS = 0.03


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


def discover_country_files(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in Path(directory).iterdir()
            if path.is_file() and path.suffix.casefold() in GEOJSON_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )


def read_geojson(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    if payload.get("type") != "FeatureCollection":
        raise ValueError(f"Expected a FeatureCollection: {path}")
    if not isinstance(payload.get("features"), list):
        raise ValueError(f"Expected a features list: {path}")
    return payload


def dataset_for_payload(payload: dict, path: Path | None = None) -> str | None:
    features = payload.get("features", [])
    values = [
        str((feature.get("properties") or {}).get("DATASET") or "").strip().upper()
        for feature in features
    ]
    populated = {value for value in values if value}

    if "DATE" in populated and any(value != "DATE" for value in values):
        location = f" in {path}" if path else ""
        raise ValueError(f"Mixed DATE and non-DATE features{location}")
    if values and all(value == "DATE" for value in values):
        return "DATE"
    if populated == {"WORLD"} and all(value == "WORLD" for value in values):
        return "WORLD"
    return None


def reference_date_filenames(reference_dir: Path) -> set[str]:
    date_filenames = set()
    files = discover_country_files(reference_dir)
    if not files:
        raise ValueError(f"No GeoJSON country files found in {reference_dir}")

    for path in files:
        if dataset_for_payload(read_geojson(path), path) == "DATE":
            date_filenames.add(path.name)

    if not date_filenames:
        raise ValueError(f"No DATASET=DATE country files found in {reference_dir}")
    return date_filenames


def polygon_parts(geometry):
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def valid_polygon_parts(geometry):
    if geometry.is_empty:
        return

    try:
        cleaned = geometry if geometry.is_valid else make_valid(geometry)
    except GEOSException:
        cleaned = geometry.buffer(0)

    if not cleaned.is_valid:
        cleaned = cleaned.buffer(0)
    yield from polygon_parts(cleaned)


def unwrap_ring(ring, reference_longitude: float | None = None):
    if not ring:
        return []

    output = []
    previous = float(ring[0][0])
    for coordinate in ring:
        longitude = float(coordinate[0])
        while longitude - previous > 180:
            longitude -= 360
        while longitude - previous < -180:
            longitude += 360
        output.append((longitude, *coordinate[1:]))
        previous = longitude

    if reference_longitude is not None:
        centre = sum(coordinate[0] for coordinate in output) / len(output)
        offset = round((reference_longitude - centre) / 360) * 360
        if offset:
            output = [
                (coordinate[0] + offset, *coordinate[1:])
                for coordinate in output
            ]
    return output


def canonical_polygon_parts(coordinates):
    if not coordinates or not coordinates[0]:
        return

    shell = unwrap_ring(coordinates[0])
    shell_centre = sum(coordinate[0] for coordinate in shell) / len(shell)
    holes = [unwrap_ring(ring, shell_centre) for ring in coordinates[1:] if ring]

    for polygon in valid_polygon_parts(Polygon(shell, holes)):
        min_x, _min_y, max_x, _max_y = polygon.bounds
        if min_x >= -180 and max_x <= 180:
            yield polygon
            continue

        for clip_box, x_offset in (
            (CENTRAL_WORLD, 0),
            (EAST_WORLD, -360),
            (WEST_WORLD, 360),
        ):
            try:
                clipped = polygon.intersection(clip_box)
            except GEOSException:
                clipped = make_valid(polygon).intersection(clip_box)
            if x_offset and not clipped.is_empty:
                clipped = translate(clipped, xoff=x_offset)
            yield from valid_polygon_parts(clipped)


def raw_geometry_polygon_parts(raw_geometry: dict | None):
    if not raw_geometry:
        return

    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        yield from canonical_polygon_parts(coordinates)
        return
    if geometry_type == "MultiPolygon":
        for polygon_coordinates in coordinates:
            yield from canonical_polygon_parts(polygon_coordinates)
        return
    raise ValueError(f"Expected Polygon or MultiPolygon, got {geometry_type!r}")


def polygon_coordinates(polygon: Polygon) -> list:
    return [
        [list(coordinate) for coordinate in polygon.exterior.coords],
        *(
            [list(coordinate) for coordinate in interior.coords]
            for interior in polygon.interiors
        ),
    ]


def raw_geometry_from_parts(parts: list[Polygon], original_type: str) -> dict | None:
    if not parts:
        return None

    coordinates = [polygon_coordinates(part) for part in parts]
    if len(coordinates) == 1 and original_type == "Polygon":
        return {"type": "Polygon", "coordinates": coordinates[0]}
    return {"type": "MultiPolygon", "coordinates": coordinates}


def protected_parts_from_files(paths: list[Path]) -> list[Polygon]:
    parts = []
    for path in paths:
        payload = read_geojson(path)
        for feature in payload.get("features", []):
            parts.extend(raw_geometry_polygon_parts(feature.get("geometry")))

    if not parts:
        raise ValueError("DATE country files contain no polygon geometry")
    return parts


def positive_intersection_area(first, second) -> float:
    try:
        return first.intersection(second).area
    except GEOSException:
        return make_valid(first).intersection(make_valid(second)).area


def polygon_geodesic_metrics(polygon: Polygon) -> tuple[float, float]:
    oriented = orient(polygon, sign=1.0)
    signed_area_m2, _exterior_perimeter_m = WGS84_GEOD.geometry_area_perimeter(
        oriented
    )
    perimeter_m = WGS84_GEOD.geometry_length(oriented.exterior) + sum(
        WGS84_GEOD.geometry_length(interior)
        for interior in oriented.interiors
    )
    area_km2 = abs(signed_area_m2) / 1_000_000
    compactness = (
        4 * pi * area_km2 / ((perimeter_m / 1000) ** 2)
        if area_km2 and perimeter_m
        else 0.0
    )
    return area_km2, compactness


def is_residual_sliver(polygon: Polygon) -> tuple[bool, float]:
    area_km2, compactness = polygon_geodesic_metrics(polygon)
    is_tiny = area_km2 < MIN_RESIDUAL_AREA_KM2
    is_narrow = (
        area_km2 < MAX_SLIVER_AREA_KM2
        and compactness < MAX_SLIVER_COMPACTNESS
    )
    return is_tiny or is_narrow, area_km2


def subtract_protected_parts(
    source_part: Polygon,
    tree: STRtree,
    protected_parts: list[Polygon],
) -> tuple[list[Polygon], float, int, float]:
    candidates = sorted({int(index) for index in tree.query(source_part)})
    output_parts = [source_part]
    removed_area = 0.0

    for index in candidates:
        protected_part = protected_parts[index]
        next_parts = []
        for current_part in output_parts:
            overlap_area = positive_intersection_area(current_part, protected_part)
            if overlap_area <= AREA_EPSILON:
                next_parts.append(current_part)
                continue

            try:
                difference = current_part.difference(protected_part)
            except GEOSException:
                difference = make_valid(current_part).difference(
                    make_valid(protected_part)
                )
            next_parts.extend(valid_polygon_parts(difference))
            removed_area += overlap_area
        output_parts = next_parts
        if not output_parts:
            break

    if removed_area <= AREA_EPSILON:
        return output_parts, 0.0, 0, 0.0

    retained_parts = []
    removed_residual_parts = 0
    removed_residual_area_km2 = 0.0
    for output_part in output_parts:
        discard, area_km2 = is_residual_sliver(output_part)
        if discard:
            removed_residual_parts += 1
            removed_residual_area_km2 += area_km2
        else:
            retained_parts.append(output_part)

    return (
        retained_parts,
        removed_area,
        removed_residual_parts,
        removed_residual_area_km2,
    )


def trim_feature_geometry(
    raw_geometry: dict,
    tree: STRtree,
    protected_parts: list[Polygon],
) -> tuple[dict | None, float, int, float]:
    output_parts = []
    removed_area = 0.0
    removed_residual_parts = 0
    removed_residual_area_km2 = 0.0
    for source_part in raw_geometry_polygon_parts(raw_geometry):
        (
            trimmed_parts,
            part_removed_area,
            part_removed_residual_parts,
            part_removed_residual_area_km2,
        ) = subtract_protected_parts(
            source_part,
            tree,
            protected_parts,
        )
        output_parts.extend(trimmed_parts)
        removed_area += part_removed_area
        removed_residual_parts += part_removed_residual_parts
        removed_residual_area_km2 += part_removed_residual_area_km2

    if removed_area <= AREA_EPSILON:
        return raw_geometry, 0.0, 0, 0.0
    return (
        raw_geometry_from_parts(output_parts, raw_geometry.get("type")),
        removed_area,
        removed_residual_parts,
        removed_residual_area_km2,
    )


def trim_payload(
    payload: dict,
    tree: STRtree,
    protected_parts: list[Polygon],
) -> tuple[dict, dict]:
    output = copy.deepcopy(payload)
    output_features = []
    stats = {
        "processed_features": len(payload.get("features", [])),
        "trimmed_features": 0,
        "removed_features": 0,
        "removed_planar_area": 0.0,
        "removed_residual_parts": 0,
        "removed_residual_area_km2": 0.0,
    }

    for feature in output.get("features", []):
        raw_geometry = feature.get("geometry")
        if not raw_geometry:
            output_features.append(feature)
            continue

        (
            trimmed_geometry,
            removed_area,
            removed_residual_parts,
            removed_residual_area_km2,
        ) = trim_feature_geometry(
            raw_geometry,
            tree,
            protected_parts,
        )
        if removed_area <= AREA_EPSILON:
            output_features.append(feature)
            continue

        stats["removed_planar_area"] += removed_area
        stats["removed_residual_parts"] += removed_residual_parts
        stats["removed_residual_area_km2"] += removed_residual_area_km2
        if trimmed_geometry is None:
            stats["removed_features"] += 1
            continue

        feature["geometry"] = trimmed_geometry
        stats["trimmed_features"] += 1
        output_features.append(feature)

    output["features"] = output_features
    return output, stats


def maximum_overlap_area(
    payload: dict,
    tree: STRtree,
    protected_parts: list[Polygon],
) -> float:
    maximum = 0.0
    for feature in payload.get("features", []):
        for part in raw_geometry_polygon_parts(feature.get("geometry")):
            for raw_index in tree.query(part):
                overlap_area = positive_intersection_area(
                    part,
                    protected_parts[int(raw_index)],
                )
                maximum = max(maximum, overlap_area)
    return maximum


def json_style(raw_bytes: bytes) -> dict:
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    text = raw_bytes.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    indent_match = re.search(r"\r?\n([ \t]+)\"", text)
    return {
        "bom": has_bom,
        "newline": newline,
        "trailing_newline": trailing_newline,
        "indent": indent_match.group(1) if indent_match else None,
    }


def encode_geojson(payload: dict, style: dict, compact: bool = False) -> bytes:
    indent = None if compact else style["indent"]
    if indent is None:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
        )
        if style["newline"] != "\n":
            text = text.replace("\n", style["newline"])

    if style["trailing_newline"]:
        text += style["newline"]
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if style["bom"] else encoded


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_directory(
    target_dir: Path,
    date_filenames: set[str],
    stage_dir: Path,
    compact: bool = False,
) -> dict:
    files = discover_country_files(target_dir)
    file_by_name = {path.name: path for path in files}
    missing_date_files = sorted(date_filenames - file_by_name.keys())
    if missing_date_files:
        raise ValueError(
            f"{target_dir} is missing DATE files: {', '.join(missing_date_files)}"
        )

    protected_paths = [file_by_name[name] for name in sorted(date_filenames)]
    protected_hashes = {path: sha256(path) for path in protected_paths}
    protected_parts = protected_parts_from_files(protected_paths)
    tree = STRtree(protected_parts)

    staged_files = []
    totals = {
        "directory": str(target_dir),
        "country_files": len(files),
        "date_files": len(protected_paths),
        "date_polygon_parts": len(protected_parts),
        "world_files": len(files) - len(protected_paths),
        "changed_files": 0,
        "trimmed_features": 0,
        "removed_features": 0,
        "removed_planar_area": 0.0,
        "removed_residual_parts": 0,
        "removed_residual_area_km2": 0.0,
        "files": [],
    }

    for source_path in files:
        if source_path.name in date_filenames:
            continue

        raw_bytes = source_path.read_bytes()
        payload = read_geojson(source_path)
        output, stats = trim_payload(payload, tree, protected_parts)
        changed = stats["trimmed_features"] or stats["removed_features"]
        if not changed:
            continue

        maximum_overlap = maximum_overlap_area(output, tree, protected_parts)
        if maximum_overlap > AREA_EPSILON:
            raise ValueError(
                f"Residual DATE overlap {maximum_overlap:.12g} in {source_path}"
            )

        stage_path = stage_dir / source_path.name
        stage_path.write_bytes(encode_geojson(
            output,
            json_style(raw_bytes),
            compact=compact,
        ))
        stage_path.chmod(stat.S_IMODE(source_path.stat().st_mode))
        read_geojson(stage_path)
        staged_files.append({
            "source": source_path,
            "stage": stage_path,
        })

        totals["changed_files"] += 1
        totals["trimmed_features"] += stats["trimmed_features"]
        totals["removed_features"] += stats["removed_features"]
        totals["removed_planar_area"] += stats["removed_planar_area"]
        totals["removed_residual_parts"] += stats["removed_residual_parts"]
        totals["removed_residual_area_km2"] += stats[
            "removed_residual_area_km2"
        ]
        totals["files"].append({
            "name": source_path.name,
            "trimmed_features": stats["trimmed_features"],
            "removed_features": stats["removed_features"],
            "removed_residual_parts": stats["removed_residual_parts"],
            "removed_residual_area_km2": stats[
                "removed_residual_area_km2"
            ],
        })

    for path, original_hash in protected_hashes.items():
        if sha256(path) != original_hash:
            raise ValueError(f"DATE source changed while staging: {path}")

    totals["_staged_files"] = staged_files
    totals["_protected_hashes"] = protected_hashes
    return totals


def backup_path_for(source: Path, target_dir: Path, backup_dir: Path) -> Path:
    return backup_dir / target_dir.parent.name / target_dir.name / source.name


def create_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def prioritize_date_boundaries(
    reference_dir: Path,
    target_dirs: list[Path],
    *,
    apply: bool = False,
    backup_dir: Path | None = None,
    compact: bool = False,
) -> dict:
    reference_dir = Path(reference_dir).resolve()
    target_dirs = [Path(directory).resolve() for directory in target_dirs]
    if not reference_dir.is_dir():
        raise ValueError(f"Reference directory does not exist: {reference_dir}")
    if not target_dirs:
        raise ValueError("At least one target directory is required")
    for directory in target_dirs:
        if not directory.is_dir():
            raise ValueError(f"Target directory does not exist: {directory}")

    date_filenames = reference_date_filenames(reference_dir)
    temporary_directories = []
    directory_results = []
    committed = []

    try:
        for target_dir in target_dirs:
            temporary = tempfile.TemporaryDirectory(
                prefix=".date-boundaries-stage-",
                dir=target_dir,
            )
            temporary_directories.append(temporary)
            result = stage_directory(
                target_dir,
                date_filenames,
                Path(temporary.name),
                compact=compact,
            )
            result["_target_dir"] = target_dir
            directory_results.append(result)

        if apply:
            if backup_dir is None:
                backup_dir = Path(tempfile.mkdtemp(
                    prefix="date-mapper-boundaries-backup-",
                ))
            else:
                backup_dir = Path(backup_dir).resolve()
                backup_dir.mkdir(parents=True, exist_ok=True)

            for result in directory_results:
                target_dir = result["_target_dir"]
                for staged in result["_staged_files"]:
                    source = staged["source"]
                    destination = backup_path_for(source, target_dir, backup_dir)
                    create_backup(source, destination)

            try:
                for result in directory_results:
                    for staged in result["_staged_files"]:
                        os.replace(staged["stage"], staged["source"])
                        committed.append((staged["source"], result["_target_dir"]))
            except Exception:
                for source, target_dir in reversed(committed):
                    backup = backup_path_for(source, target_dir, backup_dir)
                    shutil.copy2(backup, source)
                raise

            for result in directory_results:
                for path, original_hash in result["_protected_hashes"].items():
                    if sha256(path) != original_hash:
                        raise ValueError(f"DATE file changed during commit: {path}")

        public_results = []
        for result in directory_results:
            public_results.append({
                key: value
                for key, value in result.items()
                if not key.startswith("_")
            })
        return {
            "applied": apply,
            "reference_directory": str(reference_dir),
            "date_filenames": sorted(date_filenames),
            "backup_directory": str(backup_dir) if apply else None,
            "compact_output": compact,
            "residual_cleanup": {
                "minimum_area_km2": MIN_RESIDUAL_AREA_KM2,
                "maximum_sliver_area_km2": MAX_SLIVER_AREA_KM2,
                "maximum_sliver_compactness": MAX_SLIVER_COMPACTNESS,
            },
            "directories": public_results,
        }
    finally:
        for temporary in temporary_directories:
            temporary.cleanup()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Subtract every DATASET=DATE country from WORLD country geometry "
            "without modifying DATE files."
        ),
        formatter_class=HelpFormatter,
        epilog=(
            "Example:\n"
            "  python Utilities/prioritize_date_country_boundaries.py "
            "data/High-Resolution/Country data/High-Resolution/Country "
            "data/Low-Resolution/Country --apply"
        ),
    )
    parser.add_argument(
        "reference_dir",
        type=Path,
        help="Country directory whose feature DATASET values identify DATE files.",
    )
    parser.add_argument(
        "target_dirs",
        type=Path,
        nargs="+",
        help="One or more country directories to process.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replace changed WORLD files after all targets pass validation.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Backup directory used with --apply. Defaults to a new system temp folder.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write changed WORLD files as compact JSON to reduce disk and transfer size.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional path for the JSON result report.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = prioritize_date_boundaries(
            args.reference_dir,
            args.target_dirs,
            apply=args.apply,
            backup_dir=args.backup_dir,
            compact=args.compact,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
