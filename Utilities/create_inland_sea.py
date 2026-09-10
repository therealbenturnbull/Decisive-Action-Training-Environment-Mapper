#!/usr/bin/env python3
# Aim: Carve a seed-selected inland sea from WORLD countries bounded by DATE countries.
# Author: Benjamin Turnbull

"""Remove an enclosed, DATE-bounded basin from WORLD country geometry.

The basin is selected by a seed point after subtracting the supplied DATE
countries from a regional window. Every supplied WORLD country is then
trimmed by that exact basin. The default mode is a dry run; pass --apply to
atomically replace the WORLD files while leaving all DATE files untouched.
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

from shapely.errors import GEOSException
from shapely.geometry import Point, box
from shapely.validation import make_valid

try:
    from .fill_country_border_gaps import (
        feature_geometry,
        intersects_region_bounds,
        polygon_parts,
        safe_difference,
        safe_intersection_area,
    )
    from .prioritize_date_country_boundaries import (
        AREA_EPSILON,
        WGS84_GEOD,
        create_backup,
        dataset_for_payload,
        discover_country_files,
        encode_geojson,
        json_style,
        polygon_geodesic_metrics,
        raw_geometry_from_parts,
        raw_geometry_polygon_parts,
        read_geojson,
        sha256,
        valid_polygon_parts,
    )
except ImportError:  # Direct script execution.
    from fill_country_border_gaps import (
        feature_geometry,
        intersects_region_bounds,
        polygon_parts,
        safe_difference,
        safe_intersection_area,
    )
    from prioritize_date_country_boundaries import (
        AREA_EPSILON,
        WGS84_GEOD,
        create_backup,
        dataset_for_payload,
        discover_country_files,
        encode_geojson,
        json_style,
        polygon_geodesic_metrics,
        raw_geometry_from_parts,
        raw_geometry_polygon_parts,
        read_geojson,
        sha256,
        valid_polygon_parts,
    )


def payload_geometry(payload: dict, path: Path):
    geometries = [
        geometry
        for feature in payload.get("features", [])
        for geometry in feature_geometry(feature.get("geometry"))
    ]
    if not geometries:
        raise ValueError(f"Country contains no polygon geometry: {path}")

    combined = geometries[0]
    for geometry in geometries[1:]:
        try:
            combined = combined.union(geometry)
        except GEOSException:
            combined = make_valid(combined).union(make_valid(geometry))
    if combined.is_empty or combined.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Expected polygon country geometry: {path}")
    return combined if combined.is_valid else make_valid(combined)


def geodesic_area_km2(geometry) -> float:
    return sum(
        polygon_geodesic_metrics(part)[0]
        for part in polygon_parts(geometry)
    )


def derive_sea_geometry(
    boundary_geometries: list[tuple[Path, object]],
    *,
    region_bounds: tuple[float, float, float, float],
    seed_longitude: float,
    seed_latitude: float,
    minimum_sea_area_km2: float,
):
    min_x, min_y, max_x, max_y = region_bounds
    if min_x >= max_x or min_y >= max_y:
        raise ValueError("Region bounds must have positive width and height")
    if minimum_sea_area_km2 <= 0:
        raise ValueError("Minimum sea area must be positive")

    region = box(*region_bounds)
    seed = Point(seed_longitude, seed_latitude)
    if not region.covers(seed):
        raise ValueError("Seed point must be inside the regional window")

    uncovered = region
    for _path, geometry in boundary_geometries:
        if not intersects_region_bounds(geometry, region_bounds):
            raise ValueError("Every boundary country must intersect the region")
        try:
            clipped = geometry.intersection(region)
        except GEOSException:
            clipped = make_valid(geometry).intersection(region)
        uncovered = safe_difference(uncovered, clipped)

    candidates = [
        candidate
        for candidate in polygon_parts(uncovered)
        if candidate.covers(seed)
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected one basin at the seed point, found {len(candidates)}"
        )
    sea = candidates[0]
    if sea.boundary.intersects(region.boundary):
        raise ValueError("The seed-selected basin is not enclosed by the boundaries")

    area_km2 = geodesic_area_km2(sea)
    if area_km2 < minimum_sea_area_km2:
        raise ValueError(
            f"Seed-selected basin is only {area_km2:.6f} km2"
        )

    boundary_lengths = []
    for path, geometry in boundary_geometries:
        shared = sea.boundary.intersection(geometry.boundary)
        shared_km = (
            WGS84_GEOD.geometry_length(shared) / 1000
            if not shared.is_empty
            else 0.0
        )
        if shared_km <= 0:
            raise ValueError(f"Basin does not share a boundary with {path.name}")
        boundary_lengths.append({"name": path.name, "shared_boundary_km": shared_km})

    return sea, {
        "sea_area_km2": area_km2,
        "sea_bounds": list(sea.bounds),
        "sea_hole_count": len(sea.interiors),
        "boundary_countries": boundary_lengths,
    }


def trim_payload(payload: dict, sea) -> tuple[dict, dict]:
    output = copy.deepcopy(payload)
    output_features = []
    trimmed_features = 0
    removed_features = 0
    removed_planar_area = 0.0
    removed_area_km2 = 0.0

    for feature in output.get("features", []):
        raw_geometry = feature.get("geometry")
        if not raw_geometry:
            output_features.append(feature)
            continue

        output_parts = []
        feature_changed = False
        original_type = raw_geometry.get("type")
        for source_part in raw_geometry_polygon_parts(raw_geometry):
            overlap_area = safe_intersection_area(source_part, sea)
            if overlap_area <= AREA_EPSILON:
                output_parts.append(source_part)
                continue

            overlap = source_part.intersection(sea)
            difference = safe_difference(source_part, sea)
            output_parts.extend(valid_polygon_parts(difference))
            removed_planar_area += overlap_area
            removed_area_km2 += geodesic_area_km2(overlap)
            feature_changed = True

        if not feature_changed:
            output_features.append(feature)
            continue
        if not output_parts:
            removed_features += 1
            continue

        feature["geometry"] = raw_geometry_from_parts(output_parts, original_type)
        trimmed_features += 1
        output_features.append(feature)

    output["features"] = output_features
    return output, {
        "trimmed_features": trimmed_features,
        "removed_features": removed_features,
        "removed_planar_area": removed_planar_area,
        "removed_area_km2": removed_area_km2,
    }


def other_country_owners(
    country_dir: Path,
    sea,
    excluded_paths: set[Path],
) -> list[dict]:
    owners = []
    for path in discover_country_files(country_dir):
        resolved = path.resolve()
        if resolved in excluded_paths:
            continue
        payload = read_geojson(path)
        overlap_area = 0.0
        overlap_area_km2 = 0.0
        for feature in payload.get("features", []):
            for geometry in feature_geometry(feature.get("geometry")):
                if not intersects_region_bounds(geometry, sea.bounds):
                    continue
                planar_area = safe_intersection_area(geometry, sea)
                if planar_area <= AREA_EPSILON:
                    continue
                overlap = geometry.intersection(sea)
                overlap_area += planar_area
                overlap_area_km2 += geodesic_area_km2(overlap)
        if overlap_area > AREA_EPSILON:
            owners.append({
                "name": path.name,
                "overlap_area_km2": overlap_area_km2,
            })
    return owners


def prepare_inland_sea(
    target_payloads: list[tuple[Path, dict]],
    boundary_payloads: list[tuple[Path, dict]],
    *,
    region_bounds: tuple[float, float, float, float],
    seed_longitude: float,
    seed_latitude: float,
    minimum_sea_area_km2: float,
) -> tuple[list[tuple[Path, dict]], object, dict]:
    if not target_payloads:
        raise ValueError("At least one WORLD target country is required")
    if len(boundary_payloads) < 2:
        raise ValueError("At least two DATE boundary countries are required")

    for path, payload in target_payloads:
        if dataset_for_payload(payload, path) != "WORLD":
            raise ValueError(f"Target country is not marked DATASET=WORLD: {path}")
    for path, payload in boundary_payloads:
        if dataset_for_payload(payload, path) != "DATE":
            raise ValueError(f"Boundary country is not marked DATASET=DATE: {path}")

    boundary_geometries = [
        (path, payload_geometry(payload, path))
        for path, payload in boundary_payloads
    ]
    sea, stats = derive_sea_geometry(
        boundary_geometries,
        region_bounds=region_bounds,
        seed_longitude=seed_longitude,
        seed_latitude=seed_latitude,
        minimum_sea_area_km2=minimum_sea_area_km2,
    )

    target_geometries = [
        (path, payload_geometry(payload, path))
        for path, payload in target_payloads
    ]
    target_coverage = target_geometries[0][1]
    for _path, geometry in target_geometries[1:]:
        try:
            target_coverage = target_coverage.union(geometry)
        except GEOSException:
            target_coverage = make_valid(target_coverage).union(make_valid(geometry))

    uncovered = safe_difference(sea, target_coverage)
    uncovered_area_km2 = geodesic_area_km2(uncovered)
    if not uncovered.is_empty and uncovered.area > AREA_EPSILON:
        raise ValueError(
            "WORLD targets do not cover the complete basin "
            f"({uncovered_area_km2:.6f} km2 uncovered)"
        )

    outputs = []
    target_stats = []
    for path, payload in target_payloads:
        output, item_stats = trim_payload(payload, sea)
        if item_stats["removed_planar_area"] <= AREA_EPSILON:
            raise ValueError(f"Basin does not overlap target country: {path}")
        output_geometry = payload_geometry(output, path)
        residual = safe_intersection_area(output_geometry, sea)
        if residual > AREA_EPSILON:
            raise ValueError(
                f"Sea removal left {residual:.12g} overlap in {path.name}"
            )
        outputs.append((path, output))
        target_stats.append({"name": path.name, **item_stats})

    stats["uncovered_target_area_km2"] = uncovered_area_km2
    stats["target_countries"] = target_stats
    return outputs, sea, stats


def create_inland_sea(
    country_dir: Path,
    target_paths: list[Path],
    boundary_paths: list[Path],
    *,
    region_bounds: tuple[float, float, float, float],
    seed_longitude: float,
    seed_latitude: float,
    minimum_sea_area_km2: float = 1.0,
    apply: bool = False,
    backup_dir: Path | None = None,
) -> dict:
    country_dir = Path(country_dir).resolve()
    target_paths = [Path(path).resolve() for path in target_paths]
    boundary_paths = [Path(path).resolve() for path in boundary_paths]
    all_paths = target_paths + boundary_paths
    if not country_dir.is_dir():
        raise ValueError(f"Country directory does not exist: {country_dir}")
    if len(set(all_paths)) != len(all_paths):
        raise ValueError("Target and boundary paths must be unique")
    if any(not path.is_file() for path in all_paths):
        raise ValueError("Every target and boundary country file must exist")

    target_payloads = [(path, read_geojson(path)) for path in target_paths]
    boundary_payloads = [(path, read_geojson(path)) for path in boundary_paths]
    boundary_hashes = {path: sha256(path) for path in boundary_paths}
    outputs, sea, stats = prepare_inland_sea(
        target_payloads,
        boundary_payloads,
        region_bounds=region_bounds,
        seed_longitude=seed_longitude,
        seed_latitude=seed_latitude,
        minimum_sea_area_km2=minimum_sea_area_km2,
    )

    excluded_paths = set(target_paths + boundary_paths)
    owners = other_country_owners(country_dir, sea, excluded_paths)
    if owners:
        names = ", ".join(owner["name"] for owner in owners)
        raise ValueError(f"Basin is also owned by other countries: {names}")

    result = {
        "applied": apply,
        "country_directory": str(country_dir),
        "target_paths": [str(path) for path in target_paths],
        "boundary_paths": [str(path) for path in boundary_paths],
        "region_bounds": list(region_bounds),
        "seed": [seed_longitude, seed_latitude],
        "minimum_sea_area_km2": minimum_sea_area_km2,
        "other_country_owners": owners,
        **stats,
    }
    if not apply:
        result["backup_directory"] = None
        return result

    if backup_dir is None:
        backup_dir = Path(tempfile.mkdtemp(prefix="date-inland-sea-backup-"))
    else:
        backup_dir = Path(backup_dir).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)

    staged = []
    committed = []
    try:
        for source, payload in outputs:
            descriptor, stage_name = tempfile.mkstemp(
                prefix=f".{source.name}.",
                suffix=".stage",
                dir=source.parent,
            )
            os.close(descriptor)
            stage_path = Path(stage_name)
            stage_path.write_bytes(encode_geojson(
                payload,
                json_style(source.read_bytes()),
            ))
            stage_path.chmod(stat.S_IMODE(source.stat().st_mode))
            read_geojson(stage_path)
            staged.append((source, stage_path))

        for source, _stage_path in staged:
            create_backup(source, backup_dir / source.name)

        try:
            for source, stage_path in staged:
                os.replace(stage_path, source)
                committed.append(source)
        except Exception:
            for source in reversed(committed):
                shutil.copy2(backup_dir / source.name, source)
            raise

        for path, original_hash in boundary_hashes.items():
            if sha256(path) != original_hash:
                raise ValueError(f"DATE boundary changed during commit: {path}")
    finally:
        for _source, stage_path in staged:
            stage_path.unlink(missing_ok=True)

    result["backup_directory"] = str(backup_dir)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove a DATE-bounded inland sea from WORLD countries."
    )
    parser.add_argument("country_dir", type=Path)
    parser.add_argument("--target", type=Path, nargs="+", required=True)
    parser.add_argument("--boundary", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--region",
        type=float,
        nargs=4,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        required=True,
    )
    parser.add_argument(
        "--seed",
        type=float,
        nargs=2,
        metavar=("LONGITUDE", "LATITUDE"),
        required=True,
    )
    parser.add_argument("--minimum-sea-area-km2", type=float, default=1.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = create_inland_sea(
            args.country_dir,
            args.target,
            args.boundary,
            region_bounds=tuple(args.region),
            seed_longitude=args.seed[0],
            seed_latitude=args.seed[1],
            minimum_sea_area_km2=args.minimum_sea_area_km2,
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
