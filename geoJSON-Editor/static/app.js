// Aim: Provide browser-side editing, selection, comparison, and export of GeoJSON points.
// Author: Benjamin Turnbull

const editableFileInput = document.getElementById("editableFile");
const comparisonFilesInput = document.getElementById("comparisonFiles");
const downloadButton = document.getElementById("downloadButton");
const clearComparisonsButton = document.getElementById("clearComparisonsButton");
const countrySelect = document.getElementById("countrySelect");
const addCountriesButton = document.getElementById("addCountriesButton");
const selectAllButton = document.getElementById("selectAllButton");
const clearSelectionButton = document.getElementById("clearSelectionButton");
const startBoxSelectButton = document.getElementById("startBoxSelectButton");
const fitEditableButton = document.getElementById("fitEditableButton");
const deleteSelectedButton = document.getElementById("deleteSelectedButton");
const fileStatus = document.getElementById("fileStatus");
const selectionStatus = document.getElementById("selectionStatus");
const singleEditor = document.getElementById("singleEditor");
const singleEditorStatus = document.getElementById("singleEditorStatus");
const latitudeInput = document.getElementById("latitudeInput");
const longitudeInput = document.getElementById("longitudeInput");
const propertiesInput = document.getElementById("propertiesInput");
const applySingleButton = document.getElementById("applySingleButton");
const nudgeDistanceInput = document.getElementById("nudgeDistance");
const layerList = document.getElementById("layerList");
const boxSelectHint = document.getElementById("boxSelectHint");
const vertexRenderStatus = document.getElementById("vertexRenderStatus");
const basemapStatus = document.getElementById("basemapStatus");
const countryStatus = document.getElementById("countryStatus");

const editableStyle = {
  color: "#2563eb",
  fillColor: "#60a5fa",
  fillOpacity: 0.18,
  opacity: 0.85,
  weight: 3,
};

const comparisonPalette = ["#7c3aed", "#059669", "#dc2626", "#0891b2", "#ca8a04", "#be185d"];
const MAX_VISIBLE_VERTEX_MARKERS = 2500;

let editableData = null;
let editableName = "";
let editableLayer = null;
let comparisonLayers = [];
let vertexRecords = [];
let visibleVertexRecords = [];
let selectedVertexIds = new Set();
let boxSelectActive = false;
let boxStartLatLng = null;
let boxRectangle = null;
let suppressNextMapClick = false;
let localBasemapLayer = null;

const worldBounds = L.latLngBounds([[-85.05112878, -180], [85.05112878, 180]]);
const map = L.map("map", {
  maxBounds: worldBounds.pad(0.1),
  maxBoundsViscosity: 0.7,
  minZoom: 2,
  preferCanvas: true,
  worldCopyJump: false,
}).setView([15, 0], 2);

loadLocalBasemap();
loadCountryCatalog();

async function loadLocalBasemap() {
  try {
    basemapStatus.textContent = "Loading local vector basemap…";
    const response = await fetch("/api/world-basemap.geojson");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    localBasemapLayer = L.geoJSON(data, {
      interactive: false,
      style: {
        color: "#7b8794",
        fillColor: "#f4efe6",
        fillOpacity: 1,
        opacity: 0.75,
        weight: 0.7,
      },
    }).addTo(map);
    basemapStatus.textContent = "Local vector basemap loaded.";
  } catch (error) {
    basemapStatus.textContent = `Local basemap could not load (${error.message}). GeoJSON editing still works on the blue map background.`;
  }
}

async function loadCountryCatalog() {
  try {
    countryStatus.textContent = "Loading country list…";
    const response = await fetch("/api/countries");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const countries = await response.json();
    populateCountrySelect(countries);
    countryStatus.textContent = `${countries.length.toLocaleString()} built-in countries available. Select one or more to add as read-only layers.`;
  } catch (error) {
    countryStatus.textContent = `Country list could not load (${error.message}).`;
  }
}

function populateCountrySelect(countries) {
  countrySelect.innerHTML = "";
  const groups = new Map();
  countries.forEach((country) => {
    if (!groups.has(country.group)) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = country.group;
      groups.set(country.group, optgroup);
      countrySelect.appendChild(optgroup);
    }
    const option = document.createElement("option");
    option.value = country.id;
    option.textContent = country.name;
    option.dataset.group = country.group;
    groups.get(country.group).appendChild(option);
  });
  countrySelect.disabled = countries.length === 0;
  updateCountryButtons();
}

function updateCountryButtons() {
  addCountriesButton.disabled = countrySelect.disabled || countrySelect.selectedOptions.length === 0;
}

async function addSelectedCountries() {
  const selectedOptions = Array.from(countrySelect.selectedOptions);
  if (!selectedOptions.length) {
    return;
  }

  let addedCount = 0;
  let skippedCount = 0;
  for (const option of selectedOptions) {
    const sourceId = `country:${option.value}`;
    if (comparisonLayers.some((comparison) => comparison.sourceId === sourceId)) {
      skippedCount += 1;
      continue;
    }
    try {
      countryStatus.textContent = `Loading ${option.textContent}…`;
      const response = await fetch(`/api/countries/${encodeURIComponent(option.value)}.geojson`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = normalizeGeoJson(await response.json());
      addComparisonLayer(`${option.dataset.group}: ${option.textContent}`, data, comparisonLayers.length, sourceId);
      addedCount += 1;
    } catch (error) {
      alert(`Could not load ${option.textContent}: ${error.message}`);
    }
  }

  countryStatus.textContent = `Added ${addedCount} read-only ${pluralize("country", addedCount)}${skippedCount ? `; skipped ${skippedCount} already-loaded ${pluralize("country", skippedCount)}` : ""}.`;
  clearComparisonsButton.disabled = comparisonLayers.length === 0;
  updateLayerList();
}

function makeVertexIcon(selected = false, readonly = false) {
  const classes = ["point-icon"];
  if (selected) classes.push("selected");
  if (readonly) classes.push("readonly");
  return L.divIcon({
    className: "",
    html: `<span class="${classes.join(" ")}"></span>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

function normalizeGeoJson(data) {
  if (!data || typeof data !== "object") {
    throw new Error("File did not contain a GeoJSON object.");
  }
  if (data.type === "FeatureCollection") {
    return data;
  }
  if (data.type === "Feature") {
    return { type: "FeatureCollection", features: [data] };
  }
  if (data.type && (data.coordinates || data.geometries)) {
    return {
      type: "FeatureCollection",
      features: [{ type: "Feature", properties: {}, geometry: data }],
    };
  }
  throw new Error("Only GeoJSON FeatureCollection, Feature, or Geometry objects are supported.");
}

function readFileAsGeoJson(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        resolve(normalizeGeoJson(JSON.parse(reader.result)));
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.readAsText(file);
  });
}

function coordinatesMatch(first, second) {
  return Array.isArray(first)
    && Array.isArray(second)
    && first.length >= 2
    && second.length >= 2
    && first[0] === second[0]
    && first[1] === second[1];
}

function isPosition(value) {
  return Array.isArray(value)
    && value.length >= 2
    && Number.isFinite(Number(value[0]))
    && Number.isFinite(Number(value[1]));
}

function visitCoordinateArray(coordinates, metadata, visitor) {
  const isClosedRing = metadata.closedRing
    && coordinates.length > 1
    && coordinatesMatch(coordinates[0], coordinates[coordinates.length - 1]);
  const editableLength = isClosedRing ? coordinates.length - 1 : coordinates.length;

  for (let coordinateIndex = 0; coordinateIndex < editableLength; coordinateIndex += 1) {
    const coordinatesRef = coordinates[coordinateIndex];
    if (!isPosition(coordinatesRef)) {
      continue;
    }
    visitor(coordinatesRef, {
      ...metadata,
      closedRing: isClosedRing,
      coordinateIndex,
      closingCoordinate: isClosedRing && coordinateIndex === 0
        ? coordinates[coordinates.length - 1]
        : null,
      parentCoordinates: coordinates,
    });
  }
}

function visitGeometryPositions(geometry, visitor, path = []) {
  if (!geometry) {
    return;
  }

  if (geometry.type === "Point") {
    if (isPosition(geometry.coordinates)) {
      visitor(geometry.coordinates, {
        geometryType: geometry.type,
        label: "point",
        parentCoordinates: null,
        path: [...path, "coordinates"],
      });
    }
    return;
  }

  if (geometry.type === "MultiPoint" || geometry.type === "LineString") {
    visitCoordinateArray(geometry.coordinates || [], {
      geometryType: geometry.type,
      label: geometry.type === "MultiPoint" ? "multipoint vertex" : "line vertex",
      path: [...path, "coordinates"],
      closedRing: false,
    }, visitor);
    return;
  }

  if (geometry.type === "MultiLineString" || geometry.type === "Polygon") {
    (geometry.coordinates || []).forEach((lineOrRing, lineIndex) => {
      visitCoordinateArray(lineOrRing || [], {
        geometryType: geometry.type,
        label: geometry.type === "Polygon" ? `ring ${lineIndex + 1} vertex` : `line ${lineIndex + 1} vertex`,
        path: [...path, "coordinates", lineIndex],
        closedRing: geometry.type === "Polygon",
        ringContainer: geometry.type === "Polygon" ? geometry.coordinates : null,
        ringIndex: geometry.type === "Polygon" ? lineIndex : null,
      }, visitor);
    });
    return;
  }

  if (geometry.type === "MultiPolygon") {
    (geometry.coordinates || []).forEach((polygon, polygonIndex) => {
      (polygon || []).forEach((ring, ringIndex) => {
        visitCoordinateArray(ring || [], {
          geometryType: geometry.type,
          label: `polygon ${polygonIndex + 1}, ring ${ringIndex + 1} vertex`,
          path: [...path, "coordinates", polygonIndex, ringIndex],
          closedRing: true,
          polygonContainer: geometry.coordinates,
          polygonIndex,
          ringContainer: polygon,
          ringIndex,
        }, visitor);
      });
    });
    return;
  }

  if (geometry.type === "GeometryCollection") {
    (geometry.geometries || []).forEach((childGeometry, geometryIndex) => {
      visitGeometryPositions(childGeometry, visitor, [...path, "geometries", geometryIndex]);
    });
  }
}

function addVertexRecord(feature, featureIndex, vertexIndex, coordinatesRef, metadata) {
  vertexRecords.push({
    id: `${featureIndex}:${vertexIndex}`,
    feature,
    featureIndex,
    vertexIndex,
    coordinates: coordinatesRef,
    closingCoordinate: metadata.closingCoordinate,
    coordinateIndex: metadata.coordinateIndex,
    geometryType: metadata.geometryType,
    label: metadata.label,
    marker: null,
    parentCoordinates: metadata.parentCoordinates,
    polygonContainer: metadata.polygonContainer,
    polygonIndex: metadata.polygonIndex,
    ringContainer: metadata.ringContainer,
    ringIndex: metadata.ringIndex,
    closedRing: metadata.closedRing,
  });
}

function createVertexMarker(record) {
  const marker = L.marker(getRecordLatLng(record), {
    draggable: true,
    icon: makeVertexIcon(selectedVertexIds.has(record.id)),
    title: record.feature.properties?.name || `Feature ${record.featureIndex + 1} ${record.label}`,
  });

  marker.on("click", (event) => {
    L.DomEvent.stopPropagation(event);
    if (event.originalEvent.shiftKey || event.originalEvent.metaKey || event.originalEvent.ctrlKey) {
      toggleVertex(record.id);
    } else {
      selectOnly(record.id);
    }
  });

  marker.on("dragstart", () => {
    if (!selectedVertexIds.has(record.id)) {
      selectOnly(record.id);
    }
    record.dragStart = marker.getLatLng();
    vertexRecords.forEach((otherRecord) => {
      if (selectedVertexIds.has(otherRecord.id)) {
        otherRecord.dragStartCoordinates = [otherRecord.coordinates[0], otherRecord.coordinates[1]];
        if (otherRecord.marker) {
          otherRecord.dragGroupStart = otherRecord.marker.getLatLng();
        }
      }
    });
  });

  marker.on("drag", () => {
    if (selectedVertexIds.size <= 1 || !record.dragStart) {
      return;
    }
    const current = marker.getLatLng();
    const deltaLat = current.lat - record.dragStart.lat;
    const deltaLng = current.lng - record.dragStart.lng;
    visibleVertexRecords.forEach((otherRecord) => {
      if (otherRecord.id === record.id || !selectedVertexIds.has(otherRecord.id)) {
        return;
      }
      const startLatLng = otherRecord.dragGroupStart || getRecordLatLng(otherRecord);
      otherRecord.marker.setLatLng([startLatLng.lat + deltaLat, startLatLng.lng + deltaLng]);
    });
  });

  marker.on("dragend", () => {
    const endLatLng = marker.getLatLng();
    const deltaLat = endLatLng.lat - record.dragStart.lat;
    const deltaLng = endLatLng.lng - record.dragStart.lng;
    vertexRecords.forEach((otherRecord) => {
      if (selectedVertexIds.has(otherRecord.id)) {
        const startCoordinates = otherRecord.dragStartCoordinates || [otherRecord.coordinates[0], otherRecord.coordinates[1]];
        setRecordCoordinates(otherRecord, startCoordinates[0] + deltaLng, startCoordinates[1] + deltaLat);
        otherRecord.dragGroupStart = null;
        otherRecord.dragStartCoordinates = null;
      }
    });
    record.dragStart = null;
    rebuildEditableLayer(false, false);
    updateSelectionUi();
  });

  record.marker = marker;
  return marker;
}

function collectEditableVertices() {
  vertexRecords = [];
  let vertexIndex = 0;
  editableData.features.forEach((feature, featureIndex) => {
    if (!feature.geometry) {
      return;
    }
    visitGeometryPositions(feature.geometry, (coordinates, metadata) => {
      addVertexRecord(feature, featureIndex, vertexIndex, coordinates, metadata);
      vertexIndex += 1;
    });
  });
}

function getRecordLatLng(record) {
  return [record.coordinates[1], record.coordinates[0]];
}

function setRecordCoordinates(record, longitude, latitude) {
  const extraDimensions = record.coordinates.slice(2);
  record.coordinates.splice(0, record.coordinates.length, longitude, latitude, ...extraDimensions);
  if (record.closingCoordinate) {
    const closingExtraDimensions = record.closingCoordinate.slice(2);
    record.closingCoordinate.splice(
      0,
      record.closingCoordinate.length,
      longitude,
      latitude,
      ...closingExtraDimensions,
    );
  }
}

function updateRecordCoordinatesFromMarker(record) {
  const latLng = record.marker.getLatLng();
  setRecordCoordinates(record, latLng.lng, latLng.lat);
}

function clearVisibleVertexMarkers() {
  visibleVertexRecords.forEach((record) => {
    record.marker?.removeFrom(map);
    record.marker = null;
  });
  visibleVertexRecords = [];
}

function rebuildEditableLayer(fitBounds = true, recollectVertices = true) {
  if (editableLayer) {
    map.removeLayer(editableLayer);
  }

  clearVisibleVertexMarkers();
  if (recollectVertices) {
    collectEditableVertices();
  }

  editableLayer = L.geoJSON(editableData, {
    style: editableStyle,
    pointToLayer: (feature, latLng) => L.circleMarker(latLng, {
      ...editableStyle,
      interactive: false,
      radius: 5,
    }),
  }).addTo(map);

  if (fitBounds) {
    fitEditableBounds();
  }
  renderVisibleVertexMarkers();
  updateLayerList();
}

function fitEditableBounds() {
  const group = L.featureGroup([
    ...(editableLayer ? [editableLayer] : []),
  ]);
  if (group.getLayers().length && group.getBounds().isValid()) {
    map.fitBounds(group.getBounds().pad(0.15));
  }
}

function renderVisibleVertexMarkers() {
  clearVisibleVertexMarkers();
  if (!editableData || !vertexRecords.length) {
    vertexRenderStatus.textContent = "No editable vertex handles to show.";
    return;
  }

  const bounds = map.getBounds().pad(0.08);
  let inViewCount = 0;
  for (const record of vertexRecords) {
    if (!bounds.contains(getRecordLatLng(record))) {
      continue;
    }
    inViewCount += 1;
    if (visibleVertexRecords.length >= MAX_VISIBLE_VERTEX_MARKERS) {
      continue;
    }
    record.marker = createVertexMarker(record).addTo(map);
    visibleVertexRecords.push(record);
  }

  if (inViewCount > MAX_VISIBLE_VERTEX_MARKERS) {
    vertexRenderStatus.textContent = `Showing ${MAX_VISIBLE_VERTEX_MARKERS.toLocaleString()} of ${inViewCount.toLocaleString()} visible vertices. Zoom in to edit a smaller area.`;
  } else {
    vertexRenderStatus.textContent = `Showing ${inViewCount.toLocaleString()} editable ${pluralize("vertex", inViewCount)} in the current view.`;
  }
}

function addComparisonLayer(name, data, index, sourceId = null) {
  const color = comparisonPalette[index % comparisonPalette.length];
  const layer = L.geoJSON(data, {
    style: {
      color,
      fillColor: color,
      fillOpacity: 0.1,
      opacity: 0.75,
      weight: 2,
    },
    pointToLayer: (feature, latLng) => L.marker(latLng, {
      interactive: false,
      icon: makeVertexIcon(false, true),
    }),
  }).addTo(map);

  comparisonLayers.push({ name, layer, data, color, sourceId });
  updateLayerList();
}

function updateLayerList() {
  layerList.innerHTML = "";
  if (editableData) {
    const item = document.createElement("li");
    item.innerHTML = `<strong>Editable:</strong> ${escapeHtml(editableName)}<br>${vertexRecords.length} editable ${pluralize("vertex", vertexRecords.length)}`;
    layerList.appendChild(item);
  }
  comparisonLayers.forEach((comparison) => {
    const item = document.createElement("li");
    item.innerHTML = `<strong style="color:${comparison.color}">Read-only:</strong> ${escapeHtml(comparison.name)}`;
    layerList.appendChild(item);
  });
  if (!editableData && !comparisonLayers.length) {
    const item = document.createElement("li");
    item.textContent = "No layers loaded yet.";
    layerList.appendChild(item);
  }
}

function pluralize(word, count) {
  if (word === "vertex") {
    return count === 1 ? "vertex" : "vertices";
  }
  if (word === "country") {
    return count === 1 ? "country" : "countries";
  }
  return count === 1 ? word : `${word}s`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toggleVertex(id) {
  if (selectedVertexIds.has(id)) {
    selectedVertexIds.delete(id);
  } else {
    selectedVertexIds.add(id);
  }
  updateSelectionUi();
}

function selectOnly(id) {
  selectedVertexIds = new Set([id]);
  updateSelectionUi();
}

function clearSelection() {
  selectedVertexIds.clear();
  updateSelectionUi();
}

function selectAllVertices() {
  selectedVertexIds = new Set(vertexRecords.map((record) => record.id));
  updateSelectionUi();
}

function deleteSelectedVertices() {
  const selectedRecords = getSelectedRecords();
  if (!selectedRecords.length) {
    return;
  }

  const deletionPlan = buildDeletionPlan(selectedRecords);
  if (deletionPlan.errors.length) {
    alert(deletionPlan.errors.join("\n"));
    return;
  }

  deletionPlan.groups
    .filter((group) => group.action === "spliceVertices")
    .forEach((group) => {
    const indexes = [...group.indexes].sort((left, right) => right - left);
    if (group.closedRing) {
      group.parentCoordinates.pop();
    }
    indexes.forEach((coordinateIndex) => {
      group.parentCoordinates.splice(coordinateIndex, 1);
    });
    if (group.closedRing) {
      group.parentCoordinates.push([...group.parentCoordinates[0]]);
    }
  });

  deletionPlan.groups
    .filter((group) => group.action === "removeRing")
    .sort((left, right) => right.ringIndex - left.ringIndex)
    .forEach((group) => {
      group.ringContainer.splice(group.ringIndex, 1);
    });

  deletionPlan.groups
    .filter((group) => group.action === "removePolygon")
    .sort((left, right) => right.polygonIndex - left.polygonIndex)
    .forEach((group) => {
      group.polygonContainer.splice(group.polygonIndex, 1);
    });

  deletionPlan.groups
    .filter((group) => group.action === "emptyPolygon")
    .forEach((group) => {
      group.ringContainer.splice(0, group.ringContainer.length);
    });

  selectedVertexIds.clear();
  rebuildEditableLayer(false, true);
  updateSelectionUi();
  fileStatus.textContent = `Deleted ${selectedRecords.length} selected ${pluralize("vertex", selectedRecords.length)}. Neighboring coordinates are now linked by the GeoJSON geometry.`;
}

function buildDeletionPlan(selectedRecords) {
  const groupsByParent = new Map();
  const errors = [];

  selectedRecords.forEach((record) => {
    if (!record.parentCoordinates) {
      errors.push(`Cannot delete ${record.label} from feature ${record.featureIndex + 1}; a Point geometry must keep its only coordinate.`);
      return;
    }
    if (!groupsByParent.has(record.parentCoordinates)) {
      groupsByParent.set(record.parentCoordinates, {
        closedRing: record.closedRing,
        geometryType: record.geometryType,
        indexes: new Set(),
        label: record.label,
        parentCoordinates: record.parentCoordinates,
        polygonContainer: record.polygonContainer,
        polygonIndex: record.polygonIndex,
        ringContainer: record.ringContainer,
        ringIndex: record.ringIndex,
      });
    }
    groupsByParent.get(record.parentCoordinates).indexes.add(record.coordinateIndex);
  });

  const groups = [...groupsByParent.values()];
  groups.forEach((group) => {
    const currentCount = group.closedRing
      ? group.parentCoordinates.length - 1
      : group.parentCoordinates.length;
    const remainingCount = currentCount - group.indexes.size;
    const minimumCount = minimumVerticesForGroup(group);

    if (remainingCount < minimumCount) {
      const removalAction = wholePartRemovalAction(group, currentCount);
      if (removalAction) {
        group.action = removalAction;
      } else {
        errors.push(
          `Cannot delete ${group.indexes.size} ${pluralize("vertex", group.indexes.size)} from ${vertexPartName(group.label)}; ${group.geometryType} needs at least ${minimumCount} ${pluralize("vertex", minimumCount)} in that part.`,
        );
      }
    } else {
      group.action = "spliceVertices";
    }
  });

  return { errors, groups };
}

function minimumVerticesForGroup(group) {
  if (group.closedRing || group.geometryType === "Polygon" || group.geometryType === "MultiPolygon") {
    return 3;
  }
  if (group.geometryType === "LineString" || group.geometryType === "MultiLineString") {
    return 2;
  }
  return 0;
}

function wholePartRemovalAction(group, currentCount) {
  if (!group.closedRing || !group.ringContainer) {
    return null;
  }
  if (group.ringIndex > 0) {
    return "removeRing";
  }
  if (group.indexes.size !== currentCount) {
    return null;
  }
  if (group.geometryType === "MultiPolygon" && group.polygonContainer) {
    return "removePolygon";
  }
  if (group.geometryType === "Polygon") {
    return "emptyPolygon";
  }
  return null;
}

function vertexPartName(label) {
  return label.replace(/ vertex$/, "");
}

function updateSelectionUi() {
  const selectedRecords = getSelectedRecords();
  visibleVertexRecords.forEach((record) => {
    record.marker.setIcon(makeVertexIcon(selectedVertexIds.has(record.id)));
  });

  selectionStatus.textContent = selectedRecords.length
    ? `${selectedRecords.length} of ${vertexRecords.length} ${pluralize("vertex", vertexRecords.length)} selected.`
    : "No vertices selected.";

  const hasEditableVertices = vertexRecords.length > 0;
  const hasSelection = selectedRecords.length > 0;
  selectAllButton.disabled = !hasEditableVertices;
  clearSelectionButton.disabled = !hasSelection;
  startBoxSelectButton.disabled = !hasEditableVertices || boxSelectActive;
  fitEditableButton.disabled = !editableData;
  deleteSelectedButton.disabled = !hasSelection;
  document.querySelectorAll("[data-nudge]").forEach((button) => {
    button.disabled = !hasSelection;
  });

  updateSingleEditor(selectedRecords);
}

function getSelectedRecords() {
  return vertexRecords.filter((record) => selectedVertexIds.has(record.id));
}

function updateSingleEditor(selectedRecords) {
  const editable = selectedRecords.length === 1;
  singleEditor.classList.toggle("disabled", !editable);
  latitudeInput.disabled = !editable;
  longitudeInput.disabled = !editable;
  propertiesInput.disabled = !editable;
  applySingleButton.disabled = !editable;

  if (!editable) {
    latitudeInput.value = "";
    longitudeInput.value = "";
    propertiesInput.value = "";
    singleEditorStatus.textContent = selectedRecords.length > 1
      ? "Multiple vertices selected. Drag or nudge to move them together."
      : "Select exactly one vertex to edit coordinates and feature properties.";
    return;
  }

  const record = selectedRecords[0];
  const latLng = record.marker
    ? record.marker.getLatLng()
    : { lat: record.coordinates[1], lng: record.coordinates[0] };
  latitudeInput.value = latLng.lat.toFixed(8);
  longitudeInput.value = latLng.lng.toFixed(8);
  propertiesInput.value = JSON.stringify(record.feature.properties || {}, null, 2);
  singleEditorStatus.textContent = `Editing feature ${record.featureIndex + 1}, ${record.label} ${record.vertexIndex + 1}.`;
}

function nudgeSelected(direction) {
  const distance = Number(nudgeDistanceInput.value);
  if (!Number.isFinite(distance) || distance <= 0) {
    alert("Enter a positive nudge distance.");
    return;
  }

  const delta = {
    north: [distance, 0],
    south: [-distance, 0],
    east: [0, distance],
    west: [0, -distance],
  }[direction];

  getSelectedRecords().forEach((record) => {
    const nextLatitude = record.coordinates[1] + delta[0];
    const nextLongitude = record.coordinates[0] + delta[1];
    setRecordCoordinates(record, nextLongitude, nextLatitude);
    record.marker?.setLatLng([nextLatitude, nextLongitude]);
  });

  rebuildEditableLayer(false, false);
  updateSelectionUi();
}

function applySingleVertexChanges() {
  const selectedRecords = getSelectedRecords();
  if (selectedRecords.length !== 1) {
    return;
  }

  const latitude = Number(latitudeInput.value);
  const longitude = Number(longitudeInput.value);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
    alert("Latitude and longitude must be valid numbers.");
    return;
  }
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
    alert("Latitude must be between -90 and 90; longitude must be between -180 and 180.");
    return;
  }

  let properties;
  try {
    properties = JSON.parse(propertiesInput.value || "{}");
  } catch (error) {
    alert(`Properties must be valid JSON: ${error.message}`);
    return;
  }
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
    alert("Properties JSON must be an object.");
    return;
  }

  const record = selectedRecords[0];
  record.feature.properties = properties;
  setRecordCoordinates(record, longitude, latitude);
  record.marker?.setLatLng([latitude, longitude]);
  rebuildEditableLayer(false, false);
  updateSelectionUi();
}

function startBoxSelect() {
  if (!vertexRecords.length) {
    return;
  }
  boxSelectActive = true;
  boxStartLatLng = null;
  map.dragging.disable();
  boxSelectHint.classList.remove("hidden");
  startBoxSelectButton.textContent = "Box Select Active";
  updateSelectionUi();
}

function stopBoxSelect() {
  boxSelectActive = false;
  boxStartLatLng = null;
  map.dragging.enable();
  boxSelectHint.classList.add("hidden");
  startBoxSelectButton.textContent = "Box Select";
  if (boxRectangle) {
    map.removeLayer(boxRectangle);
    boxRectangle = null;
  }
  updateSelectionUi();
}

function downloadEditedGeoJson() {
  if (!editableData) {
    return;
  }
  const blob = new Blob([JSON.stringify(editableData, null, 2)], { type: "application/geo+json" });
  const link = document.createElement("a");
  const baseName = editableName.replace(/\.(geo)?json$/i, "") || "edited";
  link.href = URL.createObjectURL(blob);
  link.download = `${baseName}.edited.geojson`;
  document.body.appendChild(link);
  link.click();
  URL.revokeObjectURL(link.href);
  link.remove();
}

function clearComparisons() {
  comparisonLayers.forEach((comparison) => map.removeLayer(comparison.layer));
  comparisonLayers = [];
  comparisonFilesInput.value = "";
  Array.from(countrySelect.options).forEach((option) => {
    option.selected = false;
  });
  clearComparisonsButton.disabled = true;
  updateCountryButtons();
  updateLayerList();
}

editableFileInput.addEventListener("change", async () => {
  const file = editableFileInput.files[0];
  if (!file) {
    return;
  }
  try {
    editableData = await readFileAsGeoJson(file);
    editableName = file.name;
    selectedVertexIds.clear();
    rebuildEditableLayer(true);
    downloadButton.disabled = false;
    fileStatus.textContent = `Loaded ${file.name}. ${vertexRecords.length} editable ${pluralize("vertex", vertexRecords.length)} found.`;
    updateSelectionUi();
  } catch (error) {
    fileStatus.textContent = `Could not load ${file.name}: ${error.message}`;
    alert(fileStatus.textContent);
  }
});

comparisonFilesInput.addEventListener("change", async () => {
  const files = Array.from(comparisonFilesInput.files);
  for (const file of files) {
    try {
      const data = await readFileAsGeoJson(file);
      addComparisonLayer(file.name, data, comparisonLayers.length);
    } catch (error) {
      alert(`Could not load comparison ${file.name}: ${error.message}`);
    }
  }
  clearComparisonsButton.disabled = comparisonLayers.length === 0;
});

downloadButton.addEventListener("click", downloadEditedGeoJson);
clearComparisonsButton.addEventListener("click", clearComparisons);
countrySelect.addEventListener("change", updateCountryButtons);
addCountriesButton.addEventListener("click", addSelectedCountries);
selectAllButton.addEventListener("click", selectAllVertices);
clearSelectionButton.addEventListener("click", clearSelection);
startBoxSelectButton.addEventListener("click", startBoxSelect);
fitEditableButton.addEventListener("click", fitEditableBounds);
deleteSelectedButton.addEventListener("click", deleteSelectedVertices);
applySingleButton.addEventListener("click", applySingleVertexChanges);

document.querySelectorAll("[data-nudge]").forEach((button) => {
  button.addEventListener("click", () => nudgeSelected(button.dataset.nudge));
});

map.on("click", () => {
  if (suppressNextMapClick) {
    suppressNextMapClick = false;
    return;
  }
  if (!boxSelectActive) {
    clearSelection();
  }
});

map.on("mousedown", (event) => {
  if (!boxSelectActive) {
    return;
  }
  boxStartLatLng = event.latlng;
  if (boxRectangle) {
    map.removeLayer(boxRectangle);
  }
  boxRectangle = L.rectangle([boxStartLatLng, boxStartLatLng], {
    color: "#f97316",
    weight: 2,
    dashArray: "4 4",
    fillOpacity: 0.08,
  }).addTo(map);
});

map.on("mousemove", (event) => {
  if (!boxSelectActive || !boxStartLatLng || !boxRectangle) {
    return;
  }
  boxRectangle.setBounds(L.latLngBounds(boxStartLatLng, event.latlng));
});

map.on("mouseup", () => {
  if (!boxSelectActive || !boxRectangle) {
    return;
  }
  const bounds = boxRectangle.getBounds();
  selectedVertexIds = new Set(
    vertexRecords
      .filter((record) => bounds.contains(getRecordLatLng(record)))
      .map((record) => record.id)
  );
  suppressNextMapClick = true;
  stopBoxSelect();
});

map.on("moveend", renderVisibleVertexMarkers);
map.on("zoomend", renderVisibleVertexMarkers);

updateLayerList();
updateSelectionUi();
