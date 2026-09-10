# DATE Mapper

![DATE Mapper globe view](images/globe.png)

DATE Mapper is an independent map and globe viewer for the fictional operating
environments used by the [U.S. Army Decisive Action Training Environment (DATE)](https://odin.t2com.army.mil/DATE)
and [Australian Army DATE](https://date.army.gov.au/).

[View the DATE Mapper gallery](gallery.md).

## Local development

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:9099>.

## Run with Gunicorn

On macOS or Ubuntu with Python 3.10 or newer:

```sh
./run_gunicorn.sh
```

The launcher binds to <http://127.0.0.1:9099> with one worker and four threads by default. Settings can be changed with environment variables:

```sh
HOST=0.0.0.0 PORT=9099 GUNICORN_WORKERS=2 GUNICORN_THREADS=4 ./run_gunicorn.sh
```

Other supported settings are `GUNICORN_TIMEOUT` and `GUNICORN_LOG_LEVEL`.

## Run with Docker

The Docker image includes the datasets present under `data` when the image is built. It runs as a non-root user and starts the application through `run_gunicorn.sh` and Gunicorn.

Docker Compose is the simplest option on both macOS and Ubuntu:

```sh
docker compose up --build
```

Open <http://127.0.0.1:9099>. Stop the service with:

```sh
docker compose down
```

Compose stores generated layer caches and saved markers in the `date-mapper-cache` and `date-mapper-storage` named volumes. To discard those volumes as well, run `docker compose down --volumes`.

To build and run without Compose:

```sh
docker build -t date-mapper .
docker run --rm \
  -p 9099:9099 \
  -v date-mapper-cache:/app/cache \
  -v date-mapper-storage:/app/storage \
  date-mapper
```

The normal Compose and `docker build` commands build for the host architecture, so
they work natively on both Intel/AMD x64 (`linux/amd64`) and ARM64
(`linux/arm64`) systems. Explicit Buildx targets are also provided:

```sh
# Build one architecture and load it into the local Docker image store.
docker buildx bake amd64 --load
docker buildx bake arm64 --load

# Build and load both architecture-specific tags.
docker buildx bake all --load
```

These produce `date-mapper:local-amd64` and `date-mapper:local-arm64`. Building a
non-native architecture requires a Buildx builder with CPU emulation; Docker
Desktop provides this by default.

To publish one image tag that automatically selects ARM64 or AMD64, set a
registry-qualified image name and push the multi-platform target:

```sh
IMAGE_NAME=registry.example.com/date-mapper \
IMAGE_TAG=latest \
docker buildx bake multiarch --push
```

The Docker build imports all native geospatial modules before completing, so an
image cannot be produced with incompatible Fiona, GDAL, PROJ, GEOS, or Pyogrio
binaries.

Gunicorn settings can be overridden with `-e`, for example `-e GUNICORN_WORKERS=2 -e GUNICORN_THREADS=4`.

Rebuild the image after changing files in `data`, or mount the working dataset read-only while developing:

```sh
docker run --rm \
  -p 9099:9099 \
  -v "$PWD/data:/app/data:ro" \
  -v date-mapper-cache:/app/cache \
  -v date-mapper-storage:/app/storage \
  date-mapper
```

## Sources

- The [U.S. Army ODIN DATE portal](https://odin.t2com.army.mil/DATE) provides the foundational Decisive Action Training Environment description and reference material.
- The [Australian Army DATE website](https://date.army.gov.au/) provides Australian DATE operating environments, doctrine, and supporting resources.
- Dataset-specific provenance and licensing details are recorded in [Sources.md](Sources.md) and in the relevant sections below.

## Offline use

- The browser map library is vendored locally in `static/vendor/leaflet`.
- The map viewer does not request internet map tiles.
- Python packages must already be installed in `.venv` before disconnecting from the internet.
- The layer catalog is built from readable source files at startup. Missing or inaccessible folders, invalid placeholders, and empty shapefiles are not shown. Use `Reload layers` to rescan after adding data.

## Country data

- `data/High-Resolution/Country` is the source of truth for both the map and globe views.
- The globe simplifies those files while building `cache/_globe_countries_v6.geojson`; normal globe loads use that compact cache.
- After simplification, DATE country geometry is subtracted from WORLD country geometry so DATE countries retain priority wherever they overlap.
- Changes, additions, deletions, and renames in the high-resolution country folder invalidate the globe cache automatically.
- `data/Low-Resolution` remains available for legacy supporting data, but country boundaries and capital-city points now use `data/High-Resolution` as their source of truth.

## Capital city data

- Real-world national-capital locations come from [Natural Earth 1:10m Populated Places version 5.1.2](https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-populated-places/), filtered to records where `ADM0CAP = 1`. Natural Earth includes all admin-0 capitals and is [public-domain data](https://www.naturalearthdata.com/about/terms-of-use/).
- The filtered, application-ready source is stored at `data/High-Resolution/Place/Capital Cities/World_Capital_Cities.geojson`. WORLD capital points covered by any `DATASET=DATE` country polygon, including points on its boundary, are removed. The current layer contains 180 capital points after 20 exclusions.
- DATE capital locations remain in `data/High-Resolution/Place/Capital City` and are presented as the separate **DATE Capital Cities** layer group. They are not replaced or modified by the Natural Earth import.
- Refresh the world-capital layer reproducibly with `python Utilities/update_world_capitals.py`. The importer is pinned to Natural Earth 5.1.2, verifies the downloaded source checksum, and reapplies the current DATE-country exclusion before writing data.

## Undersea cables

- In the map viewer, enable the cable layer and click a country to highlight cable systems that terminate there. Other visible cable systems are subdued until another country is selected.
- Terminal countries are derived from route endpoints with a 50 km near-shore allowance and stored in the generated layer cache. Country boundary changes invalidate these associations automatically.

## Utilities

### Global Wind Power Tracker importer

`Utilities/update_global_wind_power_tracker.py` imports wind-farm locations from Global Energy Monitor's [Global Wind Power Tracker](https://globalenergymonitor.org/projects/global-wind-power-tracker/). Consult GEM's [tracker methodology](https://www.gem.wiki/Global_Wind_Power_Tracker_Methodology) for its complete definitions and research process.

The tracker describes wind-farm **phases**, rather than individual turbines or site-boundary polygons. It aims to cover onshore and offshore projects of more than 10 MW worldwide, with some smaller projects supplied in a separate `Below Threshold` worksheet. Records can represent announced, pre-construction, construction, operating, shelved, cancelled, mothballed, or retired phases. Coordinates may be marked `exact` or `approximate`, so proposed projects and approximate records should not be interpreted as surveyed turbine positions. GEM states that the database and its associated project pages are updated annually.

The data is available under the [Creative Commons Attribution 4.0 International licence](https://globalenergymonitor.org/creative-commons-license/). The generated GeoJSON retains the source name, release, licence URL, recommended release-specific citation, and source-workbook SHA-256 digest. Any redistributed copy or adaptation must preserve the required attribution to Global Energy Monitor.

The utility uses GEM's [official download form](https://globalenergymonitor.org/projects/global-wind-power-tracker/#download) and its issued download URL; it does not scrape the interactive map. It converts the workbook's `Data` and `Below Threshold` sheets to deterministic, application-ready UTF-8 GeoJSON at `data/High-Resolution/Infrastructure/Critical Infrastructure/Wind Farms/Global_Wind_Power_Tracker.geojson`. Every imported feature receives `DATASET: WORLD`, and the resulting optional infrastructure layer is hidden by default in the map viewer.

Run the importer interactively after reviewing the licence. GEM requires a name, email address, organization, sector, and an intended-use description of at least 100 characters. The script does not opt in to project emails unless `--email-opt-in` is supplied:

```sh
python Utilities/update_global_wind_power_tracker.py --accept-license
```

For unattended use, provide `GEM_NAME`, `GEM_EMAIL`, `GEM_ORGANIZATION`, `GEM_SECTOR`, `GEM_COUNTRY`, and `GEM_USE_CASE` as environment variables. `GEM_COUNTRY` is optional. Add `--workbook-output data_storage/Global-Wind-Power-Tracker-latest.xlsx` to retain the source workbook.

A workbook downloaded manually from GEM can be converted without submitting the form again:

```sh
python Utilities/update_global_wind_power_tracker.py \
  --source-file /path/to/Global-Wind-Power-Tracker.xlsx
```

All tracker statuses are retained. Use `--exclude-below-threshold` to omit the separate below-10-MW worksheet. Run the utility again when GEM publishes a new release; replacing the output file is detected automatically by the layer catalog and cache.

## Boundary maintenance

- Run `Utilities/prioritize_date_country_boundaries.py` after replacing country files to restore DATE-over-WORLD priority.
- The Somalia coastal corridor beside Nyumba can be repaired reproducibly with `Utilities/transfer_narrow_country_corridor.py data/High-Resolution/Country/somalia.json data/High-Resolution/Country/Democratic_Republic_of_Nyumba.geojson --seed-longitude 42.3 --seed-latitude -0.7 --erosion-km 1.5 --apply`.
- The Malaysian strip surrounding Belesia can be transferred to Belesia with `Utilities/transfer_narrow_country_corridor.py data/High-Resolution/Country/malaysia.json data/High-Resolution/Country/Federated_States_of_Belesia.geojson --seed-longitude 116.8 --seed-latitude 7.0 --erosion-km 2 --apply`. The seed selects the affected Borneo feature without changing Malaysia's other features.
- Enclosed gaps along the Ethiopia-Nyumba border can be assigned to Ethiopia with `Utilities/fill_country_border_gaps.py data/High-Resolution/Country data/High-Resolution/Country/ethiopia.json data/High-Resolution/Country/Democratic_Republic_of_Nyumba.geojson --region 30 0 46 8 --minimum-gap-area-km2 0.01 --apply`.
- Enclosed gaps along the Uganda-Amari border can be assigned to Uganda with `Utilities/fill_country_border_gaps.py data/High-Resolution/Country data/High-Resolution/Country/uganda.json data/High-Resolution/Country/Republic_of_Amari.geojson --region 27 -3 37 6 --minimum-gap-area-km2 0.01 --apply`.
- The enclosed sea between Ziwa, Amari, and Kujenga can be removed from Uganda and Tanzania with `Utilities/create_inland_sea.py data/High-Resolution/Country --target data/High-Resolution/Country/uganda.json data/High-Resolution/Country/tanzania.json --boundary data/High-Resolution/Country/Republic_of_Ziwa.geojson data/High-Resolution/Country/Republic_of_Amari.geojson data/High-Resolution/Country/Republic_of_Kujenga.geojson --region 31.5 -3.2 35 0.7 --seed 32.865691 -1.266829 --apply`.

## Tests

```sh
python -m unittest discover -s tests -v
```
