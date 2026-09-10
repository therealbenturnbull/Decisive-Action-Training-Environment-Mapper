#!/usr/bin/env python3
# Aim: Download complete Geoscience Australia ArcGIS feature layers as GeoJSON.
# Author: Benjamin Turnbull

"""Interactively list and download Geoscience Australia ArcGIS layers.

The script accepts a Geoscience Australia ArcGIS MapServer or FeatureServer
URL, lists its layer names and geometry types, and downloads one selected
feature layer as a complete EPSG:4326 GeoJSON FeatureCollection.

ArcGIS services normally limit a single feature response (often to 2,000
records). To retrieve the complete layer, this script first requests all
matching object IDs and then downloads those IDs in smaller batches. The
result is streamed to disk, so the full dataset is not kept in memory.

Examples:
    python3 ga_arcgis_downloader.py
    python3 ga_arcgis_downloader.py \
      https://services.ga.gov.au/gis/rest/services/Oil_Gas_Pipelines/MapServer
    python3 ga_arcgis_downloader.py \
      https://services.ga.gov.au/gis/rest/services/Electricity_Infrastructure/MapServer \
      --layer 2 --output power_lines.geojson
    python3 ga_arcgis_downloader.py SERVICE_URL --list-only

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_SERVICE_URL = (
    "https://services.ga.gov.au/gis/rest/services/"
    "Electricity_Infrastructure/MapServer"
)
USER_AGENT = "GA-ArcGIS-GeoJSON-Downloader/1.0"

GEOMETRY_LABELS = {
    "esriGeometryPoint": "Point",
    "esriGeometryMultipoint": "MultiPoint",
    "esriGeometryPolyline": "Line",
    "esriGeometryPolygon": "Polygon",
    "esriGeometryEnvelope": "Envelope",
}


class ArcGISError(RuntimeError):
    """Raised when an ArcGIS service or response cannot be used."""


@dataclass(frozen=True)
class LayerChoice:
    """A layer or table advertised by an ArcGIS service."""

    id: int
    name: str
    layer_type: str
    geometry_type: str | None
    selectable: bool


class ArcGISClient:
    """Small standard-library JSON client with retry and ArcGIS error handling."""

    def __init__(self, timeout: float = 60.0, retries: int = 4) -> None:
        self.timeout = timeout
        self.retries = retries

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_url = add_query(url, params or {})
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            request = Request(
                request_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    payload = json.loads(response.read().decode(charset))
                if not isinstance(payload, dict):
                    raise ArcGISError(f"Expected a JSON object from {url}")
                raise_for_arcgis_error(payload, url)
                return payload
            except HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    detail = read_http_error(exc)
                    raise ArcGISError(
                        f"HTTP {exc.code} from {url}{detail}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                wait_seconds = parse_retry_after(retry_after, attempt)
            except (URLError, socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise ArcGISError(f"Network error while requesting {url}: {exc}") from exc
                wait_seconds = min(2**attempt, 15)
            except json.JSONDecodeError as exc:
                raise ArcGISError(f"Invalid JSON returned by {url}: {exc}") from exc

            print(
                f"Request failed; retrying in {wait_seconds:g} seconds "
                f"({attempt + 1}/{self.retries})...",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

        raise ArcGISError(f"Request failed: {last_error}")


def add_query(url: str, params: dict[str, Any]) -> str:
    """Return *url* with encoded query parameters, preserving existing ones."""

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def normalize_arcgis_url(url: str) -> str:
    """Validate and normalize a MapServer/FeatureServer or direct layer URL."""

    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ArcGISError("The service URL must be an absolute HTTP or HTTPS URL.")

    clean_path = parts.path.rstrip("/")
    if not re.search(r"/(?:MapServer|FeatureServer)(?:/\d+)?$", clean_path, re.I):
        raise ArcGISError(
            "Expected a URL ending in /MapServer, /FeatureServer, "
            "or a numeric layer such as /MapServer/2."
        )
    return urlunsplit((parts.scheme, parts.netloc, clean_path, "", ""))


def split_layer_url(url: str) -> tuple[str, int | None]:
    """Return (service URL, optional layer ID) for a normalized ArcGIS URL."""

    match = re.search(r"/(MapServer|FeatureServer)(?:/(\d+))?$", url, re.I)
    if not match:
        raise ArcGISError(f"Not an ArcGIS service or layer URL: {url}")
    layer_id = int(match.group(2)) if match.group(2) is not None else None
    service_url = url[: match.start()] + "/" + match.group(1)
    return service_url, layer_id


def raise_for_arcgis_error(payload: dict[str, Any], url: str) -> None:
    """Convert an ArcGIS JSON error object into a readable exception."""

    error = payload.get("error")
    if not isinstance(error, dict):
        return

    code = error.get("code", "unknown")
    message = error.get("message", "ArcGIS request failed")
    details = error.get("details")
    suffix = ""
    if isinstance(details, list) and details:
        suffix = ": " + "; ".join(str(item) for item in details if item)
    raise ArcGISError(f"ArcGIS error {code} from {url}: {message}{suffix}")


def read_http_error(exc: HTTPError) -> str:
    """Extract a short ArcGIS error message from an HTTP error body."""

    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    message = error.get("message")
    details = error.get("details")
    text = str(message) if message else ""
    if isinstance(details, list) and details:
        text += ": " + "; ".join(str(item) for item in details if item)
    return f" ({text})" if text else ""


def parse_retry_after(value: str | None, attempt: int) -> float:
    """Return a bounded retry delay from Retry-After or exponential backoff."""

    if value:
        try:
            return min(max(float(value), 0.0), 60.0)
        except ValueError:
            pass
    return float(min(2**attempt, 15))


def layer_choices(service_metadata: dict[str, Any]) -> list[LayerChoice]:
    """Build a list of advertised layers and tables."""

    choices: list[LayerChoice] = []
    for item in list(service_metadata.get("layers") or []) + list(
        service_metadata.get("tables") or []
    ):
        if not isinstance(item, dict) or "id" not in item:
            continue
        geometry_type = item.get("geometryType")
        layer_type = str(item.get("type") or "Unknown")
        sublayers = item.get("subLayerIds")
        selectable = bool(geometry_type) and not sublayers
        choices.append(
            LayerChoice(
                id=int(item["id"]),
                name=str(item.get("name") or f"Layer {item['id']}"),
                layer_type=layer_type,
                geometry_type=str(geometry_type) if geometry_type else None,
                selectable=selectable,
            )
        )
    return choices


def direct_layer_choice(metadata: dict[str, Any], layer_id: int) -> LayerChoice:
    """Build a choice object from direct layer metadata."""

    geometry_type = metadata.get("geometryType")
    return LayerChoice(
        id=layer_id,
        name=str(metadata.get("name") or f"Layer {layer_id}"),
        layer_type=str(metadata.get("type") or "Unknown"),
        geometry_type=str(geometry_type) if geometry_type else None,
        selectable=bool(geometry_type),
    )


def print_layer_table(choices: Sequence[LayerChoice]) -> None:
    """Print layer names and both ArcGIS and geometry types."""

    if not choices:
        print("No layers or tables were advertised by this service.")
        return

    headers = ("Choice", "ID", "Name", "ArcGIS type", "Geometry")
    rows: list[tuple[str, str, str, str, str]] = []
    selectable_number = 0
    for choice in choices:
        if choice.selectable:
            selectable_number += 1
            selection = str(selectable_number)
        else:
            selection = "-"
        geometry = GEOMETRY_LABELS.get(
            choice.geometry_type or "", choice.geometry_type or "None"
        )
        rows.append(
            (selection, str(choice.id), choice.name, choice.layer_type, geometry)
        )

    widths = [len(value) for value in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print("\nAvailable layers and tables:\n")
    print("  ".join(value.ljust(widths[i]) for i, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
    print("\nRows marked '-' are not downloadable spatial feature layers.")


def choose_layer(choices: Sequence[LayerChoice], requested_id: int | None) -> LayerChoice:
    """Resolve --layer ID or interactively choose a selectable layer."""

    selectable = [choice for choice in choices if choice.selectable]
    if not selectable:
        raise ArcGISError("This service does not advertise a spatial feature layer.")

    if requested_id is not None:
        for choice in choices:
            if choice.id == requested_id:
                if not choice.selectable:
                    raise ArcGISError(
                        f"Layer ID {requested_id} is not a downloadable spatial feature layer."
                    )
                return choice
        valid_ids = ", ".join(str(choice.id) for choice in selectable)
        raise ArcGISError(f"Layer ID {requested_id} was not found. Valid IDs: {valid_ids}")

    if not sys.stdin.isatty():
        raise ArcGISError(
            "No interactive terminal is available. Select a layer with --layer ID."
        )

    while True:
        raw = input(f"\nChoose a layer [1-{len(selectable)}]: ").strip()
        try:
            position = int(raw)
        except ValueError:
            print("Enter the choice number shown in the first column.")
            continue
        if 1 <= position <= len(selectable):
            return selectable[position - 1]
        print(f"Enter a number from 1 to {len(selectable)}.")


def get_object_id_field(metadata: dict[str, Any], ids_payload: dict[str, Any]) -> str:
    """Find the layer's object-ID field across ArcGIS metadata variants."""

    for candidate in (
        ids_payload.get("objectIdFieldName"),
        metadata.get("objectIdField"),
        metadata.get("objectIdFieldName"),
    ):
        if candidate:
            return str(candidate)

    for field in metadata.get("fields") or []:
        if isinstance(field, dict) and field.get("type") == "esriFieldTypeOID":
            if field.get("name"):
                return str(field["name"])
    raise ArcGISError("The selected layer does not advertise an object-ID field.")


def supported_geojson(metadata: dict[str, Any]) -> bool:
    """Return whether layer metadata advertises native GeoJSON output."""

    formats = metadata.get("supportedQueryFormats") or ""
    if isinstance(formats, list):
        values = [str(item).lower() for item in formats]
    else:
        values = [item.strip().lower() for item in str(formats).split(",")]
    return "geojson" in values


def chunked(values: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Yield fixed-size lists from a sequence."""

    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def fetch_feature_batch(
    client: ArcGISClient,
    query_url: str,
    object_ids: Sequence[Any],
) -> list[dict[str, Any]]:
    """Fetch one ID batch, splitting it if the service returns too few records."""

    payload = client.get_json(
        query_url,
        {
            "objectIds": ",".join(str(value) for value in object_ids),
            "outFields": "*",
            "returnGeometry": "true",
            "returnZ": "false",
            "returnM": "false",
            "outSR": "4326",
            "f": "geojson",
        },
    )
    if payload.get("type") != "FeatureCollection":
        raise ArcGISError("The query did not return a GeoJSON FeatureCollection.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ArcGISError("The GeoJSON response does not contain a features array.")

    if len(features) == len(object_ids):
        return [feature for feature in features if isinstance(feature, dict)]

    # Complex geometries can trigger a transfer-size cap before maxRecordCount.
    # Recursively reducing the batch handles that case without dropping records.
    if len(object_ids) > 1:
        midpoint = len(object_ids) // 2
        return fetch_feature_batch(client, query_url, object_ids[:midpoint]) + fetch_feature_batch(
            client, query_url, object_ids[midpoint:]
        )

    raise ArcGISError(
        f"Object ID {object_ids[0]} returned {len(features)} features; expected 1."
    )


def safe_filename(value: str) -> str:
    """Convert a layer or service name into a portable filename component."""

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned.lower() or "layer"


def default_output_path(service_url: str, layer: LayerChoice) -> Path:
    """Create a descriptive default output filename."""

    service_name = service_url.rstrip("/").split("/")[-2]
    return Path(f"ga_{safe_filename(service_name)}_{safe_filename(layer.name)}.geojson")


def extract_feature_oid(feature: dict[str, Any], object_id_field: str) -> Any | None:
    """Read an object ID from GeoJSON properties without assuming field case."""

    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return None
    if object_id_field in properties:
        return properties[object_id_field]
    expected = object_id_field.casefold()
    for key, value in properties.items():
        if str(key).casefold() == expected:
            return value
    return None


def write_complete_geojson(
    client: ArcGISClient,
    service_url: str,
    layer: LayerChoice,
    metadata: dict[str, Any],
    output_path: Path,
    where: str,
    requested_batch_size: int,
    delay: float,
    overwrite: bool,
) -> int:
    """Download every matching feature and atomically create one GeoJSON file."""

    if output_path.exists() and not overwrite:
        raise ArcGISError(
            f"Output already exists: {output_path}. Use --overwrite to replace it."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    layer_url = f"{service_url}/{layer.id}"
    query_url = f"{layer_url}/query"

    count_payload = client.get_json(
        query_url,
        {"where": where, "returnCountOnly": "true", "f": "json"},
    )
    expected_count = count_payload.get("count")
    if not isinstance(expected_count, int) or expected_count < 0:
        raise ArcGISError("The service did not return a valid feature count.")

    ids_payload = client.get_json(
        query_url,
        {"where": where, "returnIdsOnly": "true", "f": "json"},
    )
    object_ids = ids_payload.get("objectIds")
    if not isinstance(object_ids, list):
        raise ArcGISError("The service did not return an objectIds array.")
    if len(object_ids) != expected_count:
        raise ArcGISError(
            "The count and object-ID queries disagree "
            f"({expected_count:,} features, {len(object_ids):,} IDs). "
            "The source may have changed during the request; run the script again."
        )

    object_id_field = get_object_id_field(metadata, ids_payload)
    max_record_count = metadata.get("maxRecordCount")
    if not isinstance(max_record_count, int) or max_record_count < 1:
        max_record_count = requested_batch_size
    batch_size = min(requested_batch_size, max_record_count)

    print(f"\nSelected: {layer.name} (ID {layer.id})")
    print(f"Features: {expected_count:,}")
    print(f"Server cap: {max_record_count:,}; download batch: {batch_size:,}")
    print(f"Output CRS: EPSG:4326")
    print(f"Writing: {output_path}")

    temporary_name: str | None = None
    written = 0
    seen_ids: set[Any] = set()
    first_feature = True

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write('{"type":"FeatureCollection","features":[')

            batches = list(chunked(object_ids, batch_size))
            for batch_number, ids in enumerate(batches, start=1):
                features = fetch_feature_batch(client, query_url, ids)
                for feature in features:
                    oid = extract_feature_oid(feature, object_id_field)
                    if oid is None:
                        raise ArcGISError(
                            f"A feature is missing its {object_id_field!r} object ID."
                        )
                    if oid in seen_ids:
                        raise ArcGISError(f"Duplicate object ID returned: {oid}")
                    seen_ids.add(oid)

                    if not first_feature:
                        handle.write(",")
                    json.dump(
                        feature,
                        handle,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    first_feature = False
                    written += 1

                print(
                    f"Downloaded {written:,}/{expected_count:,} "
                    f"(batch {batch_number}/{len(batches)})",
                    end="\r" if batch_number < len(batches) else "\n",
                    flush=True,
                )
                if delay and batch_number < len(batches):
                    time.sleep(delay)

            handle.write("]}\n")
            handle.flush()
            os.fsync(handle.fileno())

        if written != expected_count or len(seen_ids) != expected_count:
            raise ArcGISError(
                f"Verification failed: expected {expected_count:,}, wrote {written:,}."
            )

        os.replace(temporary_name, output_path)
        temporary_name = None
        return written
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""

    parser = argparse.ArgumentParser(
        description=(
            "List layers in a Geoscience Australia ArcGIS service, then "
            "download one complete layer as GeoJSON past the per-request cap."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s\n"
            "  %(prog)s SERVICE_URL --list-only\n"
            "  %(prog)s SERVICE_URL --layer 2 --output power_lines.geojson\n"
            "  %(prog)s SERVICE_URL --layer 0 --where \"STATE = 'ACT'\"\n"
        ),
    )
    parser.add_argument(
        "service_url",
        nargs="?",
        default=DEFAULT_SERVICE_URL,
        help=(
            "MapServer/FeatureServer URL, optionally ending in a layer ID "
            f"(default: {DEFAULT_SERVICE_URL})"
        ),
    )
    parser.add_argument(
        "--layer",
        type=int,
        metavar="ID",
        help="layer ID to download; omit for an interactive menu",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output .geojson path (default: derived from service and layer names)",
    )
    parser.add_argument(
        "--where",
        default="1=1",
        help='ArcGIS SQL filter (default: "1=1", meaning every feature)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="requested features per query, capped by the server maximum (default: 500)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help="polite delay between feature batches (default: 0.1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="timeout for each HTTP request (default: 60)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        metavar="N",
        help="retries for transient HTTP/network failures (default: 4)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="list layer names and types without downloading",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the output file if it already exists",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Validate numeric options before making network requests."""

    if args.batch_size < 1:
        raise ArcGISError("--batch-size must be at least 1.")
    if args.delay < 0:
        raise ArcGISError("--delay cannot be negative.")
    if args.timeout <= 0:
        raise ArcGISError("--timeout must be greater than zero.")
    if args.retries < 0:
        raise ArcGISError("--retries cannot be negative.")


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_args(args)
        normalized_url = normalize_arcgis_url(args.service_url)
        service_url, direct_layer_id = split_layer_url(normalized_url)
        client = ArcGISClient(timeout=args.timeout, retries=args.retries)

        if direct_layer_id is None:
            service_metadata = client.get_json(service_url, {"f": "pjson"})
            choices = layer_choices(service_metadata)
        else:
            direct_metadata = client.get_json(normalized_url, {"f": "pjson"})
            choices = [direct_layer_choice(direct_metadata, direct_layer_id)]

        print_layer_table(choices)
        if args.list_only:
            return 0

        if direct_layer_id is not None and args.layer not in (None, direct_layer_id):
            raise ArcGISError(
                f"The URL selects layer {direct_layer_id}, but --layer selects {args.layer}."
            )
        selected = choose_layer(
            choices, direct_layer_id if direct_layer_id is not None else args.layer
        )
        layer_url = f"{service_url}/{selected.id}"
        layer_metadata = (
            direct_metadata
            if direct_layer_id is not None
            else client.get_json(layer_url, {"f": "pjson"})
        )

        if not selected.geometry_type:
            raise ArcGISError("The selected item has no spatial geometry.")
        if not supported_geojson(layer_metadata):
            raise ArcGISError(
                "The selected layer does not advertise native GeoJSON query output."
            )

        output_path = args.output or default_output_path(service_url, selected)
        written = write_complete_geojson(
            client=client,
            service_url=service_url,
            layer=selected,
            metadata=layer_metadata,
            output_path=output_path.expanduser().resolve(),
            where=args.where,
            requested_batch_size=args.batch_size,
            delay=args.delay,
            overwrite=args.overwrite,
        )
        print(f"Complete: wrote {written:,} features to {output_path.expanduser().resolve()}")
        return 0
    except (ArcGISError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
