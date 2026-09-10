#!/usr/bin/env python3
# Aim: Assign enclosed border gaps to a target country without changing its DATE neighbour.
# Author: Benjamin Turnbull

"""Assign enclosed gaps between two countries to a target country.

The utility subtracts every current country from a caller-supplied regional
window, finds uncovered faces that touch both named country boundaries, and
adds meaningful enclosed faces to the target. The adjacent country remains
untouched. The default mode is a dry run; pass --apply to replace the target.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import shapely
from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.validation import make_valid

try:
    from .prioritize_date_country_boundaries import (
        AREA_EPSILON,
        WGS84_GEOD,
        create_backup,
        dataset_for_payload,
        discover_country_files,
        encode_geojson,
        json_style,
        polygon_geodesic_metrics,
        raw_geometry_polygon_parts,
        read_geojson,
    )
except ImportError:  # Direct script execution.
    from prioritize_date_country_boundaries import (
        AREA_EPSILON,
        WGS84_GEOD,
        create_backup,
        dataset_for_payload,
        discover_country_files,
        encode_geojson,
        json_style,
        polygon_geodesic_metrics,
        raw_geometry_polygon_parts,
        read_geojson,
    )


def polygon_parts(geometry):
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def feature_geometry(raw_geometry: dict | None):
    if not raw_geometry:
        return

    try:
        geometry = shapely.from_geojson(json.dumps(raw_geometry))
        if not geometry.is_valid:
            geometry = make_valid(geometry)
        if geometry.bounds[2] - geometry.bounds[0] <= 180:
            yield geometry
            return
    except GEOSException:
        pass

    yield from raw_geometry_polygon_parts(raw_geometry)


def payload_geometry(payload: dict, path: Path):
    features = payload.get("features", [])
    if len(features) != 1:
        raise ValueError(f"Expected exactly one feature in {path}")
    geometries = list(feature_geometry(features[0].get("geometry")))
    if len(geometries) != 1:
        raise ValueError(f"Expected one polygon geometry in {path}")
    geometry = geometries[0]
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Expected polygon geometry in {path}")
    return geometry


def intersects_region_bounds(geometry, region_bounds) -> bool:
    min_x, min_y, max_x, max_y = geometry.bounds
    region_min_x, region_min_y, region_max_x, region_max_y = region_bounds
    return not (
        max_x < region_min_x
        or min_x > region_max_x
        or max_y < region_min_y
        or min_y > region_max_y
    )


def safe_difference(first, second):
    try:
        return first.difference(second)
    except GEOSException:
        return make_valid(first).difference(make_valid(second))


def safe_intersection_area(first, second) -> float:
    try:
        return first.intersection(second).area
    except GEOSException:
        return make_valid(first).intersection(make_valid(second)).area


def regional_country_geometries(country_dir: Path, region_bounds):
    geometries = []
    for path in discover_country_files(country_dir):
        payload = read_geojson(path)
        for feature in payload.get("features", []):
            for geometry in feature_geometry(feature.get("geometry")):
                if intersects_region_bounds(geometry, region_bounds):
                    geometries.append((path.resolve(), geometry))
    return geometries


def geometry_mapping(geometry) -> dict:
    return json.loads(shapely.to_geojson(geometry))


def prepare_gap_fill(
    target_payload: dict,
    adjacent_payload: dict,
    *,
    target_path: Path,
    adjacent_path: Path,
    country_geometries: list[tuple[Path, object]],
    region_bounds: tuple[float, float, float, float],
    minimum_gap_area_km2: float,
) -> tuple[dict, dict]:
    if dataset_for_payload(target_payload, target_path) == "DATE":
        raise ValueError(f"Target country is marked DATASET=DATE: {target_path}")
    if dataset_for_payload(adjacent_payload, adjacent_path) != "DATE":
        raise ValueError(
            f"Adjacent country is not marked DATASET=DATE: {adjacent_path}"
        )
    if minimum_gap_area_km2 <= 0:
        raise ValueError("Minimum gap area must be positive")

    target = payload_geometry(target_payload, target_path)
    adjacent = payload_geometry(adjacent_payload, adjacent_path)
    region = box(*region_bounds)
    uncovered = region
    for _path, geometry in country_geometries:
        uncovered = safe_difference(uncovered, geometry)

    selected = []
    for candidate in polygon_parts(uncovered):
        area_km2, _compactness = polygon_geodesic_metrics(candidate)
        if area_km2 < minimum_gap_area_km2:
            continue
        if candidate.boundary.intersects(region.boundary):
            continue
        target_edge = candidate.boundary.intersection(target.boundary)
        adjacent_edge = candidate.boundary.intersection(adjacent.boundary)
        if target_edge.length <= AREA_EPSILON:
            continue
        if adjacent_edge.length <= AREA_EPSILON:
            continue
        selected.append((candidate, area_km2))

    if not selected:
        raise ValueError("No enclosed gaps touch both country boundaries")

    expanded = target
    for candidate, _area_km2 in selected:
        expanded = expanded.union(candidate)

    overlap_removed = 0.0
    for path, geometry in country_geometries:
        if path == target_path:
            continue
        overlap_area = safe_intersection_area(expanded, geometry)
        if overlap_area <= AREA_EPSILON:
            continue
        expanded = safe_difference(expanded, geometry)
        overlap_removed += overlap_area

    if expanded.is_empty or not expanded.is_valid:
        raise ValueError("Gap fill produced invalid target geometry")

    for path, geometry in country_geometries:
        if path == target_path:
            continue
        overlap_area = safe_intersection_area(expanded, geometry)
        if overlap_area > AREA_EPSILON:
            raise ValueError(
                f"Gap fill left overlap {overlap_area:.12g} with {path.name}"
            )

    old_shared_km = WGS84_GEOD.geometry_length(
        target.boundary.intersection(adjacent.boundary)
    ) / 1000
    new_shared_km = WGS84_GEOD.geometry_length(
        expanded.boundary.intersection(adjacent.boundary)
    ) / 1000
    if new_shared_km <= old_shared_km:
        raise ValueError("Gap fill did not create a longer defined border")

    output = copy.deepcopy(target_payload)
    output["features"][0]["geometry"] = geometry_mapping(expanded)
    stats = {
        "filled_gap_count": len(selected),
        "filled_area_km2": sum(area for _candidate, area in selected),
        "old_shared_boundary_km": old_shared_km,
        "new_shared_boundary_km": new_shared_km,
        "overlap_removed_planar_area": overlap_removed,
        "gaps": [
            {
                "area_km2": area_km2,
                "bounds": list(candidate.bounds),
            }
            for candidate, area_km2 in sorted(
                selected,
                key=lambda item: item[1],
                reverse=True,
            )
        ],
    }
    return output, stats


def fill_country_border_gaps(
    country_dir: Path,
    target_path: Path,
    adjacent_path: Path,
    *,
    region_bounds: tuple[float, float, float, float],
    minimum_gap_area_km2: float = 0.01,
    apply: bool = False,
    backup_dir: Path | None = None,
) -> dict:
    country_dir = Path(country_dir).resolve()
    target_path = Path(target_path).resolve()
    adjacent_path = Path(adjacent_path).resolve()
    if not country_dir.is_dir():
        raise ValueError(f"Country directory does not exist: {country_dir}")
    if not target_path.is_file() or not adjacent_path.is_file():
        raise ValueError("Target and adjacent country files must exist")

    target_payload = read_geojson(target_path)
    adjacent_payload = read_geojson(adjacent_path)
    country_geometries = regional_country_geometries(
        country_dir,
        region_bounds,
    )
    output, stats = prepare_gap_fill(
        target_payload,
        adjacent_payload,
        target_path=target_path,
        adjacent_path=adjacent_path,
        country_geometries=country_geometries,
        region_bounds=region_bounds,
        minimum_gap_area_km2=minimum_gap_area_km2,
    )

    result = {
        "applied": apply,
        "country_directory": str(country_dir),
        "target_path": str(target_path),
        "adjacent_path": str(adjacent_path),
        "region_bounds": list(region_bounds),
        "minimum_gap_area_km2": minimum_gap_area_km2,
        **stats,
    }
    if not apply:
        result["backup_directory"] = None
        return result

    if backup_dir is None:
        backup_dir = Path(tempfile.mkdtemp(prefix="date-gap-fill-backup-"))
    else:
        backup_dir = Path(backup_dir).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)

    descriptor, stage_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".stage",
        dir=target_path.parent,
    )
    os.close(descriptor)
    stage_path = Path(stage_name)
    backup_path = backup_dir / target_path.name
    try:
        stage_path.write_bytes(encode_geojson(
            output,
            json_style(target_path.read_bytes()),
        ))
        stage_path.chmod(stat.S_IMODE(target_path.stat().st_mode))
        read_geojson(stage_path)
        create_backup(target_path, backup_path)
        try:
            os.replace(stage_path, target_path)
        except Exception:
            shutil.copy2(backup_path, target_path)
            raise
    finally:
        stage_path.unlink(missing_ok=True)

    result["backup_directory"] = str(backup_dir)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Fill enclosed gaps between a WORLD and DATE country."
    )
    parser.add_argument("country_dir", type=Path)
    parser.add_argument("target_path", type=Path)
    parser.add_argument("adjacent_path", type=Path)
    parser.add_argument(
        "--region",
        type=float,
        nargs=4,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        required=True,
    )
    parser.add_argument("--minimum-gap-area-km2", type=float, default=0.01)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = fill_country_border_gaps(
            args.country_dir,
            args.target_path,
            args.adjacent_path,
            region_bounds=tuple(args.region),
            minimum_gap_area_km2=args.minimum_gap_area_km2,
            apply=args.apply,
            backup_dir=args.backup_dir,
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
