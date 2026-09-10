# Aim: Verify narrow corridors can be identified and transferred between country geometries.
# Author: Benjamin Turnbull

import json
import tempfile
import unittest
from pathlib import Path

import shapely
from shapely.geometry import box

from Utilities import transfer_narrow_country_corridor as transfer


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


def read_geometry(path, feature_index=0):
    payload = json.loads(path.read_text())
    return shapely.from_geojson(
        json.dumps(payload["features"][feature_index]["geometry"])
    )


class TransferNarrowCountryCorridorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.world_path = self.root / "world.geojson"
        self.date_path = self.root / "date.geojson"

        mainland = box(3, 0, 10, 8)
        neck = box(2.7, 3.9, 3, 4.1)
        corridor = box(0, 3, 2.7, 5)
        world = mainland.union(neck).union(corridor)
        date = box(0, 0, 2.7, 3)
        write_country(self.world_path, feature("World", world, "WORLD"))
        write_country(self.date_path, feature("DATE", date, "DATE"))

    def tearDown(self):
        self.temporary.cleanup()

    def test_dry_run_then_apply_transfers_only_the_seeded_corridor(self):
        original_world = self.world_path.read_bytes()
        original_date = self.date_path.read_bytes()

        dry_run = transfer.transfer_narrow_corridor(
            self.world_path,
            self.date_path,
            seed_longitude=1,
            seed_latitude=4,
            erosion_km=15,
        )

        self.assertFalse(dry_run["applied"])
        self.assertLess(
            dry_run["new_shared_boundary_km"],
            dry_run["old_shared_boundary_km"],
        )
        self.assertEqual(self.world_path.read_bytes(), original_world)
        self.assertEqual(self.date_path.read_bytes(), original_date)

        result = transfer.transfer_narrow_corridor(
            self.world_path,
            self.date_path,
            seed_longitude=1,
            seed_latitude=4,
            erosion_km=15,
            apply=True,
            backup_dir=self.root / "backup",
        )

        world = read_geometry(self.world_path)
        date = read_geometry(self.date_path)
        self.assertTrue(result["applied"])
        self.assertTrue(world.is_valid)
        self.assertTrue(date.is_valid)
        self.assertFalse(world.covers(shapely.Point(1, 4)))
        self.assertTrue(date.covers(shapely.Point(1, 4)))
        self.assertAlmostEqual(world.intersection(date).area, 0)
        self.assertTrue((self.root / "backup" / "world.geojson").exists())
        self.assertTrue((self.root / "backup" / "date.geojson").exists())

    def test_seed_selects_one_feature_in_a_multi_feature_world_file(self):
        payload = json.loads(self.world_path.read_text())
        target_feature = payload["features"][0]
        unrelated_feature = feature(
            "Unrelated island",
            box(20, 20, 21, 21),
            "WORLD",
        )
        payload["features"] = [unrelated_feature, target_feature]
        self.world_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

        result = transfer.transfer_narrow_corridor(
            self.world_path,
            self.date_path,
            seed_longitude=1,
            seed_latitude=4,
            erosion_km=15,
            apply=True,
            backup_dir=self.root / "backup",
        )

        output_payload = json.loads(self.world_path.read_text())
        world = read_geometry(self.world_path, feature_index=1)
        date = read_geometry(self.date_path)
        self.assertEqual(result["world_feature_index"], 1)
        self.assertEqual(output_payload["features"][0], unrelated_feature)
        self.assertFalse(world.covers(shapely.Point(1, 4)))
        self.assertTrue(date.covers(shapely.Point(1, 4)))
        self.assertAlmostEqual(world.intersection(date).area, 0)


if __name__ == "__main__":
    unittest.main()
