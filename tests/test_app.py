# Aim: Verify DATE Mapper APIs, catalog discovery, caching, and geospatial response behavior.
# Author: Benjamin Turnbull

import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import app
from shapely.geometry import Point, Polygon, shape


class DateMapperAppTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.tempdir.name)
        self.cache_dir = self.storage_dir / "cache"
        self.data_dir = self.storage_dir / "data"
        self.cache_dir.mkdir()
        self.data_dir.mkdir()

        self.markers_file = self.storage_dir / "user_markers.geojson"
        self.patches = [
            patch.object(app, "DATA_DIR", self.data_dir),
            patch.object(app, "STORAGE_DIR", self.storage_dir),
            patch.object(app, "CACHE_DIR", self.cache_dir),
            patch.object(app, "MARKERS_FILE", self.markers_file),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tempdir.cleanup()

    def test_layers_catalog_includes_user_markers(self):
        res = self.client.get("/api/layers")
        self.assertEqual(res.status_code, 200)
        layer_ids = {item["id"] for item in res.get_json()}
        self.assertIn(app.USER_MARKERS_LAYER_ID, layer_ids)

    def test_layers_catalog_excludes_configured_sources_that_are_not_present(self):
        response = self.client.get("/api/layers")

        self.assertEqual(response.status_code, 200)
        layers = response.get_json()
        self.assertEqual(
            {item["id"] for item in layers},
            {app.USER_MARKERS_LAYER_ID},
        )
        self.assertTrue(all(item["available"] for item in layers))

    def test_can_create_user_marker_and_read_layer(self):
        res = self.client.post(
            "/api/markers",
            json={
                "name": "Sydney",
                "description": "Manual marker",
                "lat": -33.8688,
                "lng": 151.2093,
            },
        )
        self.assertEqual(res.status_code, 201)
        feature = res.get_json()
        self.assertEqual(feature["properties"]["name"], "Sydney")

        layer_res = self.client.get(f"/api/layer/{app.USER_MARKERS_LAYER_ID}")
        self.assertEqual(layer_res.status_code, 200)
        collection = layer_res.get_json()
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 1)
        self.assertEqual(collection["features"][0]["properties"]["name"], "Sydney")

        stored = json.loads(self.markers_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["features"][0]["geometry"]["coordinates"], [151.2093, -33.8688])

    def test_can_create_user_marker_with_png_icon(self):
        png_payload = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        )
        res = self.client.post(
            "/api/markers",
            data={
                "name": "Sydney",
                "description": "Manual marker",
                "lat": "-33.8688",
                "lng": "151.2093",
                "icon": (BytesIO(png_payload), "marker.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 201)
        feature = res.get_json()
        properties = feature["properties"]
        self.assertTrue(properties["icon_filename"].endswith(".png"))
        self.assertTrue(properties["icon_url"].startswith("/storage/marker-icons/"))

        icon_res = self.client.get(properties["icon_url"])
        self.assertEqual(icon_res.status_code, 200)
        self.assertEqual(icon_res.data, png_payload)
        icon_res.close()

        layer_res = self.client.get(f"/api/layer/{app.USER_MARKERS_LAYER_ID}")
        self.assertEqual(layer_res.status_code, 200)
        layer_properties = layer_res.get_json()["features"][0]["properties"]
        self.assertEqual(layer_properties["icon_url"], properties["icon_url"])

    def test_user_marker_icon_must_be_png(self):
        res = self.client.post(
            "/api/markers",
            data={
                "lat": "-33.8688",
                "lng": "151.2093",
                "icon": (BytesIO(b"not a png"), "marker.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("PNG", res.get_json()["error"])

    def test_globe_markers_api_reads_user_markers_with_icons(self):
        res = self.client.post(
            "/api/markers",
            data={
                "name": "Sydney",
                "description": "Globe marker",
                "lat": "-33.8688",
                "lng": "151.2093",
                "icon": (BytesIO(b"\x89PNG\r\n\x1a\nsmall"), "marker.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 201)
        marker_feature = res.get_json()

        globe_res = self.client.get("/api/globe/markers")
        self.assertEqual(globe_res.status_code, 200)
        payload = globe_res.get_json()

        self.assertEqual(payload["type"], "GlobeMarkerCollection")
        self.assertEqual(payload["count"], 1)
        marker = payload["markers"][0]
        self.assertEqual(marker["id"], marker_feature["properties"]["marker_id"])
        self.assertEqual(marker["name"], "Sydney")
        self.assertEqual(marker["description"], "Globe marker")
        self.assertEqual(marker["longitude"], 151.2093)
        self.assertEqual(marker["latitude"], -33.8688)
        self.assertEqual(marker["icon_url"], marker_feature["properties"]["icon_url"])

    def test_invalid_marker_coordinates_are_rejected(self):
        res = self.client.post("/api/markers", json={"lat": 200, "lng": 151.2093})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Latitude", res.get_json()["error"])

    def test_obsolete_admin_level_zero_catalog_layers_are_removed(self):
        obsolete_ids = {
            "gadm41_AUS_0",
            "gadm41_GBR_0",
            "gadm41_NZL_0",
            "gadm41_ATA_0",
        }
        layers = self.client.get("/api/layers").get_json()

        self.assertTrue(obsolete_ids.isdisjoint({item["id"] for item in layers}))
        for layer_id in obsolete_ids:
            self.assertEqual(self.client.get(f"/api/layer/{layer_id}").status_code, 404)

    def test_settings_page_loads(self):
        response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Colour scheme", response.data)
        self.assertIn(b"colourSchemeOptions", response.data)
        self.assertIn(b"Vertically flip Antarctica", response.data)
        self.assertIn(b"/static/colour-schemes.js", response.data)

    def test_map_and_globe_use_shared_colour_settings(self):
        map_response = self.client.get("/")
        globe_response = self.client.get("/globe")

        for response in (map_response, globe_response):
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'href="/settings"', response.data)
            self.assertIn(b"/static/app-shell.css", response.data)
            self.assertIn(b'class="view-switch"', response.data)
            self.assertIn(b"/static/settings-store.js", response.data)
            self.assertIn(b"/static/colour-schemes.js", response.data)

        self.assertIn(b'aria-current="page">Map', map_response.data)
        self.assertIn(b'aria-current="page">Globe', globe_response.data)
        self.assertIn(b'tabindex="0"', globe_response.data)
        self.assertNotIn(b"globeColourMode", globe_response.data)
        self.assertNotIn(b"colour-panel", globe_response.data)

    def test_globe_has_separate_world_and_date_capital_menus(self):
        globe_response = self.client.get("/globe")
        globe_script = self.client.get("/static/globe.js")
        self.addCleanup(globe_script.close)

        self.assertEqual(globe_response.status_code, 200)
        self.assertEqual(globe_script.status_code, 200)
        self.assertIn(b">Capital Cities</h2>", globe_response.data)
        self.assertIn(b">DATE Capital Cities</h2>", globe_response.data)
        self.assertIn(b'id="capitalList"', globe_response.data)
        self.assertIn(b'id="dateCapitalList"', globe_response.data)
        self.assertIn(b'const WORLD_CAPITAL_DATASET = "WORLD"', globe_script.data)
        self.assertIn(b'const DATE_CAPITAL_DATASET = "DATE"', globe_script.data)
        self.assertIn(b"renderWorldCapitalOptions", globe_script.data)
        self.assertIn(b"renderDateCapitalOptions", globe_script.data)
        self.assertIn(b"selectAllOptionMarkup", globe_script.data)
        self.assertIn(b'data-select-all=', globe_script.data)
        self.assertIn(b'highlight-name">All', globe_script.data)
        self.assertIn(b"setItemsSelected", globe_script.data)

    def test_map_has_no_basemap_control_and_marker_tools_start_collapsed(self):
        map_response = self.client.get("/")
        main_script = self.client.get("/static/main.js")
        self.addCleanup(main_script.close)

        self.assertEqual(map_response.status_code, 200)
        self.assertEqual(main_script.status_code, 200)
        self.assertIn(
            b'<details id="markerTools" class="marker-tools">',
            map_response.data,
        )
        self.assertNotIn(b"Offline background", main_script.data)
        self.assertNotIn(b"L.control.layers", main_script.data)
        self.assertNotIn(b"L.tileLayer", main_script.data)
        self.assertIn(b"attributionControl: false", main_script.data)

    def test_globe_exposes_button_wheel_keyboard_and_pinch_zoom(self):
        globe_response = self.client.get("/globe")
        globe_script = self.client.get("/static/globe.js")
        self.addCleanup(globe_script.close)

        self.assertEqual(globe_response.status_code, 200)
        self.assertEqual(globe_script.status_code, 200)
        self.assertIn(b'id="zoomInButton"', globe_response.data)
        self.assertIn(b'id="zoomOutButton"', globe_response.data)
        self.assertIn(b'canvas.addEventListener("wheel"', globe_script.data)
        self.assertIn(b"state.pinching", globe_script.data)
        self.assertIn(b'event.key === "+"', globe_script.data)
        self.assertIn(b"GLOBE_MIN_ZOOM", globe_script.data)
        self.assertIn(b"GLOBE_MAX_ZOOM", globe_script.data)
        self.assertIn(b"globeBaseSize", globe_script.data)
        self.assertIn(b"canvas.style.width = `${availableWidth}px`", globe_script.data)
        self.assertIn(b"MAX_CANVAS_PIXELS", globe_script.data)

    def test_shared_colour_catalog_contains_every_supported_scheme(self):
        response = self.client.get("/static/colour-schemes.js")
        self.addCleanup(response.close)

        self.assertEqual(response.status_code, 200)
        scheme_ids = (
            b'basic',
            b'modern',
            b'neon-night',
            b'mirrorwave',
            b'wireframe',
            b'white-fill',
            b'inverted',
            b'natural',
            b'nordic',
            b'pacific',
            b'graphite',
            b'solar',
            b'aurora',
            b'transit',
        )
        self.assertEqual(response.data.count(b'\n      id: "'), len(scheme_ids))
        for scheme_id in scheme_ids:
            self.assertIn(b'id: "' + scheme_id + b'"', response.data)

        settings_store = self.client.get("/static/settings-store.js")
        self.addCleanup(settings_store.close)
        self.assertEqual(settings_store.status_code, 200)
        for scheme_id in scheme_ids:
            self.assertIn(b'"' + scheme_id + b'"', settings_store.data)

    def test_mirrorwave_uses_metallic_gradients_on_both_views(self):
        map_response = self.client.get("/")
        colour_catalog = self.client.get("/static/colour-schemes.js")
        map_script = self.client.get("/static/main.js")
        globe_script = self.client.get("/static/globe.js")
        self.addCleanup(colour_catalog.close)
        self.addCleanup(map_script.close)
        self.addCleanup(globe_script.close)

        self.assertIn(b'id="mirrorwave-world-metal"', map_response.data)
        self.assertIn(b'id="mirrorwave-date-metal"', map_response.data)
        self.assertIn(b"globeGradientStops", colour_catalog.data)
        self.assertIn(b"countryGradientStops", colour_catalog.data)
        self.assertIn(b"url(#mirrorwave-world-metal)", map_script.data)
        self.assertIn(b"createMetallicGradient", globe_script.data)

    def test_docker_configuration_runs_gunicorn_as_non_root(self):
        project_dir = Path(__file__).resolve().parents[1]
        dockerfile = (project_dir / "Dockerfile").read_text(encoding="utf-8")
        bake = (project_dir / "docker-bake.hcl").read_text(encoding="utf-8")
        compose = (project_dir / "compose.yaml").read_text(encoding="utf-8")
        readme = (project_dir / "Readme.md").read_text(encoding="utf-8")

        self.assertIn("USER appuser", dockerfile)
        self.assertIn('CMD ["./run_gunicorn.sh"]', dockerfile)
        self.assertIn("HOST=0.0.0.0", dockerfile)
        self.assertIn('VOLUME ["/app/cache", "/app/storage"]', dockerfile)
        self.assertIn("import fiona, geopandas, pyogrio, pyproj, shapely", dockerfile)
        self.assertIn('platforms = ["linux/amd64"]', bake)
        self.assertIn('platforms = ["linux/arm64"]', bake)
        self.assertIn('platforms = ["linux/amd64", "linux/arm64"]', bake)
        self.assertIn("date-mapper-cache:/app/cache", compose)
        self.assertIn("date-mapper-storage:/app/storage", compose)
        self.assertIn("docker compose up --build", readme)
        self.assertIn("docker buildx bake multiarch --push", readme)

    def test_shapefile_loader_retries_legacy_text_encoding(self):
        source_file = self.data_dir / "legacy_layer.shp"
        source_file.write_bytes(b"")
        calls = []

        class FakeColumn(list):
            class dtype:
                kind = "O"

        class FakeGeoDataFrame:
            crs = "EPSG:4326"

            def __init__(self, label):
                self.label = label
                self.columns = ["label"]

            def __getitem__(self, column_name):
                if column_name != "label":
                    raise KeyError(column_name)
                return FakeColumn([self.label])

            def to_crs(self, epsg):
                self.epsg = epsg
                return self

            def to_json(self):
                return json.dumps({
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [151.2093, -33.8688]},
                        "properties": {"label": self.label},
                    }],
                })

        def read_file(path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return FakeGeoDataFrame(b"Bearing 45\xb0")
            return FakeGeoDataFrame("Bearing 45\u00b0")

        with patch.object(app.gpd, "read_file", side_effect=read_file):
            payload = app.build_geojson_for_shp("legacy_layer")

        self.assertEqual(calls, [{}, {"encoding": "cp1252"}])
        self.assertEqual(payload["features"][0]["properties"]["label"], "Bearing 45\u00b0")

    def test_non_utf8_shapefile_cache_is_rebuilt_from_source(self):
        layer_id = "legacy_layer"
        source_file = self.data_dir / f"{layer_id}.shp"
        source_file.write_bytes(b"")

        cached_file = app.cache_path(layer_id)
        cached_file.write_bytes(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [{"properties": {"label": "Bearing 45\u00b0"}}],
                },
                ensure_ascii=False,
            ).encode("cp1252")
        )

        rebuilt_payload = {
            "type": "FeatureCollection",
            "features": [{"properties": {"label": "Bearing 45\u00b0"}}],
        }
        with patch.object(app, "build_geojson_for_shp", return_value=rebuilt_payload) as builder:
            payload = app.ensure_cached(layer_id)

        builder.assert_called_once_with(layer_id)
        self.assertEqual(payload, rebuilt_payload)
        self.assertEqual(
            json.loads(cached_file.read_text(encoding="utf-8")),
            rebuilt_payload,
        )

    def test_legacy_encoded_geojson_source_loads(self):
        source_dir = self.data_dir / app.MAP_DATA_SUBDIR / "Country"
        source_dir.mkdir(parents=True)
        source_file = source_dir / "legacy_country.geojson"
        source_file.write_bytes(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [151, -33]},
                        "properties": {"label": "Bearing 45\u00b0"},
                    }],
                },
                ensure_ascii=False,
            ).encode("cp1252")
        )

        layer_id = app.discover_geojson_files()[0]["id"]
        payload = app.build_geojson_for_geojson(layer_id)

        self.assertEqual(
            payload["features"][0]["properties"]["label"],
            "Bearing 45\u00b0",
        )

    def test_geojson_discovery_ignores_macos_metadata_files(self):
        source_dir = self.data_dir / app.MAP_DATA_SUBDIR / "Country"
        source_dir.mkdir(parents=True)
        (source_dir / "China_new_x3.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": []}),
            encoding="utf-8",
        )
        (source_dir / "._China_new_x3.geojson").write_bytes(
            b"\x00\x05\x16\x07\x00\x02\x00\x00AppleDouble metadata"
        )

        discovered = app.discover_geojson_paths(app.map_data_dir())

        self.assertEqual(
            [path.name for path in discovered],
            ["China_new_x3.geojson"],
        )

    def test_nested_cache_geojson_is_auto_discovered(self):
        source_dir = self.cache_dir / "DATE_Test"
        source_dir.mkdir()
        source_file = source_dir / "Test_Capital.geojson"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [151.2093, -33.8688]},
                    "properties": {"name": "Sydney"},
                }],
            }),
            encoding="utf-8",
        )

        layers_res = self.client.get("/api/layers")
        self.assertEqual(layers_res.status_code, 200)
        cache_layers = [
            item for item in layers_res.get_json()
            if item["id"].startswith("cache_geojson__")
        ]
        self.assertEqual(len(cache_layers), 1)
        self.assertEqual(cache_layers[0]["title"], "DATE Test — Test Capital (Cache GeoJSON)")

        layer_res = self.client.get(f"/api/layer/{cache_layers[0]['id']}")
        self.assertEqual(layer_res.status_code, 200)
        payload = layer_res.get_json()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(payload["features"][0]["properties"]["name"], "Sydney")

    def test_nested_cache_geojson_closes_polygon_rings(self):
        source_dir = self.cache_dir / "World"
        source_dir.mkdir()
        source_file = source_dir / "south_africa.json"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[18, -34], [19, -34], [19, -33], [18, -33]]],
                    },
                    "properties": {"name": "South Africa"},
                }],
            }),
            encoding="utf-8",
        )

        layer_id = app.discover_cache_geojson_files()[0]["id"]
        layer_res = self.client.get(f"/api/layer/{layer_id}")
        self.assertEqual(layer_res.status_code, 200)
        ring = layer_res.get_json()["features"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])

    def test_globe_country_api_reads_high_resolution_country_files(self):
        country_dir = self.data_dir / "High-Resolution" / "Country"
        custom_dir = self.data_dir / "High-Resolution" / "Region"
        stale_dir = self.data_dir / "Low-Resolution" / "Country"
        country_dir.mkdir(parents=True)
        custom_dir.mkdir(parents=True)
        stale_dir.mkdir(parents=True)
        country_file = country_dir / "test_country.json"
        custom_file = custom_dir / "Custom_Republic.geojson"
        stale_file = stale_dir / "Stale_Country.geojson"
        country_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
                    },
                    "properties": {"name": "Test Country", "DATASET": "DATE"},
                }],
            }),
            encoding="utf-8",
        )
        custom_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[20, 0], [21, 0], [21, 1], [20, 1], [20, 0]]],
                    },
                    "properties": {"name": "Custom Republic"},
                }],
            }),
            encoding="utf-8",
        )
        stale_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[30, 0], [31, 0], [31, 1], [30, 1], [30, 0]]],
                    },
                    "properties": {"name": "Stale Country"},
                }],
            }),
            encoding="utf-8",
        )

        res = self.client.get("/api/globe/countries")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        names = {country["name"] for country in payload["countries"]}
        self.assertEqual(payload["count"], 1)
        self.assertIn("Test Country", names)
        self.assertNotIn("Custom Republic", names)
        self.assertNotIn("Stale Country", names)
        self.assertTrue(all("id" in country for country in payload["countries"]))
        self.assertTrue(payload["countries"][0]["is_date_dataset"])

    def test_globe_clips_high_resolution_world_after_simplifying(self):
        high_country_dir = self.data_dir / "High-Resolution" / "Country"
        high_country_dir.mkdir(parents=True)

        date_geometry = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        }
        world_geometry = {
            "type": "Polygon",
            "coordinates": [[[1, 0], [3, 0], [3, 2], [1, 2], [1, 0]]],
        }
        (high_country_dir / "Date_Republic.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"DATASET": "DATE"},
                    "geometry": date_geometry,
                }],
            }),
            encoding="utf-8",
        )
        (high_country_dir / "world.json").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {},
                    "geometry": world_geometry,
                }],
            }),
            encoding="utf-8",
        )

        response = self.client.get("/api/globe/countries")

        self.assertEqual(response.status_code, 200)
        countries = response.get_json()["countries"]
        date_country = next(country for country in countries if country["name"] == "Date Republic")
        world_country = next(country for country in countries if country["name"] == "World")
        self.assertTrue(date_country["is_date_dataset"])
        date_shape = shape(date_country["geometries"][0])
        world_shape = shape(world_country["geometries"][0])
        self.assertEqual(world_shape.area, 2)
        self.assertEqual(world_shape.intersection(date_shape).area, 0)

    def test_globe_simplification_preserves_significant_border_shape(self):
        dense_bottom_edge = [[index / 10, 0] for index in range(101)]
        ring = dense_bottom_edge + [
            [10, 10],
            [5.1, 10],
            [5, 6],
            [4.9, 10],
            [0, 10],
            [0, 0],
        ]
        geometry = {"type": "Polygon", "coordinates": [ring]}

        simplified = app.simplify_geometry_for_globe(geometry)
        simplified_ring = simplified["coordinates"][0]

        self.assertLess(len(simplified_ring), len(ring))
        self.assertIn([5, 6], simplified_ring)
        self.assertEqual(simplified_ring[0], simplified_ring[-1])

    def test_globe_country_payload_is_cached_until_source_changes(self):
        country_dir = self.data_dir / "High-Resolution" / "Country"
        country_dir.mkdir(parents=True)
        source_path = country_dir / "test_country.json"

        def write_source(right):
            source_path.write_text(
                json.dumps({
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [right, 0], [right, 1], [0, 0]]],
                        },
                        "properties": {},
                    }],
                }),
                encoding="utf-8",
            )

        write_source(1)

        with patch.object(app, "build_globe_countries", wraps=app.build_globe_countries) as builder:
            first_payload = app.ensure_globe_countries_cached()
            second_payload = app.ensure_globe_countries_cached()
            cache_mtime_ns = app.globe_countries_cache_path().stat().st_mtime_ns
            write_source(2)
            os.utime(source_path, ns=(cache_mtime_ns + 1_000_000, cache_mtime_ns + 1_000_000))
            third_payload = app.ensure_globe_countries_cached()
            source_path.unlink()
            fourth_payload = app.ensure_globe_countries_cached()

        self.assertEqual(first_payload, second_payload)
        self.assertNotEqual(first_payload, third_payload)
        self.assertEqual(fourth_payload["count"], 0)
        self.assertEqual(builder.call_count, 3)
        self.assertTrue(app.globe_countries_cache_path().exists())

    def test_globe_country_api_ignores_cache_only_polygon_files(self):
        source_dir = self.cache_dir / "World"
        source_dir.mkdir()
        source_file = source_dir / "test_country.json"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
                    },
                    "properties": {"name": "Test Country"},
                }],
            }),
            encoding="utf-8",
        )

        res = self.client.get("/api/globe/countries")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["count"], 0)

    def test_globe_capitals_api_separates_world_and_date_sources(self):
        legacy_dir = self.data_dir / "Low-Resolution" / "Place" / "Capital City"
        date_dir = self.data_dir / "High-Resolution" / "Place" / "Capital City"
        world_dir = self.data_dir / "High-Resolution" / "Place" / "Capital Cities"
        legacy_dir.mkdir(parents=True)
        date_dir.mkdir(parents=True)
        world_dir.mkdir(parents=True)
        (legacy_dir / "Capital_City_Legacyville.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [10, 20]},
                    "properties": {
                        "name": "Legacyville",
                        "country": "Exampleland",
                    },
                }],
            }),
            encoding="utf-8",
        )
        (date_dir / "Capital_City_Dateville.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "id": "date-1",
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [151.2093, -33.8688]},
                    "properties": {
                        "name": "Dateville",
                        "country": "DATE Exampleland",
                        "region": "Test",
                    },
                }],
            }),
            encoding="utf-8",
        )
        (world_dir / "World_Capital_Cities.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "id": "world-1",
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1, 2]},
                    "properties": {
                        "name": "Worldville",
                        "country": "World Exampleland",
                        "DATASET": "WORLD",
                    },
                }],
            }),
            encoding="utf-8",
        )

        res = self.client.get("/api/globe/capitals")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["count"], 2)
        capitals = {capital["name"]: capital for capital in payload["capitals"]}
        self.assertNotIn("Legacyville", capitals)
        self.assertEqual(capitals["Dateville"]["dataset"], "DATE")
        self.assertEqual(capitals["Dateville"]["longitude"], 151.2093)
        self.assertEqual(capitals["Dateville"]["latitude"], -33.8688)
        self.assertEqual(capitals["Worldville"]["dataset"], "WORLD")
        self.assertEqual(len({capital["id"] for capital in capitals.values()}), 2)

    def test_main_map_geojson_auto_discovery_uses_high_resolution_only(self):
        high_source_dir = self.data_dir / "High-Resolution" / "Country"
        low_source_dir = self.data_dir / "Low-Resolution" / "Country"
        other_source_dir = self.data_dir / "Country"
        high_source_dir.mkdir(parents=True)
        low_source_dir.mkdir(parents=True)
        other_source_dir.mkdir(parents=True)

        source_file = high_source_dir / "Nested_Country.geojson"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
                    },
                    "properties": {"NAME_FULL": "Nested Country"},
                }],
            }),
            encoding="utf-8",
        )
        for skipped_dir in (low_source_dir, other_source_dir):
            (skipped_dir / "Nested_Country.geojson").write_text(
                source_file.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        layers_res = self.client.get("/api/layers")
        self.assertEqual(layers_res.status_code, 200)
        data_layers = [
            item for item in layers_res.get_json()
            if item["id"].startswith("geojson__")
        ]
        self.assertEqual(len(data_layers), 1)
        self.assertEqual(
            data_layers[0]["title"],
            "High-Resolution — Country — Nested_Country.geojson (GeoJSON)",
        )

        layer_res = self.client.get(f"/api/layer/{data_layers[0]['id']}")
        self.assertEqual(layer_res.status_code, 200)
        payload = layer_res.get_json()
        self.assertEqual(payload["features"][0]["properties"]["NAME_FULL"], "Nested Country")

    def test_undersea_cables_are_an_optional_infrastructure_layer(self):
        source_dir = self.data_dir / "High-Resolution" / "Infrastructure"
        source_dir.mkdir(parents=True)
        source_file = source_dir / "cables.geojson"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "MultiLineString",
                        "coordinates": [[[151, -33], [152, -32]]],
                    },
                    "properties": {
                        "id": "test-cable",
                        "name": "Test Cable",
                        "color": "#12a4c7",
                    },
                }],
            }),
            encoding="utf-8",
        )

        layers = self.client.get("/api/layers").get_json()
        cable_layer = next(item for item in layers if item.get("category") == "Infrastructure")

        self.assertEqual(cable_layer["subcategory"], "Undersea Cables")
        self.assertEqual(cable_layer["name"], "Cables")
        self.assertFalse(cable_layer["default_visible"])

        layer_response = self.client.get(f"/api/layer/{cable_layer['id']}")
        self.assertEqual(layer_response.status_code, 200)
        self.assertEqual(
            layer_response.get_json()["features"][0]["properties"]["color"],
            "#12a4c7",
        )

    def test_undersea_cables_include_terminal_country_layer_ids(self):
        country_dir = self.data_dir / "High-Resolution" / "Country"
        cable_dir = self.data_dir / "High-Resolution" / "Infrastructure"
        country_dir.mkdir(parents=True)
        cable_dir.mkdir(parents=True)

        alpha_file = country_dir / "Alpha.geojson"
        alpha_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
                    },
                    "properties": {"NAME_SHORT": "Alpha", "DATASET": "WORLD"},
                }],
            }),
            encoding="utf-8",
        )
        (country_dir / "Beta.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[9, -1], [11, -1], [11, 1], [9, 1], [9, -1]]],
                    },
                    "properties": {"NAME_SHORT": "Beta", "DATASET": "WORLD"},
                }],
            }),
            encoding="utf-8",
        )
        (cable_dir / "cables.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "MultiLineString",
                        "coordinates": [
                            [[1.2, 0], [5, 0]],
                            [[6, 0], [10, 0]],
                        ],
                    },
                    "properties": {"id": "alpha-beta", "name": "Alpha Beta"},
                }],
            }),
            encoding="utf-8",
        )

        layers = self.client.get("/api/layers").get_json()
        layer_ids = {item["name"]: item["id"] for item in layers}
        cable_layer_id = layer_ids["Cables"]
        response = self.client.get(f"/api/layer/{cable_layer_id}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            set(payload["features"][0]["terminal_country_ids"]),
            {layer_ids["Alpha"], layer_ids["Beta"]},
        )
        self.assertEqual(
            payload["date_mapper_terminal_country_version"],
            app.CABLE_TERMINAL_COUNTRY_CACHE_VERSION,
        )

        alpha_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[39, -1], [41, -1], [41, 1], [39, 1], [39, -1]]],
                    },
                    "properties": {"NAME_SHORT": "Alpha", "DATASET": "WORLD"},
                }],
            }),
            encoding="utf-8",
        )
        refreshed_payload = self.client.get(f"/api/layer/{cable_layer_id}").get_json()
        self.assertEqual(
            refreshed_payload["features"][0]["terminal_country_ids"],
            [layer_ids["Beta"]],
        )

    def test_globe_cable_api_reads_high_resolution_cable_routes(self):
        high_source_dir = self.data_dir / "High-Resolution" / "Infrastructure"
        low_source_dir = self.data_dir / "Low-Resolution" / "Undersea Cables"
        high_source_dir.mkdir(parents=True)
        low_source_dir.mkdir(parents=True)
        cable_payload = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[151, -33], [152, -32], [153, -31]]],
                },
                "properties": {
                    "id": "test-cable",
                    "name": "Test Cable",
                    "color": "#12a4c7",
                    "owners": ["Example Networks"],
                    "rfs_year": 2024,
                },
            }],
        }
        (high_source_dir / "cables.geojson").write_text(
            json.dumps(cable_payload),
            encoding="utf-8",
        )
        (low_source_dir / "ignored.geojson").write_text(
            json.dumps(cable_payload),
            encoding="utf-8",
        )

        response = self.client.get("/api/globe/cables")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertEqual(payload["type"], "GlobeCableCollection")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["source_count"], 1)
        self.assertEqual(payload["cables"][0]["name"], "Test Cable")
        self.assertEqual(payload["cables"][0]["geometry"]["type"], "MultiLineString")
        self.assertTrue(app.globe_cables_cache_path().exists())

    def test_critical_infrastructure_selects_the_best_shapefile_representation(self):
        source_dir = (
            self.data_dir
            / "High-Resolution"
            / "Infrastructure"
            / "Critical Infrastructure"
        )

        def write_shapefile(folder_name, geometries):
            folder = source_dir / folder_name
            folder.mkdir(parents=True)
            frame = app.gpd.GeoDataFrame(
                {"name": [f"Asset {index}" for index in range(len(geometries))]},
                geometry=geometries,
                crs="EPSG:4326",
            )
            frame.to_file(folder / f"{folder_name}.shp", index=False)

        write_shapefile("power_generator_point", [Point(6, 49)])
        write_shapefile(
            "power_generator_polygon",
            [
                Polygon([(6, 49), (6.01, 49), (6.01, 49.01), (6, 49.01)]),
                Polygon([(6.02, 49), (6.03, 49), (6.03, 49.01), (6.02, 49.01)]),
            ],
        )
        write_shapefile("water_well_point", [Point(6, 49), Point(6.02, 49)])
        write_shapefile(
            "water_well_polygon",
            [Polygon([(6, 49), (6.01, 49), (6.01, 49.01), (6, 49.01)])],
        )

        empty_folder = source_dir / "telecom_cable"
        empty_folder.mkdir(parents=True)
        app.gpd.GeoDataFrame(
            {"name": []},
            geometry=[],
            crs="EPSG:4326",
        ).to_file(empty_folder / "telecom_cable.shp", index=False)

        layers_response = self.client.get("/api/layers")
        self.assertEqual(layers_response.status_code, 200)
        infrastructure_layers = [
            item for item in layers_response.get_json()
            if item["id"].startswith("shp__")
        ]
        self.assertEqual(len(infrastructure_layers), 2)

        layers_by_name = {item["name"]: item for item in infrastructure_layers}
        generator = layers_by_name["Power Generator"]
        water_well = layers_by_name["Water Well"]

        for item in infrastructure_layers:
            self.assertEqual(item["category"], "Infrastructure")
            self.assertEqual(item["subcategory"], "Critical Infrastructure")
            self.assertFalse(item["default_visible"])

        self.assertIn("power_generator_polygon", generator["title"])
        self.assertEqual(generator["feature_count"], 2)
        self.assertIn("water_well_point", water_well["title"])
        self.assertEqual(water_well["feature_count"], 2)

        generator_response = self.client.get(f"/api/layer/{generator['id']}")
        self.assertEqual(generator_response.status_code, 200)
        generator_payload = generator_response.get_json()
        self.assertEqual(len(generator_payload["features"]), 2)
        self.assertTrue(all(
            feature["geometry"]["type"] == "Polygon"
            for feature in generator_payload["features"]
        ))

        water_well_response = self.client.get(f"/api/layer/{water_well['id']}")
        self.assertEqual(water_well_response.status_code, 200)
        water_well_payload = water_well_response.get_json()
        self.assertEqual(len(water_well_payload["features"]), 2)
        self.assertTrue(all(
            feature["geometry"]["type"] == "Point"
            for feature in water_well_payload["features"]
        ))

    def test_removed_shapefile_is_not_populated_from_its_generated_cache(self):
        source_dir = (
            self.data_dir
            / "High-Resolution"
            / "Infrastructure"
            / "Critical Infrastructure"
            / "pipeline"
        )
        source_dir.mkdir(parents=True)
        source_file = source_dir / "pipeline.shp"
        app.gpd.GeoDataFrame(
            {"name": ["Test pipeline"]},
            geometry=[Point(6, 49)],
            crs="EPSG:4326",
        ).to_file(source_file, index=False)

        first_catalog = self.client.get("/api/layers").get_json()
        layer = next(item for item in first_catalog if item["id"].startswith("shp__"))
        layer_response = self.client.get(f"/api/layer/{layer['id']}")
        self.assertEqual(layer_response.status_code, 200)
        self.assertTrue(app.cache_path(layer["id"]).exists())

        for sidecar in source_dir.iterdir():
            sidecar.unlink()

        second_catalog = self.client.get("/api/layers").get_json()
        self.assertFalse(any(item["id"].startswith("shp__") for item in second_catalog))

    def test_invalid_shapefile_placeholder_is_not_populated(self):
        source_dir = (
            self.data_dir
            / "High-Resolution"
            / "Infrastructure"
            / "Critical Infrastructure"
            / "pipeline"
        )
        source_dir.mkdir(parents=True)
        (source_dir / "pipeline.shp").write_text(
            "This source data is not installed.",
            encoding="utf-8",
        )

        catalog = self.client.get("/api/layers").get_json()

        self.assertFalse(any(item["id"].startswith("shp__") for item in catalog))

    def test_data_folder_categories_and_subcategories_are_exposed(self):
        source_dir = self.data_dir / "High-Resolution" / "Place" / "Capital City"
        world_dir = self.data_dir / "High-Resolution" / "Place" / "Capital Cities"
        source_dir.mkdir(parents=True)
        world_dir.mkdir(parents=True)
        source_file = source_dir / "Capital_City_Testville.geojson"
        source_file.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [151.2093, -33.8688]},
                    "properties": {"name": "Testville"},
                }],
            }),
            encoding="utf-8",
        )
        (world_dir / "World_Capital_Cities.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1, 2]},
                    "properties": {"name": "Worldville", "DATASET": "WORLD"},
                }],
            }),
            encoding="utf-8",
        )

        layers_res = self.client.get("/api/layers")
        self.assertEqual(layers_res.status_code, 200)
        items = {item["name"]: item for item in layers_res.get_json()}

        self.assertEqual(items["Testville"]["category"], "Place")
        self.assertEqual(items["Testville"]["subcategory"], "DATE Capital Cities")
        self.assertTrue(items["Testville"]["default_visible"])
        self.assertEqual(items["World Capital Cities"]["category"], "Place")
        self.assertEqual(items["World Capital Cities"]["subcategory"], "Capital Cities")
        self.assertFalse(items["World Capital Cities"]["default_visible"])

    def test_nested_critical_infrastructure_section_is_exposed(self):
        wind_farm_dir = (
            self.data_dir
            / "High-Resolution"
            / "Infrastructure"
            / "Critical Infrastructure"
            / "Wind Farms"
        )
        wind_farm_dir.mkdir(parents=True)
        (wind_farm_dir / "Global_Wind_Power_Tracker.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [8.25, 55.5]},
                    "properties": {"name": "Example wind farm"},
                }],
            }),
            encoding="utf-8",
        )

        response = self.client.get("/api/layers")

        self.assertEqual(response.status_code, 200)
        wind_layer = next(
            item for item in response.get_json()
            if item.get("name") == "Global Wind Power Tracker"
        )
        self.assertEqual(wind_layer["category"], "Infrastructure")
        self.assertEqual(wind_layer["subcategory"], "Critical Infrastructure")
        self.assertEqual(wind_layer["section"], "Wind Farms")
        self.assertFalse(wind_layer["default_visible"])

    def test_high_resolution_wrapper_preserves_categories(self):
        country_dir = self.data_dir / "High-Resolution" / "Country"
        capital_dir = self.data_dir / "High-Resolution" / "Place" / "Capital City"
        country_dir.mkdir(parents=True)
        capital_dir.mkdir(parents=True)
        (country_dir / "test_country.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[10, 0], [11, 0], [11, 1], [10, 1], [10, 0]]],
                    },
                    "properties": {"name": "Test Country"},
                }],
            }),
            encoding="utf-8",
        )
        (capital_dir / "Capital_City_Testville.geojson").write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [151.2093, -33.8688]},
                    "properties": {"name": "Testville"},
                }],
            }),
            encoding="utf-8",
        )

        layers_res = self.client.get("/api/layers")
        self.assertEqual(layers_res.status_code, 200)
        items = {
            item["name"]: item
            for item in layers_res.get_json()
            if item["id"].startswith("geojson__")
        }

        self.assertEqual(items["Test Country"]["category"], "Country")
        self.assertIsNone(items["Test Country"]["subcategory"])
        self.assertEqual(items["Testville"]["category"], "Place")
        self.assertEqual(items["Testville"]["subcategory"], "DATE Capital Cities")


if __name__ == "__main__":
    unittest.main()
