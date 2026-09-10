# Aim: Verify inland-sea selection, geometry trimming, validation, and file updates.
# Author: Benjamin Turnbull

import json
import tempfile
import unittest
from pathlib import Path

import shapely
from shapely.geometry import Point, box

from Utilities import create_inland_sea as sea_creator


def feature(name, geometry, dataset):
    return {
        "type": "Feature",
        "properties": {"NAME_SHORT": name, "DATASET": dataset},
        "geometry": json.loads(shapely.to_geojson(geometry)),
    }


def write_country(path, features):
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def read_geometry(path):
    payload = json.loads(path.read_text())
    geometries = [
        shapely.from_geojson(json.dumps(item["geometry"]))
        for item in payload["features"]
    ]
    combined = geometries[0]
    for geometry in geometries[1:]:
        combined = combined.union(geometry)
    return combined


class CreateInlandSeaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target_one = self.root / "target-one.geojson"
        self.target_two = self.root / "target-two.geojson"
        self.boundaries = [
            self.root / "north.geojson",
            self.root / "west.geojson",
            self.root / "south-east.geojson",
        ]

        write_country(
            self.target_one,
            [feature(
                "Target One",
                box(2, 2, 5, 8).union(box(11, 0, 12, 1)),
                "WORLD",
            )],
        )
        write_country(
            self.target_two,
            [
                feature("Target Two", box(5, 2, 8, 8), "WORLD"),
                feature("Target Two", box(13, 0, 14, 1), "WORLD"),
            ],
        )
        write_country(
            self.boundaries[0],
            [feature("North", box(0, 8, 10, 10), "DATE")],
        )
        write_country(
            self.boundaries[1],
            [feature("West", box(0, 0, 2, 8), "DATE")],
        )
        write_country(
            self.boundaries[2],
            [feature(
                "South East",
                box(2, 0, 10, 2).union(box(8, 2, 10, 8)),
                "DATE",
            )],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_utility(self, **kwargs):
        return sea_creator.create_inland_sea(
            self.root,
            [self.target_one, self.target_two],
            self.boundaries,
            region_bounds=(0, 0, 10, 10),
            seed_longitude=5,
            seed_latitude=5,
            **kwargs,
        )

    def test_dry_run_then_apply_removes_basin_from_all_world_targets(self):
        original_targets = [
            self.target_one.read_bytes(),
            self.target_two.read_bytes(),
        ]
        original_boundaries = [path.read_bytes() for path in self.boundaries]

        dry_run = self.run_utility()

        self.assertFalse(dry_run["applied"])
        self.assertEqual(len(dry_run["target_countries"]), 2)
        self.assertEqual(dry_run["other_country_owners"], [])
        self.assertEqual(self.target_one.read_bytes(), original_targets[0])
        self.assertEqual(self.target_two.read_bytes(), original_targets[1])

        result = self.run_utility(
            apply=True,
            backup_dir=self.root / "backup",
        )

        self.assertTrue(result["applied"])
        for path in (self.target_one, self.target_two):
            geometry = read_geometry(path)
            self.assertTrue(geometry.is_valid)
            self.assertFalse(geometry.covers(Point(5, 5)))
            self.assertTrue((self.root / "backup" / path.name).exists())
        self.assertTrue(read_geometry(self.target_one).covers(Point(11.5, 0.5)))
        self.assertTrue(read_geometry(self.target_two).covers(Point(13.5, 0.5)))
        self.assertEqual(
            [path.read_bytes() for path in self.boundaries],
            original_boundaries,
        )

    def test_rejects_a_seed_region_that_is_not_enclosed(self):
        self.boundaries[0].unlink()
        write_country(
            self.boundaries[0],
            [feature("North", box(0, 9, 10, 10), "DATE")],
        )

        with self.assertRaisesRegex(ValueError, "not enclosed"):
            self.run_utility()


if __name__ == "__main__":
    unittest.main()
