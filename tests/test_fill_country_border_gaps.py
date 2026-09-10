# Aim: Verify enclosed border gaps are safely assigned to the requested WORLD country.
# Author: Benjamin Turnbull

import json
import tempfile
import unittest
from pathlib import Path

import shapely
from shapely.geometry import Point, box

from Utilities import fill_country_border_gaps as gap_filler


def feature(name, geometry, dataset):
    return {
        "type": "Feature",
        "properties": {"NAME_SHORT": name, "DATASET": dataset},
        "geometry": json.loads(shapely.to_geojson(geometry)),
    }


def write_country(path, item):
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [item]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def read_geometry(path):
    payload = json.loads(path.read_text())
    return shapely.from_geojson(json.dumps(payload["features"][0]["geometry"]))


class FillCountryBorderGapsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target_path = self.root / "target.geojson"
        self.adjacent_path = self.root / "adjacent.geojson"

        write_country(
            self.target_path,
            feature("Target", box(2, 6, 8, 10), "WORLD"),
        )
        write_country(
            self.adjacent_path,
            feature("Adjacent", box(2, 0, 8, 4), "DATE"),
        )
        write_country(
            self.root / "left.geojson",
            feature("Left", box(0, 0, 2, 10), "WORLD"),
        )
        write_country(
            self.root / "right.geojson",
            feature("Right", box(8, 0, 10, 10), "WORLD"),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_dry_run_then_apply_fills_only_the_enclosed_border_gap(self):
        original_target = self.target_path.read_bytes()
        original_adjacent = self.adjacent_path.read_bytes()

        dry_run = gap_filler.fill_country_border_gaps(
            self.root,
            self.target_path,
            self.adjacent_path,
            region_bounds=(0, 0, 10, 10),
        )

        self.assertFalse(dry_run["applied"])
        self.assertEqual(dry_run["filled_gap_count"], 1)
        self.assertGreater(
            dry_run["new_shared_boundary_km"],
            dry_run["old_shared_boundary_km"],
        )
        self.assertEqual(self.target_path.read_bytes(), original_target)
        self.assertEqual(self.adjacent_path.read_bytes(), original_adjacent)

        result = gap_filler.fill_country_border_gaps(
            self.root,
            self.target_path,
            self.adjacent_path,
            region_bounds=(0, 0, 10, 10),
            apply=True,
            backup_dir=self.root / "backup",
        )

        target = read_geometry(self.target_path)
        adjacent = read_geometry(self.adjacent_path)
        self.assertTrue(result["applied"])
        self.assertTrue(target.is_valid)
        self.assertTrue(target.covers(Point(5, 5)))
        self.assertAlmostEqual(target.intersection(adjacent).area, 0)
        self.assertEqual(self.adjacent_path.read_bytes(), original_adjacent)
        self.assertTrue((self.root / "backup" / "target.geojson").exists())


if __name__ == "__main__":
    unittest.main()
