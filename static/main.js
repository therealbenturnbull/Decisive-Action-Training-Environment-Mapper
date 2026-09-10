// Aim: Drive the flat map, layer catalog, overlays, markers, and map interactions.
// Author: Benjamin Turnbull

let map;
let draftMarker;
let markerPlacementArmed = false;
let currentCatalog = [];
let selectedCountry = null;

const USER_MARKERS_LAYER_ID = "user_defined_markers";
const CATEGORY_ORDER = ["Country", "Place", "Infrastructure", "User-Defined"];

// Active overlays by id
const activeLayers = new Map();
const layerRows = new Map();
const layerLoadPromises = new Map();
let activeColourScheme = window.DateMapperColourSchemes.get(
  window.DateMapperSettings.get().colourScheme
);

function currentMapPalette() {
  return activeColourScheme.map;
}

function countryMapFillStyle(country) {
  if (activeColourScheme.id === "mirrorwave") {
    return country.isDateDataset
      ? "url(#mirrorwave-date-metal)"
      : "url(#mirrorwave-world-metal)";
  }
  return window.DateMapperColourSchemes.countryFill(
    activeColourScheme.id,
    country,
    "map"
  );
}

function refreshMapLayerStyles() {
  const highlightedCableFeatures = new Set();
  for (const layer of activeLayers.values()) {
    if (typeof layer.eachLayer !== "function") continue;
    layer.eachLayer((featureLayer) => {
      if (typeof featureLayer.setStyle !== "function" || !featureLayer.feature) return;
      const highlightedCable = cableTerminatesInSelectedCountry(
        featureLayer.feature,
        layer.__item
      );
      const mutedCable = Boolean(
        selectedCountry
        && isUnderseaCableItem(layer.__item)
        && !highlightedCable
      );
      featureLayer.setStyle(geoJsonFeatureStyle(
        featureLayer.feature,
        layer.__item,
        layer.__title
      ));

      const pathElement = typeof featureLayer.getElement === "function"
        ? featureLayer.getElement()
        : null;
      if (pathElement) {
        pathElement.classList.toggle("cable-terminal-highlight", highlightedCable);
        pathElement.classList.toggle("cable-terminal-muted", mutedCable);
        pathElement.classList.toggle(
          "selected-country-outline",
          isSelectedCountryItem(layer.__item)
        );
      }

      if (highlightedCable) {
        highlightedCableFeatures.add(featureLayer.feature);
        if (map?.hasLayer(layer) && typeof featureLayer.bringToFront === "function") {
          featureLayer.bringToFront();
        }
      }
    });
  }
  return highlightedCableFeatures.size;
}

function applyMapColourScheme(colourScheme = window.DateMapperSettings.get().colourScheme) {
  activeColourScheme = window.DateMapperColourSchemes.get(colourScheme);
  document.body.dataset.colourScheme = activeColourScheme.id;
  if (map) {
    map.getContainer().style.backgroundColor = currentMapPalette().ocean;
    refreshMapLayerStyles();
  }
}

function setStatus(msg) {
  document.getElementById("status").textContent = msg || "";
}

function setMarkerPlacementArmed(armed) {
  markerPlacementArmed = armed;
  const button = document.getElementById("pickMarkerBtn");
  button.classList.toggle("is-armed", armed);
  button.textContent = armed ? "Tap map now" : "Pick location";
}

function markerFormFields() {
  return {
    name: document.getElementById("markerName"),
    description: document.getElementById("markerDescription"),
    lat: document.getElementById("markerLat"),
    lng: document.getElementById("markerLng"),
    icon: document.getElementById("markerIcon"),
  };
}

function initMap() {
  map = L.map("map", {
    zoomControl: true,
    attributionControl: false,
  }).setView([0, 0], 2);

}

function featurePopupHtml(props) {
  const keys = Object.keys(props || {})
    .filter((key) => !(
      key.toLowerCase() === "capital_type"
      && String(props[key] || "").trim().toLowerCase() === "admin-0 capital"
    ))
    .slice(0, 15);
  if (!keys.length) return null;
  return keys.map((k) => `<div><b>${k}:</b> ${String(props[k])}</div>`).join("");
}

function prettifyHoverLabel(value) {
  return String(value || "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\w\S*/g, (word) => word[0].toUpperCase() + word.slice(1));
}

function titleToCountryLabel(title) {
  const cleaned = String(title || "")
    .replace(/\s*\((Cache\s+)?GeoJSON\)\s*$/i, "")
    .replace(/\s*\(GPKG\)\s*$/i, "")
    .split(" — ")
    .pop()
    .replace(/\.[^.]+$/, "")
    .replace(/_/g, " ")
    .trim();
  return prettifyHoverLabel(cleaned);
}

function featureHoverLabel(feature, title) {
  const props = feature?.properties || {};
  const candidates = [
    props.NAME_FULL,
    props.NAME_SHORT,
    props.COUNTRY,
    props.NAME,
    props.name,
    props.ADMIN,
    props.SOVEREIGNT,
    props.GID_0,
  ];

  const found = candidates.find((value) => String(value || "").trim());
  return prettifyHoverLabel(found || titleToCountryLabel(title) || "Country");
}

function isCoordinatePosition(value) {
  return Array.isArray(value)
    && value.length >= 2
    && typeof value[0] === "number"
    && typeof value[1] === "number";
}

function unwrapCoordinateLine(coords) {
  if (!Array.isArray(coords) || coords.length === 0) {
    return coords;
  }

  let longitudeOffset = 0;
  let previousLongitude = null;

  return coords.map((position) => {
    if (!isCoordinatePosition(position)) {
      return position;
    }

    let longitude = position[0] + longitudeOffset;

    if (previousLongitude !== null) {
      while (longitude - previousLongitude > 180) {
        longitudeOffset -= 360;
        longitude = position[0] + longitudeOffset;
      }

      while (longitude - previousLongitude < -180) {
        longitudeOffset += 360;
        longitude = position[0] + longitudeOffset;
      }
    }

    previousLongitude = longitude;
    const unwrappedPosition = position.slice();
    unwrappedPosition[0] = longitude;
    return unwrappedPosition;
  });
}

function normalizeGeometryAntimeridian(geometry) {
  if (!geometry) {
    return geometry;
  }

  switch (geometry.type) {
    case "LineString":
      return { ...geometry, coordinates: unwrapCoordinateLine(geometry.coordinates) };
    case "MultiLineString":
    case "Polygon":
      return {
        ...geometry,
        coordinates: geometry.coordinates.map((line) => unwrapCoordinateLine(line)),
      };
    case "MultiPolygon":
      return {
        ...geometry,
        coordinates: geometry.coordinates.map((polygon) => (
          polygon.map((ring) => unwrapCoordinateLine(ring))
        )),
      };
    case "GeometryCollection":
      return {
        ...geometry,
        geometries: geometry.geometries.map((item) => normalizeGeometryAntimeridian(item)),
      };
    default:
      return geometry;
  }
}

function normalizeGeoJsonAntimeridian(geojson, title) {
  if (!geojson) {
    return geojson;
  }

  if (geojson.type === "FeatureCollection") {
    return {
      ...geojson,
      features: geojson.features.map((feature) => ({
        ...feature,
        // A polar cap already spans both map edges. Unwrapping it shifts half
        // of Antarctica into the adjacent world copy.
        geometry: isAntarcticaFeature(feature, title)
          ? feature.geometry
          : normalizeGeometryAntimeridian(feature.geometry),
      })),
    };
  }

  if (geojson.type === "Feature") {
    return {
      ...geojson,
      geometry: isAntarcticaFeature(geojson, title)
        ? geojson.geometry
        : normalizeGeometryAntimeridian(geojson.geometry),
    };
  }

  return normalizeGeometryAntimeridian(geojson);
}

function isDateDatasetFeature(feature) {
  return String(feature?.properties?.DATASET || "").trim().toUpperCase() === "DATE";
}

function featureStrokeColor(feature, fallback) {
  const color = String(feature?.properties?.color || "").trim();
  return /^#[0-9a-f]{6}$/i.test(color) ? color : fallback;
}

function isUnderseaCableItem(item) {
  if (item?.category !== "Infrastructure") return false;
  const label = [item?.name, item?.subcategory, item?.title]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return /undersea|submarine|internet cable/.test(label);
}

function infrastructureLayerColor(item) {
  const label = `${item?.name || ""} ${item?.subcategory || ""}`.toLowerCase();
  if (isUnderseaCableItem(item)) return currentMapPalette().cableStroke;
  if (/water|wastewater|pumping/.test(label)) return "#167b91";
  if (/power|generator|transformer|substation/.test(label)) return "#b7790b";
  if (/telecom|mast/.test(label)) return "#6658a6";
  if (/petroleum|pipeline/.test(label)) return "#a9493a";
  return "#8c5b35";
}

function countryStyleDescriptor(feature, title) {
  const properties = feature?.properties || {};
  return {
    isDateDataset: isDateDatasetFeature(feature),
    key: properties.NAME_FULL
      || properties.NAME_SHORT
      || properties.CODE
      || title,
  };
}

function isSelectedCountryItem(item) {
  return Boolean(
    selectedCountry
    && item?.category === "Country"
    && item?.id === selectedCountry.layerId
  );
}

function cableTerminatesInSelectedCountry(feature, item) {
  return Boolean(
    selectedCountry
    && isUnderseaCableItem(item)
    && Array.isArray(feature?.terminal_country_ids)
    && feature.terminal_country_ids.includes(selectedCountry.layerId)
  );
}

function visibleUnderseaCableLayers() {
  return [...activeLayers.values()].filter((layer) => (
    isUnderseaCableItem(layer.__item) && map.hasLayer(layer)
  ));
}

function selectedCountryCableStatus(highlightedCount) {
  if (!selectedCountry) return "";
  if (!visibleUnderseaCableLayers().length) {
    return `${selectedCountry.name} selected. Submarine cables are hidden.`;
  }
  if (!highlightedCount) {
    return `No visible submarine cables terminate in ${selectedCountry.name}.`;
  }
  const noun = highlightedCount === 1 ? "cable" : "cables";
  return `${highlightedCount} submarine ${noun} terminating in ${selectedCountry.name} highlighted.`;
}

function clearSelectedCountry() {
  if (!selectedCountry) return false;
  selectedCountry = null;
  refreshMapLayerStyles();
  return true;
}

function selectCountry(item, feature, title) {
  const shortName = feature?.properties?.NAME_SHORT;
  selectedCountry = {
    layerId: item.id,
    name: shortName
      ? prettifyHoverLabel(shortName)
      : featureHoverLabel(feature, title),
  };
  const highlightedCount = refreshMapLayerStyles();
  setStatus(selectedCountryCableStatus(highlightedCount));
}

function geoJsonFeatureStyle(feature, item, title) {
  const geometryType = feature?.geometry?.type || "";
  const isInfrastructureLayer = item?.category === "Infrastructure";
  const isUnderseaCableLayer = isUnderseaCableItem(item);
  const infrastructureColor = infrastructureLayerColor(item);
  const palette = currentMapPalette();

  if (geometryType.includes("Polygon")) {
    if (isInfrastructureLayer) {
      return {
        color: infrastructureColor,
        fillColor: infrastructureColor,
        weight: 1.5,
        fillOpacity: 0.34,
      };
    }

    const country = countryStyleDescriptor(feature, title);
    const fillColor = countryMapFillStyle(country);
    const selected = isSelectedCountryItem(item);
    return {
      color: selected
        ? palette.pointFill
        : window.DateMapperColourSchemes.countryStroke(
          activeColourScheme.id,
          country,
          "map"
        ),
      fill: Boolean(fillColor),
      fillColor: fillColor || palette.ocean,
      fillOpacity: country.isDateDataset
        ? palette.dateCountryFillOpacity
        : palette.countryFillOpacity,
      weight: (
        country.isDateDataset
          ? palette.dateCountryLineWidth
          : palette.countryLineWidth
      ) + (selected ? 1.2 : 0),
    };
  }

  if (geometryType.includes("LineString")) {
    if (isUnderseaCableLayer && selectedCountry) {
      const highlighted = cableTerminatesInSelectedCountry(feature, item);
      return {
        color: highlighted
          ? palette.pointFill
          : featureStrokeColor(feature, infrastructureColor),
        weight: highlighted ? 4.6 : 1.2,
        opacity: highlighted ? 1 : 0.22,
      };
    }
    return {
      color: featureStrokeColor(
        feature,
        isInfrastructureLayer ? infrastructureColor : palette.lineStroke
      ),
      weight: isInfrastructureLayer ? 1.8 : 2,
      opacity: 0.9,
    };
  }

  if (geometryType.includes("Point")) {
    if (isInfrastructureLayer) {
      return {
        color: infrastructureColor,
        fillColor: infrastructureColor,
        fillOpacity: 0.78,
        weight: 1,
      };
    }
    return {
      color: palette.pointStroke,
      fillColor: palette.pointFill,
      fillOpacity: 0.75,
      weight: 2,
    };
  }

  return {};
}

function isAntarcticaFeature(feature, title) {
  const properties = feature?.properties || {};
  const candidates = [
    title,
    properties.COUNTRY,
    properties.NAME,
    properties.NAME_0,
    properties.NAME_FULL,
    properties.ADMIN,
    properties.GID_0,
  ].map((value) => String(value || "").trim().toLowerCase());

  return candidates.some((value) => value.includes("antarctica") || value === "ata");
}

function collectGeometryLatitudes(geometry, latitudes) {
  if (!geometry) return;

  if (geometry.type === "GeometryCollection") {
    geometry.geometries.forEach((item) => collectGeometryLatitudes(item, latitudes));
    return;
  }

  function visit(value) {
    if (isCoordinatePosition(value)) {
      latitudes.push(value[1]);
      return;
    }
    if (Array.isArray(value)) value.forEach(visit);
  }

  visit(geometry.coordinates);
}

function flipGeometryVertically(geometry, latitudeSum) {
  if (!geometry) return geometry;

  if (geometry.type === "GeometryCollection") {
    return {
      ...geometry,
      geometries: geometry.geometries.map((item) => flipGeometryVertically(item, latitudeSum)),
    };
  }

  function flipCoordinates(value) {
    if (isCoordinatePosition(value)) {
      const flipped = value.slice();
      flipped[1] = latitudeSum - value[1];
      return flipped;
    }
    return Array.isArray(value) ? value.map(flipCoordinates) : value;
  }

  return { ...geometry, coordinates: flipCoordinates(geometry.coordinates) };
}

function latitudeBoundsSum(latitudes) {
  let minimum = Infinity;
  let maximum = -Infinity;
  for (const latitude of latitudes) {
    minimum = Math.min(minimum, latitude);
    maximum = Math.max(maximum, latitude);
  }
  return minimum + maximum;
}

function applyMapSettingsToGeoJson(geojson, title) {
  const settings = window.DateMapperSettings?.get() || {};
  if (!settings.flipAntarcticaVertically || !geojson) {
    return geojson;
  }

  if (geojson.type === "FeatureCollection") {
    const antarcticaFeatures = geojson.features.filter((feature) => isAntarcticaFeature(feature, title));
    const latitudes = [];
    antarcticaFeatures.forEach((feature) => collectGeometryLatitudes(feature.geometry, latitudes));
    if (!latitudes.length) return geojson;

    const latitudeSum = latitudeBoundsSum(latitudes);
    return {
      ...geojson,
      features: geojson.features.map((feature) => (
        isAntarcticaFeature(feature, title)
          ? { ...feature, geometry: flipGeometryVertically(feature.geometry, latitudeSum) }
          : feature
      )),
    };
  }

  if (geojson.type === "Feature" && isAntarcticaFeature(geojson, title)) {
    const latitudes = [];
    collectGeometryLatitudes(geojson.geometry, latitudes);
    if (!latitudes.length) return geojson;
    const latitudeSum = latitudeBoundsSum(latitudes);
    return { ...geojson, geometry: flipGeometryVertically(geojson.geometry, latitudeSum) };
  }

  return geojson;
}

function makeGeoJsonLayer(layerId, title, geojson, item) {
  const preparedGeojson = applyMapSettingsToGeoJson(geojson, title);
  const isInfrastructureLayer = item?.category === "Infrastructure";
  const normalizedGeojson = normalizeGeoJsonAntimeridian(preparedGeojson, title);
  const gj = L.geoJSON(normalizedGeojson, {
    style: (feature) => geoJsonFeatureStyle(feature, item, title),
    pointToLayer: (feature, latlng) => {
      if (layerId === USER_MARKERS_LAYER_ID) {
        const iconUrl = feature?.properties?.icon_url;
        if (iconUrl) {
          return L.marker(latlng, {
            icon: L.icon({
              iconUrl,
              iconSize: [30, 30],
              iconAnchor: [15, 30],
              popupAnchor: [0, -28],
              className: "custom-marker-icon",
            }),
          });
        }
        return L.marker(latlng);
      }
      const pointStyle = geoJsonFeatureStyle(feature, item, title);
      if (isInfrastructureLayer) {
        return L.circleMarker(latlng, {
          radius: /tower|switch|compensator|transformer/.test(String(item?.name || "").toLowerCase()) ? 3 : 4,
          ...pointStyle,
        });
      }
      return L.circleMarker(latlng, {
        radius: 5,
        ...pointStyle,
      });
    },
    onEachFeature: (feature, layer) => {
      const html = featurePopupHtml(feature.properties || {});
      if (html) layer.bindPopup(html);

      const geometryType = feature?.geometry?.type || "";
      if (geometryType.includes("Polygon") || geometryType.includes("LineString")) {
        layer.bindTooltip(featureHoverLabel(feature, title), {
          className: "country-hover-tooltip",
          sticky: true,
          direction: "top",
          opacity: 0.95,
        });
      }

      if (geometryType.includes("Polygon") && item?.category === "Country") {
        layer.on("click", () => selectCountry(item, feature, title));
      }
    },
  });

  gj.__layerId = layerId;
  gj.__title = title;
  gj.__item = item;
  return gj;
}

function updateDraftMarker(lat, lng) {
  const latlng = [lat, lng];
  if (!draftMarker) {
    draftMarker = L.marker(latlng, { opacity: 0.85 });
    draftMarker.bindTooltip("Marker draft");
  } else {
    draftMarker.setLatLng(latlng);
  }

  if (!map.hasLayer(draftMarker)) {
    draftMarker.addTo(map);
  }
}

function clearDraftMarker() {
  if (draftMarker && map.hasLayer(draftMarker)) {
    map.removeLayer(draftMarker);
  }
}

function currentMarkerCoordinates() {
  const { lat, lng } = markerFormFields();
  const parsedLat = Number.parseFloat(lat.value);
  const parsedLng = Number.parseFloat(lng.value);

  if (!Number.isFinite(parsedLat) || !Number.isFinite(parsedLng)) {
    return null;
  }

  return { lat: parsedLat, lng: parsedLng };
}

function syncDraftMarkerFromInputs() {
  const coords = currentMarkerCoordinates();
  if (!coords) {
    clearDraftMarker();
    return;
  }
  updateDraftMarker(coords.lat, coords.lng);
}

async function fetchLayer(layerId) {
  const res = await fetch(`/api/layer/${encodeURIComponent(layerId)}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Failed to load layer");
  return data;
}

function categoryRank(category) {
  const index = CATEGORY_ORDER.indexOf(category);
  return index === -1 ? CATEGORY_ORDER.length : index;
}

function layerDisplayName(item) {
  return item.name || item.title || item.id || "Layer";
}

function layerCategory(item) {
  return CATEGORY_ORDER.includes(item.category) ? item.category : "User-Defined";
}

function layerSubcategory(item) {
  return item.subcategory || "General";
}

function layerSection(item) {
  return item.section || "";
}

function setRowState(layerId, state, message = "") {
  const row = layerRows.get(layerId);
  if (!row) return;

  row.status.textContent = message;
  row.element.classList.toggle("is-loading", state === "loading");
  row.element.classList.toggle("has-error", state === "error");
  row.checkbox.disabled = state === "loading" || state === "missing";
}

function updateGroupCounts() {
  document.querySelectorAll("[data-layer-group]").forEach((group) => {
    const ids = group.dataset.layerIds.split(",").filter(Boolean);
    const availableIds = ids.filter((id) => {
      const row = layerRows.get(id);
      return row && !row.missing;
    });
    const checkedCount = availableIds.filter((id) => {
      const row = layerRows.get(id);
      return row?.checkbox.checked;
    }).length;
    const count = group.querySelector("[data-group-count]");
    if (count) {
      count.textContent = `${checkedCount}/${availableIds.length}`;
    }
  });
}

async function setLayerVisibility(layerId, visible) {
  const row = layerRows.get(layerId);
  if (!row || row.missing) return;

  row.checkbox.checked = visible;
  updateGroupCounts();

  if (!visible) {
    const layer = activeLayers.get(layerId);
    if (layer && map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
    if (selectedCountry?.layerId === layerId) clearSelectedCountry();
    setRowState(layerId, "idle");
    return;
  }

  const existingLayer = activeLayers.get(layerId);
  if (existingLayer) {
    if (!map.hasLayer(existingLayer)) {
      existingLayer.addTo(map);
    }
    if (selectedCountry && isUnderseaCableItem(row.item)) {
      refreshMapLayerStyles();
    }
    setRowState(layerId, "idle");
    return;
  }

  if (layerLoadPromises.has(layerId)) {
    await layerLoadPromises.get(layerId);
    return;
  }

  const item = row.item;
  setRowState(layerId, "loading", "loading");
  setStatus(`Loading: ${layerDisplayName(item)}...`);

  const loadPromise = fetchLayer(layerId)
    .then((geojson) => {
      const layer = makeGeoJsonLayer(layerId, layerDisplayName(item), geojson, item);
      activeLayers.set(layerId, layer);
      if (row.checkbox.checked) {
        layer.addTo(map);
      }
      if (selectedCountry && isUnderseaCableItem(item)) {
        refreshMapLayerStyles();
      }
      setRowState(layerId, "idle");
      return layer;
    })
    .catch((error) => {
      row.checkbox.checked = false;
      setRowState(layerId, "error", "failed");
      throw new Error(`${layerDisplayName(item)}: ${error.message}`);
    })
    .finally(() => {
      layerLoadPromises.delete(layerId);
      updateGroupCounts();
    });

  layerLoadPromises.set(layerId, loadPromise);
  await loadPromise;
}

async function setLayerGroupVisibility(layerIds, visible) {
  for (const layerId of layerIds) {
    try {
      await setLayerVisibility(layerId, visible);
    } catch (error) {
      setStatus(`Loaded with issues: ${error.message}`);
    }
  }
  updateGroupCounts();
  const noun = layerIds.length === 1 ? "layer" : "layers";
  setStatus(`${visible ? "Showing" : "Hidden"} ${layerIds.length} ${noun}.`);
}

function createGroupActions(layerIds) {
  const actions = document.createElement("span");
  actions.className = "layer-group-actions";

  const onButton = document.createElement("button");
  onButton.type = "button";
  onButton.textContent = "All on";
  onButton.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setLayerGroupVisibility(layerIds, true).catch((error) => setStatus(String(error)));
  });

  const offButton = document.createElement("button");
  offButton.type = "button";
  offButton.textContent = "All off";
  offButton.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    setLayerGroupVisibility(layerIds, false).catch((error) => setStatus(String(error)));
  });

  const count = document.createElement("span");
  count.className = "layer-group-count";
  count.dataset.groupCount = "";

  actions.append(onButton, offButton, count);
  return actions;
}

function appendLayerRow(parent, item, visibleLayerIds, firstLoad) {
  const row = document.createElement("label");
  row.className = "layer-row";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = item.available && (
    firstLoad ? item.default_visible !== false : visibleLayerIds.has(item.id)
  );

  const name = document.createElement("span");
  name.className = "layer-name";
  name.textContent = layerDisplayName(item);

  const status = document.createElement("span");
  status.className = "layer-row-status";
  status.textContent = item.available ? "" : "missing";

  row.append(checkbox, name, status);
  parent.append(row);

  layerRows.set(item.id, {
    item,
    checkbox,
    status,
    element: row,
    missing: !item.available,
  });

  if (!item.available) {
    setRowState(item.id, "missing", "missing");
  }

  checkbox.addEventListener("change", async () => {
    const visible = checkbox.checked;
    try {
      await setLayerVisibility(item.id, visible);
      if (visible && selectedCountry && isUnderseaCableItem(item)) {
        setStatus(selectedCountryCableStatus(refreshMapLayerStyles()));
      } else {
        setStatus(`${visible ? "Showing" : "Hidden"}: ${layerDisplayName(item)}.`);
      }
    } catch (error) {
      setStatus(`Loaded with issues: ${error.message}`);
    }
  });
}

function setAllLayerGroupsExpanded(expanded) {
  document.querySelectorAll("#layerPanelList details").forEach((details) => {
    details.open = expanded;
  });
}

function renderLayerPanel(catalog, visibleLayerIds, firstLoad) {
  const panel = document.getElementById("layerPanel");
  const list = document.getElementById("layerPanelList");
  list.innerHTML = "";
  layerRows.clear();

  const sortedCatalog = [...catalog].sort((a, b) => (
    categoryRank(layerCategory(a)) - categoryRank(layerCategory(b))
      || layerSubcategory(a).localeCompare(layerSubcategory(b))
      || layerSection(a).localeCompare(layerSection(b))
      || layerDisplayName(a).localeCompare(layerDisplayName(b))
  ));

  const byCategory = new Map();
  for (const item of sortedCatalog) {
    const category = layerCategory(item);
    const subcategory = layerSubcategory(item);
    const section = layerSection(item);
    if (!byCategory.has(category)) byCategory.set(category, new Map());
    const subcategories = byCategory.get(category);
    if (!subcategories.has(subcategory)) subcategories.set(subcategory, new Map());
    // This used to collect layers directly under each subcategory, but doing so
    // flattened folders such as Wind Farms into the wider infrastructure list.
    // const sections = subcategories.get(subcategory) || [];
    const sections = subcategories.get(subcategory);
    if (!sections.has(section)) sections.set(section, []);
    sections.get(section).push(item);
  }

  const orderedCategories = [...byCategory.keys()].sort((a, b) => categoryRank(a) - categoryRank(b));
  for (const category of orderedCategories) {
    const subcategories = byCategory.get(category);
    const categoryItems = [...subcategories.values()].flatMap(
      (sections) => [...sections.values()].flat()
    );
    const categoryIds = categoryItems.map((item) => item.id);
    const categoryDetails = document.createElement("details");
    categoryDetails.className = "layer-category";
    categoryDetails.open = !["Country", "Place", "Infrastructure"].includes(category);
    categoryDetails.dataset.layerGroup = category;
    categoryDetails.dataset.layerIds = categoryIds.join(",");

    const categorySummary = document.createElement("summary");
    const categoryName = document.createElement("span");
    categoryName.className = "layer-group-title";
    categoryName.textContent = category;
    categorySummary.append(categoryName, createGroupActions(categoryIds));
    categoryDetails.append(categorySummary);

    for (const [subcategory, sections] of subcategories.entries()) {
      const items = [...sections.values()].flat();
      const subcategoryIds = items.map((item) => item.id);
      const subcategoryDetails = document.createElement("details");
      subcategoryDetails.className = "layer-subcategory";
      subcategoryDetails.open = false;
      subcategoryDetails.dataset.layerGroup = `${category}/${subcategory}`;
      subcategoryDetails.dataset.layerIds = subcategoryIds.join(",");

      const subcategorySummary = document.createElement("summary");
      const subcategoryName = document.createElement("span");
      subcategoryName.className = "layer-group-title";
      subcategoryName.textContent = subcategory;
      subcategorySummary.append(subcategoryName, createGroupActions(subcategoryIds));
      subcategoryDetails.append(subcategorySummary);

      for (const [section, sectionItems] of sections.entries()) {
        let rowParent = subcategoryDetails;
        if (section) {
          const sectionIds = sectionItems.map((item) => item.id);
          const sectionDetails = document.createElement("details");
          sectionDetails.className = "layer-section";
          sectionDetails.open = false;
          sectionDetails.dataset.layerGroup = `${category}/${subcategory}/${section}`;
          sectionDetails.dataset.layerIds = sectionIds.join(",");

          const sectionSummary = document.createElement("summary");
          const sectionName = document.createElement("span");
          sectionName.className = "layer-group-title";
          sectionName.textContent = section;
          sectionSummary.append(sectionName, createGroupActions(sectionIds));
          sectionDetails.append(sectionSummary);
          subcategoryDetails.append(sectionDetails);
          rowParent = sectionDetails;
        }

        for (const item of sectionItems) {
          appendLayerRow(rowParent, item, visibleLayerIds, firstLoad);
        }
      }

      categoryDetails.append(subcategoryDetails);
    }

    list.append(categoryDetails);
  }

  panel.hidden = false;
  updateGroupCounts();
}

async function createMarker(payload) {
  const fetchOptions = {
    method: "POST",
  };

  if (payload instanceof FormData) {
    fetchOptions.body = payload;
  } else {
    fetchOptions.headers = { "Content-Type": "application/json" };
    fetchOptions.body = JSON.stringify(payload);
  }

  const res = await fetch("/api/markers", {
    ...fetchOptions,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Failed to create marker");
  return data;
}

async function loadCatalogAndLayers() {
  setStatus("Loading layer catalog...");
  selectedCountry = null;
  const res = await fetch("/api/layers");
  const catalog = await res.json();

  if (!res.ok) {
    throw new Error("Unable to load layer catalog.");
  }

  currentCatalog = catalog;
  const firstLoad = activeLayers.size === 0;
  const visibleLayerIds = new Set();

  for (const [id, lyr] of activeLayers.entries()) {
    if (map.hasLayer(lyr)) {
      visibleLayerIds.add(id);
    }
  }

  for (const lyr of activeLayers.values()) {
    try {
      map.removeLayer(lyr);
    } catch {}
  }
  activeLayers.clear();
  layerLoadPromises.clear();

  renderLayerPanel(catalog, visibleLayerIds, firstLoad);

  let fitTargetBounds = null;
  const loadErrors = [];

  for (const item of catalog) {
    const row = layerRows.get(item.id);
    const shouldShow = item.available && row?.checkbox.checked;
    if (!shouldShow) continue;

    try {
      await setLayerVisibility(item.id, true);
      const lyr = activeLayers.get(item.id);
      if (firstLoad && item.fit_on_load && lyr) {
        try {
          fitTargetBounds = lyr.getBounds();
        } catch {}
      }
    } catch (error) {
      loadErrors.push(error.message);
    }
  }

  if (fitTargetBounds) {
    map.fitBounds(fitTargetBounds, { padding: [20, 20] });
  }

  if (loadErrors.length) {
    setStatus(`Loaded with issues: ${loadErrors.join(" | ")}`);
    return;
  }

  setStatus("Ready.");
}

function clearMarkerForm() {
  const { name, description, lat, lng, icon } = markerFormFields();
  name.value = "";
  description.value = "";
  lat.value = "";
  lng.value = "";
  icon.value = "";
  clearDraftMarker();
  setMarkerPlacementArmed(false);
}

function bindMarkerControls() {
  const { lat, lng, name, description, icon } = markerFormFields();

  document.getElementById("reloadBtn").addEventListener("click", () => {
    loadCatalogAndLayers().catch((e) => setStatus(String(e)));
  });

  document.getElementById("collapseLayerGroupsBtn").addEventListener("click", () => {
    setAllLayerGroupsExpanded(false);
  });

  document.getElementById("expandLayerGroupsBtn").addEventListener("click", () => {
    setAllLayerGroupsExpanded(true);
  });

  document.getElementById("pickMarkerBtn").addEventListener("click", () => {
    setMarkerPlacementArmed(!markerPlacementArmed);
    setStatus(markerPlacementArmed ? "Tap the map to place the marker." : "Marker placement cancelled.");
  });

  document.getElementById("clearMarkerBtn").addEventListener("click", () => {
    clearMarkerForm();
    setStatus("Marker draft cleared.");
  });

  document.getElementById("markerForm").addEventListener("submit", async (event) => {
    event.preventDefault();

    const payload = new FormData();
    payload.append("name", name.value.trim());
    payload.append("description", description.value.trim());
    payload.append("lat", lat.value);
    payload.append("lng", lng.value);

    if (icon.files && icon.files[0]) {
      payload.append("icon", icon.files[0]);
    }

    try {
      setStatus("Saving marker...");
      const feature = await createMarker(payload);
      await loadCatalogAndLayers();
      clearMarkerForm();
      const markerName = feature?.properties?.name || "Marker";
      setStatus(`Saved marker: ${markerName}.`);
    } catch (error) {
      setStatus(String(error));
    }
  });

  lat.addEventListener("input", syncDraftMarkerFromInputs);
  lng.addEventListener("input", syncDraftMarkerFromInputs);

  map.on("click", (event) => {
    if (!markerPlacementArmed) {
      if (event.sourceTarget === map && clearSelectedCountry()) {
        setStatus("Country selection cleared.");
      }
      return;
    }

    lat.value = event.latlng.lat.toFixed(6);
    lng.value = event.latlng.lng.toFixed(6);
    syncDraftMarkerFromInputs();
    setMarkerPlacementArmed(false);
    setStatus(`Marker coordinates set to ${lat.value}, ${lng.value}.`);
  });
}

window.addEventListener("storage", (event) => {
  if (event.key === window.DateMapperSettings.storageKey) {
    applyMapColourScheme();
  }
});

window.addEventListener("pageshow", () => applyMapColourScheme());

applyMapColourScheme();

document.addEventListener("DOMContentLoaded", async () => {
  initMap();
  applyMapColourScheme(activeColourScheme.id);
  bindMarkerControls();
  setMarkerPlacementArmed(false);

  loadCatalogAndLayers().catch((e) => setStatus(String(e)));
});
