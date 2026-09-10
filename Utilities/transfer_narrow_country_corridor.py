#!/usr/bin/env python3
# Aim: Transfer a narrow attached WORLD corridor to its adjacent DATE country.
# Author: Benjamin Turnbull

"""Transfer a narrow attached WORLD corridor to an adjacent DATE country.

The corridor is detected within the seed-selected WORLD feature by eroding its
geometry until a narrow neck breaks, rebuilding only the largest mainland
component, and selecting the detached component containing the seed point.
The default mode is a dry run. Pass --apply to atomically replace both source
files.
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

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon
from shapely.validation import make_valid

try:
    from .prioritize_date_country_boundaries import (
        create_backup,
        encode_geojson,
        json_style,
        read_geojson,
    )
except ImportError:  # Direct script execution.
    from prioritize_date_country_boundaries import (
        create_backup,
        encode_geojson,
        json_style,
        read_geojson,
    )


PROJECTED_CRS = "EPSG:6933"
OVERLAP_EPSILON_M2 = 0.01


def polygon_parts(geometry):
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def transform_geometry(geometry, transformer: Transformer):
    def transform_coordinates(coordinates):
        x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        return np.column_stack((x, y))

    return shapely.transform(geometry, transform_coordinates)


def geometry_for_feature(feature: dict, path: Path, feature_index: int):
    raw_geometry = feature.get("geometry")
    if not raw_geometry:
        raise ValueError(f"Missing geometry for feature {feature_index} in {path}")

    geometry = shapely.from_geojson(json.dumps(raw_geometry))
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            f"Expected polygon geometry for feature {feature_index} in {path}"
        )
    return geometry


def feature_geometry(payload: dict, path: Path):
    features = payload.get("features", [])
    if len(features) != 1:
        raise ValueError(f"Expected exactly one feature in {path}")
    return geometry_for_feature(features[0], path, 0)


def seed_feature_geometry(
    payload: dict,
    path: Path,
    seed: Point,
) -> tuple[int, object]:
    matches = []
    for feature_index, feature in enumerate(payload.get("features", [])):
        geometry = geometry_for_feature(feature, path, feature_index)
        if geometry.covers(seed):
            matches.append((feature_index, geometry))

    if len(matches) != 1:
        raise ValueError(
            f"Expected the seed to select one feature in {path}, found {len(matches)}"
        )
    return matches[0]


def payload_datasets(payload: dict) -> set[str]:
    return {
        str((feature.get("properties") or {}).get("DATASET") or "")
        .strip()
        .upper()
        for feature in payload.get("features", [])
    }


def detect_corridor(
    world_geometry,
    seed: Point,
    erosion_metres: float,
):
    eroded = world_geometry.buffer(-erosion_metres)
    cores = list(polygon_parts(eroded))
    if not cores:
        raise ValueError("Erosion removed the entire WORLD geometry")

    mainland_core = max(cores, key=lambda part: part.area)
    rebuilt_mainland = mainland_core.buffer(erosion_metres).intersection(
        world_geometry
    )
    candidates = list(polygon_parts(world_geometry.difference(rebuilt_mainland)))
    if not candidates:
        raise ValueError("No narrow corridor candidates were detected")

    corridor = min(candidates, key=lambda part: part.distance(seed))
    if corridor.distance(seed) > 1.0:
        raise ValueError("The seed point is not inside a detected corridor")
    return corridor


def geometry_mapping(geometry) -> dict:
    return json.loads(shapely.to_geojson(geometry))


def repair_polygonal_geometry(geometry, label: str):
    if geometry.is_valid:
        return geometry

    repaired = geometry.buffer(0)
    if repaired.is_empty or repaired.geom_type not in {"Polygon", "MultiPolygon"}:
        repaired = make_valid(geometry)
    if repaired.is_empty or repaired.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{label} reprojection did not produce polygon geometry")
    if not repaired.is_valid:
        raise ValueError(f"{label} reprojection produced invalid geometry")
    return repaired


def prepare_transfer(
    world_payload: dict,
    date_payload: dict,
    *,
    world_path: Path,
    date_path: Path,
    seed_longitude: float,
    seed_latitude: float,
    erosion_km: float,
) -> tuple[dict, dict, dict]:
    if "DATE" in payload_datasets(world_payload):
        raise ValueError(f"WORLD source is marked DATASET=DATE: {world_path}")
    if payload_datasets(date_payload) != {"DATE"}:
        raise ValueError(f"DATE source is not marked DATASET=DATE: {date_path}")
    if erosion_km <= 0:
        raise ValueError("Erosion distance must be positive")

    seed_wgs84 = Point(seed_longitude, seed_latitude)
    world_feature_index, world_wgs84 = seed_feature_geometry(
        world_payload,
        world_path,
        seed_wgs84,
    )
    date_wgs84 = feature_geometry(date_payload, date_path)
    forward = Transformer.from_crs("EPSG:4326", PROJECTED_CRS, always_xy=True)
    inverse = Transformer.from_crs(PROJECTED_CRS, "EPSG:4326", always_xy=True)
    world_projected = transform_geometry(world_wgs84, forward)
    date_projected = transform_geometry(date_wgs84, forward)
    seed_x, seed_y = forward.transform(seed_longitude, seed_latitude)
    corridor = detect_corridor(
        world_projected,
        Point(seed_x, seed_y),
        erosion_km * 1000,
    )

    corridor_date_boundary_km = (
        corridor.boundary.intersection(date_projected.boundary).length / 1000
    )
    if corridor_date_boundary_km <= 0:
        raise ValueError("Detected corridor does not border the DATE country")

    old_shared_boundary_km = (
        world_projected.boundary.intersection(date_projected.boundary).length
        / 1000
    )
    new_world_projected = world_projected.difference(corridor)
    new_date_projected = date_projected.union(corridor)
    if new_world_projected.is_empty or new_date_projected.is_empty:
        raise ValueError("Corridor transfer produced an empty country geometry")
    if not new_world_projected.is_valid or not new_date_projected.is_valid:
        raise ValueError("Corridor transfer produced invalid geometry")

    new_world = repair_polygonal_geometry(
        transform_geometry(new_world_projected, inverse),
        "WORLD",
    )
    new_date = repair_polygonal_geometry(
        transform_geometry(new_date_projected, inverse),
        "DATE",
    )
    validated_world_projected = transform_geometry(new_world, forward)
    validated_date_projected = transform_geometry(new_date, forward)
    overlap_m2 = validated_world_projected.intersection(
        validated_date_projected
    ).area
    if overlap_m2 > OVERLAP_EPSILON_M2:
        raise ValueError(f"Corridor transfer left {overlap_m2:.6f} m2 overlap")

    new_shared_boundary_km = (
        validated_world_projected.boundary.intersection(
            validated_date_projected.boundary
        ).length
        / 1000
    )
    if new_shared_boundary_km >= old_shared_boundary_km:
        raise ValueError("Corridor transfer did not shorten the shared boundary")
    output_world = copy.deepcopy(world_payload)
    output_date = copy.deepcopy(date_payload)
    output_world["features"][world_feature_index]["geometry"] = geometry_mapping(
        new_world
    )
    output_date["features"][0]["geometry"] = geometry_mapping(new_date)

    stats = {
        "world_feature_index": world_feature_index,
        "corridor_area_km2": corridor.area / 1_000_000,
        "corridor_date_boundary_km": corridor_date_boundary_km,
        "old_shared_boundary_km": old_shared_boundary_km,
        "new_shared_boundary_km": new_shared_boundary_km,
        "replacement_boundary_km": (
            corridor.boundary.intersection(new_world_projected.boundary).length
            / 1000
        ),
        "overlap_m2": overlap_m2,
    }
    return output_world, output_date, stats


def transfer_narrow_corridor(
    world_path: Path,
    date_path: Path,
    *,
    seed_longitude: float,
    seed_latitude: float,
    erosion_km: float,
    apply: bool = False,
    backup_dir: Path | None = None,
) -> dict:
    world_path = Path(world_path).resolve()
    date_path = Path(date_path).resolve()
    world_payload = read_geojson(world_path)
    date_payload = read_geojson(date_path)
    output_world, output_date, stats = prepare_transfer(
        world_payload,
        date_payload,
        world_path=world_path,
        date_path=date_path,
        seed_longitude=seed_longitude,
        seed_latitude=seed_latitude,
        erosion_km=erosion_km,
    )

    result = {
        "applied": apply,
        "world_path": str(world_path),
        "date_path": str(date_path),
        "seed": [seed_longitude, seed_latitude],
        "erosion_km": erosion_km,
        **stats,
    }
    if not apply:
        result["backup_directory"] = None
        return result

    if backup_dir is None:
        backup_dir = Path(tempfile.mkdtemp(prefix="date-corridor-backup-"))
    else:
        backup_dir = Path(backup_dir).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)

    sources = [world_path, date_path]
    outputs = [output_world, output_date]
    staged_paths = []
    backups = []
    try:
        for source, payload in zip(sources, outputs):
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
            staged_paths.append(stage_path)

        for source in sources:
            backup = backup_dir / source.name
            create_backup(source, backup)
            backups.append(backup)

        try:
            for source, stage_path in zip(sources, staged_paths):
                os.replace(stage_path, source)
        except Exception:
            for source, backup in zip(sources, backups):
                shutil.copy2(backup, source)
            raise
    finally:
        for stage_path in staged_paths:
            stage_path.unlink(missing_ok=True)

    result["backup_directory"] = str(backup_dir)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Transfer a narrow corridor from one seed-selected WORLD feature "
            "to an adjacent DATE country."
        )
    )
    parser.add_argument("world_path", type=Path)
    parser.add_argument("date_path", type=Path)
    parser.add_argument("--seed-longitude", type=float, required=True)
    parser.add_argument("--seed-latitude", type=float, required=True)
    parser.add_argument("--erosion-km", type=float, default=1.5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = transfer_narrow_corridor(
            args.world_path,
            args.date_path,
            seed_longitude=args.seed_longitude,
            seed_latitude=args.seed_latitude,
            erosion_km=args.erosion_km,
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
