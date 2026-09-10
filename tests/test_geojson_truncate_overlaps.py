# Aim: Verify overlap subtraction and output properties for the GeoJSON trimming helper.
# Author: Benjamin Turnbull

import unittest

from shapely.geometry import shape

import geojson_truncate_overlaps as trimmer


def square_feature(name, left, bottom, right, top):
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [left, bottom],
                [right, bottom],
                [right, top],
                [left, top],
                [left, bottom],
            ]],
        },
    }


def collection(*features):
    return {
        "type": "FeatureCollection",
        "features": list(features),
    }


def multipolygon_feature(name, polygons):
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                polygon["geometry"]["coordinates"]
                for polygon in polygons
            ],
        },
    }


class GeojsonTruncateOverlapsTests(unittest.TestCase):
    def test_second_geojson_is_truncated_by_default(self):
        protected = collection(square_feature("protected", 0, 0, 2, 2))
        target = collection(square_feature("target", 1, 0, 3, 2))

        output, stats = trimmer.build_truncated_collection(protected, target)

        self.assertEqual(stats["removed_features"], 0)
        self.assertEqual(stats["trimmed_features"], 1)
        self.assertEqual(stats["removed_area"], 2)
        self.assertEqual(len(output["features"]), 1)
        target_geometry = shape(output["features"][0]["geometry"])
        protected_geometry = shape(protected["features"][0]["geometry"])
        self.assertEqual(target_geometry.area, 2)
        self.assertEqual(target_geometry.intersection(protected_geometry).area, 0)

    def test_first_geojson_can_be_truncated(self):
        first = collection(square_feature("first", 0, 0, 2, 2))
        second = collection(square_feature("second", 1, 0, 3, 2))

        output, stats = trimmer.build_truncated_collection(first, second, truncate="first")

        self.assertEqual(stats["removed_features"], 0)
        self.assertEqual(stats["trimmed_features"], 1)
        self.assertEqual(stats["removed_area"], 2)
        self.assertEqual(len(output["features"]), 1)
        first_geometry = shape(output["features"][0]["geometry"])
        second_geometry = shape(second["features"][0]["geometry"])
        self.assertEqual(first_geometry.area, 2)
        self.assertEqual(first_geometry.intersection(second_geometry).area, 0)

    def test_combined_output_contains_untouched_and_truncated_features(self):
        protected = collection(square_feature("protected", 0, 0, 2, 2))
        target = collection(square_feature("target", 1, 0, 3, 2))

        output, stats = trimmer.build_truncated_collection(
            protected,
            target,
            combined_output=True,
        )

        self.assertEqual(stats["removed_features"], 0)
        self.assertEqual(stats["trimmed_features"], 1)
        self.assertEqual(stats["removed_area"], 2)
        self.assertEqual(len(output["features"]), 2)
        protected_geometry = shape(output["features"][0]["geometry"])
        target_geometry = shape(output["features"][1]["geometry"])
        self.assertEqual(protected_geometry.intersection(target_geometry).area, 0)

    def test_fully_overlapped_features_are_dropped(self):
        protected = collection(square_feature("protected", 0, 0, 2, 2))
        target = collection(square_feature("target", 0.5, 0.5, 1.5, 1.5))

        output, stats = trimmer.build_truncated_collection(protected, target)

        self.assertEqual(stats["removed_features"], 1)
        self.assertEqual(stats["trimmed_features"], 0)
        self.assertEqual(stats["removed_area"], 1)
        self.assertEqual(output["features"], [])

    def test_multipolygon_protected_geojson_truncates_target(self):
        protected = collection(multipolygon_feature("protected", [
            square_feature("part-1", 0, 0, 2, 2),
            square_feature("part-2", 10, 10, 12, 12),
        ]))
        target = collection(square_feature("target", 1, 0, 3, 2))

        output, stats = trimmer.build_truncated_collection(protected, target)

        self.assertEqual(stats["removed_features"], 0)
        self.assertEqual(stats["trimmed_features"], 1)
        self.assertEqual(stats["removed_area"], 2)
        target_geometry = shape(output["features"][0]["geometry"])
        protected_geometry = trimmer.geometry_from_geojson(protected["features"][0]["geometry"])
        self.assertEqual(target_geometry.area, 2)
        self.assertEqual(target_geometry.intersection(protected_geometry).area, 0)

    def test_empty_polygon_coordinates_are_dropped(self):
        protected = collection(square_feature("protected", 0, 0, 2, 2))
        target = collection({
            "type": "Feature",
            "properties": {"name": "empty"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [],
            },
        })

        output, stats = trimmer.build_truncated_collection(protected, target)

        self.assertEqual(stats["removed_features"], 1)
        self.assertEqual(stats["empty_features"], 1)
        self.assertEqual(stats["trimmed_features"], 0)
        self.assertEqual(output["features"], [])


if __name__ == "__main__":
    unittest.main()
