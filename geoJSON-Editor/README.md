# GeoJSON Editor

A small Flask + Leaflet app for loading a primary `.geojson` file, selecting and moving coordinate vertices, and downloading the edited GeoJSON as a new object.
It uses a local vector world basemap instead of internet raster tiles, so the background does not break into blank tile squares.
Leaflet is vendored locally in `static/vendor/leaflet`, so the editor does not need CDN access at runtime.

The main DATE Mapper app uses port `9099`, so this editor runs on port `9101`.

## Run

```bash
cd geoJSON-Editor
python3 app.py
```

Open: http://127.0.0.1:9101

## Use

1. Load an editable `.geojson` with **Editable GeoJSON**.
2. Load optional comparison overlays with **Read-Only Comparison GeoJSONs**, or choose multiple **Built-In Read-Only Countries** and click **Add Selected Countries**.
3. Click vertex markers to select them. Shift-click or use box-select/select-all controls for multiple vertices.
4. Drag any selected marker to move all selected vertices together, or use the coordinate/property panel.
5. Use **Delete Selected** to remove one or more selected vertices; the remaining neighboring vertices are linked automatically by the geometry.
6. Click **Download Edited GeoJSON** to save the result as a new file.

Notes:

- Point, MultiPoint, LineString, MultiLineString, Polygon, MultiPolygon, and GeometryCollection coordinates are editable.
- Polygon closing coordinates are kept in sync when the first ring vertex moves.
- Deleting enough vertices to invalidate an interior polygon ring removes that whole ring, which is useful for deleting holes.
- Deletion is blocked if it would leave a line with fewer than two vertices or partially break an exterior polygon ring.
- Very large files show only the first 2,500 visible vertex handles at once; zoom into the area you want to edit for finer control.
- Comparison layers are always read-only.
