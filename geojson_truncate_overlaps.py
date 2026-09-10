#!/usr/bin/env python3
# Aim: Trim overlapping GeoJSON geometry while preserving a designated source layer.
# Author: Benjamin Turnbull

"""
Trim overlaps between two GeoJSON inputs by subtracting one from the other.

Default usage keeps the first GeoJSON intact and truncates the second:

    python geojson_truncate_overlaps.py protected.geojson target.geojson -o target_trimmed.geojson

Use --combined-output to write both non-overlapping layers into one FeatureCollection.
"""

import argparse
import copy
import json
import sys
from functools import reduce
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.validation import make_valid


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def load_geojson(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def write_geojson(data, path=None, indent=2):
    if path:
        with Path(path).open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")
    else:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=indent)
        sys.stdout.write("\n")


def features_from_geojson(data, source_name="GeoJSON"):
    geojson_type = data.get("type")

    if geojson_type == "FeatureCollection":
        return data.get("features", [])

    if geojson_type == "Feature":
        return [data]

    if geojson_type:
        return [{
            "type": "Feature",
            "properties": {},
            "geometry": data,
        }]

    raise ValueError(f"{source_name} is not a valid GeoJSON object")


def clean_geometry(geometry):
    if geometry.is_empty:
        return geometry

    if geometry.is_valid:
        return geometry

    fixed = make_valid(geometry)
    if fixed.is_valid:
        return fixed

    return fixed.buffer(0)


def union_geometries(geometries):
    return clean_geometry(reduce(lambda left, right: left.union(right), geometries))


def polygon_from_coordinates(coordinates):
    if not coordinates:
        return Polygon()

    shell = coordinates[0]
    holes = coordinates[1:]
    return Polygon(shell, holes)


def geometry_from_geojson(raw_geometry):
    """
    Convert a GeoJSON geometry to Shapely without using Shapely's collection
    constructor for multi-geometries, which can fail in some local builds.
    """
    geometry_type = raw_geometry.get("type")
    coordinates = raw_geometry.get("coordinates")

    if geometry_type == "Polygon":
        return clean_geometry(polygon_from_coordinates(coordinates))

    if geometry_type == "MultiPolygon":
        polygons = [
            clean_geometry(polygon_from_coordinates(polygon_coordinates))
            for polygon_coordinates in coordinates or []
        ]
        polygons = [polygon for polygon in polygons if not polygon.is_empty]
        if not polygons:
            return Polygon()
        if len(polygons) == 1:
            return polygons[0]
        return union_geometries(polygons)

    if geometry_type == "Point":
        return Point(coordinates)

    if geometry_type == "MultiPoint":
        points = [Point(point_coordinates) for point_coordinates in coordinates or []]
        points = [point for point in points if not point.is_empty]
        if not points:
            return Point()
        return union_geometries(points)

    if geometry_type == "LineString":
        return LineString(coordinates)

    if geometry_type == "MultiLineString":
        lines = [LineString(line_coordinates) for line_coordinates in coordinates or []]
        lines = [line for line in lines if not line.is_empty]
        if not lines:
            return LineString()
        return union_geometries(lines)

    if geometry_type == "GeometryCollection":
        geometries = [
            geometry_from_geojson(geometry)
            for geometry in raw_geometry.get("geometries", [])
        ]
        geometries = [geometry for geometry in geometries if not geometry.is_empty]
        if not geometries:
            return Polygon()
        return union_geometries(geometries)

    return clean_geometry(shape(raw_geometry))


def geometry_union(features):
    geometries = []
    for feature in features:
        raw_geometry = feature.get("geometry")
        if raw_geometry is None:
            continue

        geometry = geometry_from_geojson(raw_geometry)
        if not geometry.is_empty:
            geometries.append(geometry)

    if not geometries:
        return None

    return union_geometries(geometries)


def truncate_features(features_to_truncate, overlap_source_features, keep_empty=False):
    overlap_geometry = geometry_union(overlap_source_features)
    stats = {
        "processed_features": len(features_to_truncate),
        "trimmed_features": 0,
        "removed_features": 0,
        "empty_features": 0,
        "removed_area": 0.0,
    }

    if overlap_geometry is None or overlap_geometry.is_empty:
        return copy.deepcopy(features_to_truncate), stats

    output_features = []

    for feature in features_to_truncate:
        new_feature = copy.deepcopy(feature)
        raw_geometry = new_feature.get("geometry")

        if raw_geometry is None:
            output_features.append(new_feature)
            continue

        geometry = geometry_from_geojson(raw_geometry)
        if geometry.is_empty:
            stats["empty_features"] += 1
            if keep_empty:
                new_feature["geometry"] = None
                output_features.append(new_feature)
            else:
                stats["removed_features"] += 1
            continue

        original_area = geometry.area
        if geometry.intersects(overlap_geometry):
            geometry = clean_geometry(geometry.difference(overlap_geometry))

        if geometry.is_empty:
            stats["removed_area"] += original_area
            if keep_empty:
                new_feature["geometry"] = None
                output_features.append(new_feature)
            else:
                stats["removed_features"] += 1
            continue

        removed_area = original_area - geometry.area
        if removed_area > 0:
            stats["trimmed_features"] += 1
            stats["removed_area"] += removed_area

        new_feature["geometry"] = mapping(geometry)
        output_features.append(new_feature)

    return output_features, stats


def build_truncated_collection(first_data, second_data, truncate="second", keep_empty=False, combined_output=False):
    first_features = features_from_geojson(first_data, "first GeoJSON")
    second_features = features_from_geojson(second_data, "second GeoJSON")

    if truncate == "first":
        truncated_features, stats = truncate_features(first_features, second_features, keep_empty)
        output_features = (
            copy.deepcopy(second_features) + truncated_features
            if combined_output
            else truncated_features
        )
    else:
        truncated_features, stats = truncate_features(second_features, first_features, keep_empty)
        output_features = (
            copy.deepcopy(first_features) + truncated_features
            if combined_output
            else truncated_features
        )

    return {
        "type": "FeatureCollection",
        "features": output_features,
    }, stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Remove overlaps between two GeoJSON inputs by subtracting one layer's "
            "geometry from the other."
        ),
        formatter_class=HelpFormatter,
        add_help=False,
        epilog=(
            "Examples:\n"
            "  Keep protected.geojson intact and trim target.geojson:\n"
            "    python geojson_truncate_overlaps.py protected.geojson target.geojson "
            "-o target_trimmed.geojson\n\n"
            "  Trim the first file instead:\n"
            "    python geojson_truncate_overlaps.py first.geojson second.geojson "
            "--truncate first -o first_trimmed.geojson\n\n"
            "  Write both non-overlapping layers to one file:\n"
            "    python geojson_truncate_overlaps.py protected.geojson target.geojson "
            "--combined-output -o no_overlaps.geojson"
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
        "protected_geojson",
        metavar="PROTECTED_GEOJSON",
        help="First GeoJSON file. Its geometry is kept intact by default.",
    )
    parser.add_argument(
        "target_geojson",
        metavar="TARGET_GEOJSON",
        help="Second GeoJSON file. Its overlapping geometry is trimmed by default.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT_GEOJSON",
        help="Output GeoJSON path. Defaults to stdout.",
    )
    parser.add_argument(
        "--truncate",
        choices=("first", "second"),
        default="second",
        help=(
            "Which input to truncate: 'first' trims PROTECTED_GEOJSON using "
            "TARGET_GEOJSON; 'second' trims TARGET_GEOJSON using PROTECTED_GEOJSON."
        ),
    )
    parser.add_argument(
        "--combined-output",
        action="store_true",
        help="Write the untouched input plus the truncated input into one FeatureCollection.",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Keep fully-erased features with null geometry instead of dropping them.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        metavar="SPACES",
        help="JSON indentation level.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    first_data = load_geojson(args.protected_geojson)
    second_data = load_geojson(args.target_geojson)
    output, stats = build_truncated_collection(
        first_data,
        second_data,
        truncate=args.truncate,
        keep_empty=args.keep_empty,
        combined_output=args.combined_output,
    )

    write_geojson(output, args.output, args.indent)
    print(
        "Processed {processed_features} feature(s); trimmed {trimmed_features}; "
        "removed {removed_features}; empty input geometries {empty_features}; "
        "removed area {removed_area:.12g}.".format(**stats),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
