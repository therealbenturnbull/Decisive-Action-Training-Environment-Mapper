# Aim: Split a GeoJSON FeatureCollection into one file per named country.
# Author: Benjamin Turnbull

import json
import re
from pathlib import Path

#input_file = Path("DATE_Indo-Pacific_Boundaries.geojson")
#input_file = Path("DATE_Indo-Pacific_Capital_Cities.geojson")
#input_file = Path("DATE_Africa_Boundaries.geojson")
#input_file = Path("DATE_Africa_Capital_Cities.geojson")
#input_file = Path("DATE_Eurasia_Boundaries.geojson")
#input_file = Path("DATE_Eurasia_Capital_Cities.geojson")
#input_file = Path("")
input_file = Path("/Users/bpt/Code/DATE Mapper/data_storage/geojson-world-master/countries.geojson")


#output_dir = Path("DATE_Eurasia")
output_dir = Path("tmp_countries")
output_dir.mkdir(exist_ok=True)
file_prefix = ""
#file_prefix = "Capital_City_"




def safe_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_")

print(input_file)
with open(input_file, "r", encoding="utf-8") as f:
    data = json.load(f)

features_by_country = {}

for feature in data["features"]:
    props = feature.get("properties", {})

    print(props)

    # Change this if your country field has a different name
    country = props.get("NAME_FULL") or props.get("name") or props.get("country")

    if not country:
        country = "unknown"

    features_by_country.setdefault(country, []).append(feature)
    print(features_by_country)
#    exit(0)

for country, features in features_by_country.items():
    output = {
        "type": "FeatureCollection",
        "features": features
    }

    filename = output_dir / f"{file_prefix}{safe_filename(country)}.geojson"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

print(f"Created {len(features_by_country)} files in {output_dir}")
