# Aim: Verify world-capital filtering, normalization, and GeoJSON output generation.
# Author: Benjamin Turnbull

import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Polygon

from Utilities.update_world_capitals import (
    NATURAL_EARTH_VERSION,
    build_world_capitals,
    write_geojson,
)


class UpdateWorldCapitalsTests(unittest.TestCase):
    def test_filters_and_normalizes_admin_zero_capitals(self):
        source = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "ADM0CAP": 1,
                        "NAME": "Kobenhavn",
                        "NAME_EN": "Copenhagen",
                        "ADM0NAME": "Denmark",
                        "SOV0NAME": "Denmark",
                        "ADM0_A3": "DNK",
                        "ISO_A2": "DK",
                        "FEATURECLA": "Admin-0 capital",
                        "POP_MAX": 1085000,
                        "WIKIDATAID": "Q1748",
                        "NE_ID": 1159151003,
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [12.5683, 55.6761],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "ADM0CAP": 0,
                        "NAME": "Not a capital",
                        "ADM0NAME": "Denmark",
                        "NE_ID": 2,
                    },
                    "geometry": {"type": "Point", "coordinates": [10, 56]},
                },
            ],
        }

        result = build_world_capitals(source)

        self.assertEqual(result["source"]["version"], NATURAL_EARTH_VERSION)
        self.assertEqual(result["source"]["filter"], "ADM0CAP = 1")
        self.assertEqual(len(result["features"]), 1)
        capital = result["features"][0]
        self.assertEqual(capital["id"], "natural-earth-1159151003")
        self.assertEqual(capital["properties"]["name"], "Copenhagen")
        self.assertEqual(capital["properties"]["country"], "Denmark")
        self.assertEqual(capital["properties"]["DATASET"], "WORLD")
        self.assertNotIn("capital_type", capital["properties"])
        self.assertEqual(capital["geometry"]["coordinates"], [12.5683, 55.6761])

    def test_excludes_capitals_covered_by_date_country_geometry(self):
        def capital_feature(name, identifier, coordinates):
            return {
                "type": "Feature",
                "properties": {
                    "ADM0CAP": 1,
                    "NAME_EN": name,
                    "ADM0NAME": "Exampleland",
                    "NE_ID": identifier,
                },
                "geometry": {"type": "Point", "coordinates": coordinates},
            }

        source = {
            "type": "FeatureCollection",
            "features": [
                capital_feature("Inside", 1, [5, 5]),
                capital_feature("On boundary", 2, [10, 5]),
                capital_feature("Outside", 3, [11, 5]),
            ],
        }
        date_country = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])

        result = build_world_capitals(source, [date_country])

        self.assertEqual(
            [feature["properties"]["name"] for feature in result["features"]],
            ["Outside"],
        )
        self.assertEqual(result["date_country_exclusion"]["excluded_count"], 2)
        self.assertEqual(
            [item["name"] for item in result["date_country_exclusion"]["excluded_capitals"]],
            ["Inside", "On boundary"],
        )

    def test_writes_valid_utf8_geojson(self):
        payload = {
            "type": "FeatureCollection",
            "name": "World Capital Cities",
            "features": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "capitals.geojson"

            write_geojson(payload, output)

            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                payload,
            )


if __name__ == "__main__":
    unittest.main()
