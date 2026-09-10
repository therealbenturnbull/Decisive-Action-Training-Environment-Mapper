#!/usr/bin/env python3
# Aim: Build a mirrored low-resolution GeoJSON tree from higher-resolution source data.
# Author: Benjamin Turnbull

"""
Create lower-fidelity GeoJSON copies from a folder tree.

The script recursively reads .geojson files from an input directory, simplifies
line and polygon coordinates, and writes the results to a separate output
directory while mirroring the original folder structure.

By default, simplification is local: it removes dense points, and points that
do not meaningfully change the direction of the line being drawn. This is more
conservative for borders/coastlines than drawing long replacement chords.

Tolerance means: “remove points only when the replacement segment stays within
this distance of the removed point.”

Smaller tolerance = more detail, larger files.
Larger tolerance = less detail, smaller files.
Units match your coordinates. For lon/lat GeoJSON, that means degrees.
Rough guide at the equator:
 - 0.00001 ≈ 1.1 m
 - 0.0001 ≈ 11 m
 - 0.001 ≈ 111 m

Example:
    python geojson_simplify_folder.py data/raw data/simplified --tolerance 0.0001
"""

import argparse
import copy
import json
import math
import sys
from pathlib import Path


GEOJSON_FILE_EXTENSIONS = {".geojson", ".json"}
GEOJSON_TYPES = {
    "FeatureCollection",
    "Feature",
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}
LOCAL_SIMPLIFY_MAX_PASSES = 8


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


def load_geojson(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def is_geojson_object(data):
    if not isinstance(data, dict):
        return False

    geojson_type = data.get("type")
    if geojson_type not in GEOJSON_TYPES:
        return False

    if geojson_type == "FeatureCollection":
        return isinstance(data.get("features"), list)

    if geojson_type == "Feature":
        return "geometry" in data

    if geojson_type == "GeometryCollection":
        return isinstance(data.get("geometries"), list)

    return "coordinates" in data


def write_geojson(data, path, compact):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def point_distance(point, segment_start, segment_end):
    x, y = point[:2]
    x1, y1 = segment_start[:2]
    x2, y2 = segment_end[:2]

    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)

    position = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    position = max(0, min(1, position))
    nearest_x = x1 + position * dx
    nearest_y = y1 + position * dy
    return math.hypot(x - nearest_x, y - nearest_y)


def coordinate_distance(first_point, second_point):
    return math.hypot(
        second_point[0] - first_point[0],
        second_point[1] - first_point[1],
    )


def direction_change_degrees(previous_point, point, next_point):
    first_vector = (
        point[0] - previous_point[0],
        point[1] - previous_point[1],
    )
    second_vector = (
        next_point[0] - point[0],
        next_point[1] - point[1],
    )
    first_length = math.hypot(*first_vector)
    second_length = math.hypot(*second_vector)

    if first_length == 0 or second_length == 0:
        return 0.0

    cosine = (
        first_vector[0] * second_vector[0]
        + first_vector[1] * second_vector[1]
    ) / (first_length * second_length)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


def locally_insignificant_point(previous_point, point, next_point, tolerance, angle_tolerance):
    distance_from_chord = point_distance(point, previous_point, next_point)
    direction_change = direction_change_degrees(previous_point, point, next_point)
    nearest_neighbor_distance = min(
        coordinate_distance(previous_point, point),
        coordinate_distance(point, next_point),
    )

    return (
        distance_from_chord <= tolerance
        and (
            direction_change <= angle_tolerance
            or nearest_neighbor_distance <= tolerance
        )
    )


def simplify_local_line(points, tolerance, angle_tolerance):
    if len(points) <= 2:
        return points

    simplified = points
    for _ in range(LOCAL_SIMPLIFY_MAX_PASSES):
        changed = False
        next_points = [simplified[0]]
        index = 1

        while index < len(simplified) - 1:
            remaining_after_current = len(simplified) - index - 1
            if len(next_points) + remaining_after_current <= 2:
                next_points.append(simplified[index])
                index += 1
                continue

            previous_point = next_points[-1]
            point = simplified[index]
            next_point = simplified[index + 1]

            if locally_insignificant_point(
                previous_point,
                point,
                next_point,
                tolerance,
                angle_tolerance,
            ):
                changed = True
            else:
                next_points.append(point)

            index += 1

        next_points.append(simplified[-1])
        simplified = next_points

        if not changed:
            break

    return simplified


def simplify_local_ring(ring, tolerance, angle_tolerance):
    ring = ensure_closed_ring(ring)
    points = ring[:-1]

    if len(points) <= 3:
        return ring

    for _ in range(LOCAL_SIMPLIFY_MAX_PASSES):
        remove_indices = set()
        point_count = len(points)

        if point_count <= 3:
            break

        for index, point in enumerate(points):
            if point_count - len(remove_indices) <= 3:
                break

            previous_index = (index - 1) % point_count
            next_index = (index + 1) % point_count
            if previous_index in remove_indices or next_index in remove_indices:
                continue

            if locally_insignificant_point(
                points[previous_index],
                point,
                points[next_index],
                tolerance,
                angle_tolerance,
            ):
                remove_indices.add(index)

        if not remove_indices:
            break

        points = [
            point
            for index, point in enumerate(points)
            if index not in remove_indices
        ]

    return ensure_closed_ring(points)


def douglas_peucker(points, tolerance):
    if len(points) <= 2:
        return points

    max_distance = -1
    max_index = 0

    for index in range(1, len(points) - 1):
        distance = point_distance(points[index], points[0], points[-1])
        if distance > max_distance:
            max_distance = distance
            max_index = index

    if max_distance > tolerance:
        left = douglas_peucker(points[: max_index + 1], tolerance)
        right = douglas_peucker(points[max_index:], tolerance)
        return left[:-1] + right

    return [points[0], points[-1]]


def ensure_closed_ring(ring):
    if not ring:
        return ring

    if ring[0] == ring[-1]:
        return ring

    return ring + [ring[0]]


def simplify_ring(ring, tolerance):
    ring = ensure_closed_ring(ring)
    unique_points = ring[:-1]

    if len(unique_points) <= 3:
        return ring

    start_index = min(
        range(len(unique_points)),
        key=lambda index: (unique_points[index][0], unique_points[index][1]),
    )
    rotated = unique_points[start_index:] + unique_points[:start_index]
    farthest_index = max(
        range(1, len(rotated)),
        key=lambda index: point_distance(rotated[index], rotated[0], rotated[0]),
    )

    first_arc = rotated[: farthest_index + 1]
    second_arc = rotated[farthest_index:] + [rotated[0]]
    simplified = (
        douglas_peucker(first_arc, tolerance)[:-1]
        + douglas_peucker(second_arc, tolerance)[:-1]
    )

    if len(simplified) < 3:
        return ring

    return ensure_closed_ring(simplified)


def coordinate_count(raw_geometry):
    if raw_geometry is None:
        return 0

    geometry_type = raw_geometry.get("type")
    if geometry_type == "GeometryCollection":
        return sum(
            coordinate_count(geometry)
            for geometry in raw_geometry.get("geometries", [])
        )

    def count_coordinates(value):
        if not isinstance(value, list):
            return 0

        if value and all(isinstance(item, (int, float)) for item in value):
            return 1

        return sum(count_coordinates(item) for item in value)

    return count_coordinates(raw_geometry.get("coordinates", []))


def simplify_line(points, tolerance, angle_tolerance, method):
    if method == "douglas-peucker":
        return douglas_peucker(points, tolerance)

    return simplify_local_line(points, tolerance, angle_tolerance)


def simplify_polygon_ring(ring, tolerance, angle_tolerance, method):
    if method == "douglas-peucker":
        return simplify_ring(ring, tolerance)

    return simplify_local_ring(ring, tolerance, angle_tolerance)


def simplify_raw_geometry(raw_geometry, tolerance, angle_tolerance, method):
    if raw_geometry is None:
        return None

    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates")

    if geometry_type == "LineString":
        return {
            **raw_geometry,
            "coordinates": simplify_line(
                coordinates or [],
                tolerance,
                angle_tolerance,
                method,
            ),
        }

    if geometry_type == "MultiLineString":
        return {
            **raw_geometry,
            "coordinates": [
                simplify_line(line, tolerance, angle_tolerance, method)
                for line in coordinates or []
            ],
        }

    if geometry_type == "Polygon":
        return {
            **raw_geometry,
            "coordinates": [
                simplify_polygon_ring(ring, tolerance, angle_tolerance, method)
                for ring in coordinates or []
            ],
        }

    if geometry_type == "MultiPolygon":
        return {
            **raw_geometry,
            "coordinates": [
                [
                    simplify_polygon_ring(ring, tolerance, angle_tolerance, method)
                    for ring in polygon
                ]
                for polygon in coordinates or []
            ],
        }

    if geometry_type == "GeometryCollection":
        return {
            **raw_geometry,
            "geometries": [
                simplify_raw_geometry(
                    geometry,
                    tolerance,
                    angle_tolerance,
                    method,
                )
                for geometry in raw_geometry.get("geometries", [])
            ],
        }

    return raw_geometry


def simplify_geojson(data, tolerance, angle_tolerance, method):
    output = copy.deepcopy(data)
    stats = {
        "features": 0,
        "geometries": 0,
        "input_coordinates": 0,
        "output_coordinates": 0,
    }

    def simplify_geometry_holder(holder):
        raw_geometry = holder.get("geometry")
        stats["input_coordinates"] += coordinate_count(raw_geometry)
        holder["geometry"] = simplify_raw_geometry(
            raw_geometry,
            tolerance,
            angle_tolerance,
            method,
        )
        stats["output_coordinates"] += coordinate_count(holder.get("geometry"))
        stats["geometries"] += 1

    geojson_type = output.get("type")

    if geojson_type == "FeatureCollection":
        for feature in output.get("features", []):
            simplify_geometry_holder(feature)
            stats["features"] += 1
        return output, stats

    if geojson_type == "Feature":
        simplify_geometry_holder(output)
        stats["features"] = 1
        return output, stats

    if "coordinates" in output or geojson_type == "GeometryCollection":
        stats["input_coordinates"] = coordinate_count(output)
        output = simplify_raw_geometry(output, tolerance, angle_tolerance, method)
        stats["output_coordinates"] = coordinate_count(output)
        stats["geometries"] = 1
        return output, stats

    raise ValueError("Not a valid GeoJSON FeatureCollection, Feature, or geometry object.")


def safe_output_dir(input_dir, output_dir):
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    if input_dir == output_dir:
        raise ValueError("Output directory must be different from the input directory.")


def path_is_relative_to(path, parent):
    # A string-prefix check was used briefly, but paths such as /maps and
    # /maps-old share the same prefix without having a parent-child relationship.
    # return str(path).startswith(str(parent))
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def simplify_folder(input_dir, output_dir, tolerance, angle_tolerance, method, compact):
    safe_output_dir(input_dir, output_dir)
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        path
        for path in input_dir.rglob("*")
        if (
            path.is_file()
            and path.suffix.lower() in GEOJSON_FILE_EXTENSIONS
            and not path_is_relative_to(path.resolve(), output_dir)
        )
    )
    totals = {
        "files": len(files),
        "features": 0,
        "geometries": 0,
        "input_coordinates": 0,
        "output_coordinates": 0,
        "skipped_non_geojson_json": 0,
        "errors": 0,
    }

    if not files:
        print(
            f"No .geojson or .json files found in {input_dir}. "
            f"Output folder is {output_dir}.",
            file=sys.stderr,
        )
        return totals

    for input_path in files:
        output_path = output_dir / input_path.relative_to(input_dir)

        try:
            data = load_geojson(input_path)
            if input_path.suffix.lower() == ".json" and not is_geojson_object(data):
                totals["skipped_non_geojson_json"] += 1
                print(f"Skipped non-GeoJSON JSON file: {input_path}", file=sys.stderr)
                continue

            simplified, stats = simplify_geojson(
                data,
                tolerance,
                angle_tolerance,
                method,
            )
            write_geojson(simplified, output_path, compact)
        except Exception as exc:
            totals["errors"] += 1
            print(f"Error: {input_path}: {exc}", file=sys.stderr)
            continue

        for key in (
            "features",
            "geometries",
            "input_coordinates",
            "output_coordinates",
        ):
            totals[key] += stats[key]

        print(
            f"Wrote {output_path} "
            f"({stats['input_coordinates']} -> {stats['output_coordinates']} coordinates)",
            file=sys.stderr,
        )

    return totals


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Recursively simplify .geojson files into a mirrored output folder.",
        formatter_class=HelpFormatter,
        add_help=False,
        epilog=(
            "Notes:\n"
            "  Tolerance is the maximum allowed distance between a removed geometry\n"
            "  point and its local replacement segment. Smaller values preserve more\n"
            "  detail and produce larger files; larger values remove more points and\n"
            "  produce smaller files.\n\n"
            "  The default --method local removes points when they are tightly clustered\n"
            "  or do not materially change line direction. This is usually safer for\n"
            "  country boundaries than Douglas-Peucker, which can draw longer replacement\n"
            "  chords across detailed borders or coastlines.\n\n"
            "  --angle-tolerance controls what counts as a negligible direction change.\n"
            "  Smaller values keep more bends; larger values remove more nearly-straight\n"
            "  points.\n\n"
            "  Tolerance uses the same units as the GeoJSON coordinates. For common\n"
            "  lon/lat GeoJSON in degrees, rough equator distances are:\n"
            "    0.00001 = about 1.1 metres\n"
            "    0.0001  = about 11 metres\n"
            "    0.001   = about 111 metres\n\n"
            "  Files ending in .geojson are always processed. Files ending in .json are\n"
            "  processed too when they contain a GeoJSON object such as FeatureCollection,\n"
            "  Feature, Polygon, or MultiPolygon.\n\n"
            "  The output folder may be inside the input folder. Files already in the\n"
            "  output folder are skipped so repeat runs do not simplify their own output.\n\n"
            "Examples:\n"
            "  python geojson_simplify_folder.py data/original data/simplified\n"
            "  python geojson_simplify_folder.py data/original data/simplified "
            "--tolerance 0.001 --compact"
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        default=argparse.SUPPRESS,
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Folder containing .geojson files, including nested folders.",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="New folder where simplified files will be written.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.0001,
        help="Simplification tolerance in coordinate units.",
    )
    parser.add_argument(
        "--angle-tolerance",
        type=float,
        default=3.0,
        help="Maximum local direction change, in degrees, for removing a point with --method local.",
    )
    parser.add_argument(
        "--method",
        choices=("local", "douglas-peucker"),
        default="local",
        help="Simplification method. Local is safer for country boundaries; Douglas-Peucker is more aggressive.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of pretty-printed JSON.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"Input directory does not exist: {args.input_dir}", file=sys.stderr)
        return 1

    if args.tolerance < 0:
        print("Tolerance must be zero or greater.", file=sys.stderr)
        return 1

    if args.angle_tolerance < 0:
        print("Angle tolerance must be zero or greater.", file=sys.stderr)
        return 1

    try:
        totals = simplify_folder(
            args.input_dir,
            args.output_dir,
            args.tolerance,
            angle_tolerance=args.angle_tolerance,
            method=args.method,
            compact=args.compact,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    reduction = 0.0
    if totals["input_coordinates"]:
        reduction = 100 * (
            1 - totals["output_coordinates"] / totals["input_coordinates"]
        )

    print(
        "Processed {files} file(s), {features} feature(s), "
        "{geometries} geometry object(s). "
        "Coordinates: {input_coordinates} -> {output_coordinates} "
        "({reduction:.1f}% reduction). "
        "Skipped non-GeoJSON JSON files: {skipped_non_geojson_json}. "
        "Errors: {errors}. "
        "Output folder: {output_dir}".format(
            reduction=reduction,
            output_dir=args.output_dir.resolve(),
            **totals,
        ),
        file=sys.stderr,
    )

    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
