# Aim: Verify DATE country geometry takes priority across generated country datasets.
# Author: Benjamin Turnbull

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import shape

from Utilities import prioritize_date_country_boundaries as prioritizer


def square_feature(name, left, bottom, right, top, dataset=None):
    properties = {"name": name}
    if dataset:
        properties["DATASET"] = dataset
    return {
        "type": "Feature",
        "properties": properties,
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
    return {"type": "FeatureCollection", "features": list(features)}


def write_geojson(path, payload, indent=2):
    path.write_text(
        json.dumps(payload, indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class PrioritizeDateCountryBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.high = self.root / "High-Resolution" / "Country"
        self.low = self.root / "Low-Resolution" / "Country"
        self.high.mkdir(parents=True)
        self.low.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_clips_both_directories_and_never_rewrites_date_files(self):
        high_date = self.high / "Date Republic.geojson"
        low_date = self.low / "Date Republic.geojson"
        write_geojson(high_date, collection(
            square_feature("DATE", 0, 0, 2, 2, "DATE"),
        ))
        write_geojson(low_date, collection(
            square_feature("DATE", 0, 0, 2, 2),
        ), indent=4)
        high_date_bytes = high_date.read_bytes()
        low_date_bytes = low_date.read_bytes()

        for directory, dataset in ((self.high, "WORLD"), (self.low, None)):
            write_geojson(directory / "world.json", collection(
                square_feature("World", 1, 0, 3, 2, dataset),
            ), indent=4)

        result = prioritizer.prioritize_date_boundaries(
            self.high,
            [self.high, self.low],
            apply=True,
            backup_dir=self.root / "backup",
            compact=True,
        )

        self.assertTrue(result["applied"])
        self.assertEqual([item["changed_files"] for item in result["directories"]], [1, 1])
        self.assertEqual(high_date.read_bytes(), high_date_bytes)
        self.assertEqual(low_date.read_bytes(), low_date_bytes)
        self.assertNotIn(b'\n  "type"', (self.high / "world.json").read_bytes())

        for directory in (self.high, self.low):
            world_payload = json.loads((directory / "world.json").read_text())
            world_geometry = shape(world_payload["features"][0]["geometry"])
            date_geometry = shape(collection(
                square_feature("DATE", 0, 0, 2, 2),
            )["features"][0]["geometry"])
            self.assertEqual(world_geometry.area, 2)
            self.assertEqual(world_geometry.intersection(date_geometry).area, 0)
            self.assertEqual(
                world_payload["features"][0]["properties"]["name"],
                "World",
            )

    def test_dry_run_does_not_change_world_file(self):
        write_geojson(self.high / "date.geojson", collection(
            square_feature("DATE", 0, 0, 2, 2, "DATE"),
        ))
        world_path = self.high / "world.geojson"
        write_geojson(world_path, collection(
            square_feature("World", 1, 0, 3, 2, "WORLD"),
        ))
        original = world_path.read_bytes()

        result = prioritizer.prioritize_date_boundaries(
            self.high,
            [self.high],
        )

        self.assertFalse(result["applied"])
        self.assertEqual(result["directories"][0]["changed_files"], 1)
        self.assertEqual(world_path.read_bytes(), original)

    def test_removes_only_fully_covered_features(self):
        write_geojson(self.high / "date.geojson", collection(
            square_feature("DATE", 0, 0, 2, 2, "DATE"),
        ))
        world_path = self.high / "world.geojson"
        write_geojson(world_path, collection(
            square_feature("Covered", 0.5, 0.5, 1.5, 1.5, "WORLD"),
            square_feature("Unaffected", 10, 10, 11, 11, "WORLD"),
        ))

        result = prioritizer.prioritize_date_boundaries(
            self.high,
            [self.high],
            apply=True,
            backup_dir=self.root / "backup",
        )

        payload = json.loads(world_path.read_text())
        self.assertEqual(result["directories"][0]["removed_features"], 1)
        self.assertEqual(len(payload["features"]), 1)
        self.assertEqual(payload["features"][0]["properties"]["name"], "Unaffected")

    def test_discards_only_small_or_narrow_residuals_created_by_a_date_cut(self):
        write_geojson(self.high / "date.geojson", collection(
            square_feature("DATE", 0.02, -1, 1, 2, "DATE"),
            square_feature("Long DATE cut", 3, -1, 4, 11, "DATE"),
        ))
        world_path = self.high / "world.geojson"
        write_geojson(world_path, collection(
            square_feature("Cut world", 0, 0, 2, 1, "WORLD"),
            square_feature("Narrow cut world", 3, 0, 4.01, 10, "WORLD"),
            square_feature("Untouched island", 10, 0, 10.01, 0.01, "WORLD"),
        ))

        result = prioritizer.prioritize_date_boundaries(
            self.high,
            [self.high],
            apply=True,
            backup_dir=self.root / "backup",
        )

        payload = json.loads(world_path.read_text())
        cut_world = shape(payload["features"][0]["geometry"])
        untouched_island = shape(payload["features"][1]["geometry"])
        stats = result["directories"][0]

        self.assertAlmostEqual(cut_world.bounds[0], 1.0)
        self.assertEqual(stats["removed_residual_parts"], 2)
        self.assertGreater(stats["removed_residual_area_km2"], 0)
        self.assertEqual(stats["removed_features"], 1)
        self.assertEqual(
            payload["features"][1]["properties"]["name"],
            "Untouched island",
        )
        self.assertAlmostEqual(untouched_island.area, 0.0001)

    def test_dateline_polygon_is_not_treated_as_a_global_overlap(self):
        write_geojson(self.high / "date.geojson", collection(
            square_feature("DATE", -1, -1, 1, 1, "DATE"),
        ))
        dateline_path = self.high / "dateline.geojson"
        dateline_feature = {
            "type": "Feature",
            "properties": {"name": "Dateline", "DATASET": "WORLD"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [179, -1],
                    [-179, -1],
                    [-179, 1],
                    [179, 1],
                    [179, -1],
                ]],
            },
        }
        write_geojson(dateline_path, collection(dateline_feature))
        original = dateline_path.read_bytes()

        result = prioritizer.prioritize_date_boundaries(
            self.high,
            [self.high],
            apply=True,
            backup_dir=self.root / "backup",
        )

        self.assertEqual(result["directories"][0]["changed_files"], 0)
        self.assertEqual(dateline_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
