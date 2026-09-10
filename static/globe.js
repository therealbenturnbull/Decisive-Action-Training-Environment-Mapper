// Aim: Render and control the interactive canvas globe and its optional data layers.
// Author: Benjamin Turnbull

const canvas = document.getElementById("globeCanvas");
const ctx = canvas.getContext("2d");
const globeStage = canvas.closest(".globe-stage");
const countryCount = document.getElementById("countryCount");
const pauseButton = document.getElementById("pauseButton");
const zoomInButton = document.getElementById("zoomInButton");
const zoomOutButton = document.getElementById("zoomOutButton");
const capitalList = document.getElementById("capitalList");
const capitalSearch = document.getElementById("capitalSearch");
const clearCapitalsButton = document.getElementById("clearCapitalsButton");
const dateCapitalList = document.getElementById("dateCapitalList");
const dateCapitalSearch = document.getElementById("dateCapitalSearch");
const clearDateCapitalsButton = document.getElementById("clearDateCapitalsButton");
const countryList = document.getElementById("countryList");
const countrySearch = document.getElementById("countrySearch");
const clearCountriesButton = document.getElementById("clearCountriesButton");
const showUnderseaCablesToggle = document.getElementById("showUnderseaCablesToggle");
const underseaCableCount = document.getElementById("underseaCableCount");
const showUserMarkersToggle = document.getElementById("showUserMarkersToggle");
const userMarkerCount = document.getElementById("userMarkerCount");

const COUNTRY_LINE_WIDTH_SCALE = 1.5;
const COUNTRY_SEAM_OVERPAINT_WIDTH = 2.6;
const COUNTRY_HIGHLIGHT_SEAM_OVERPAINT_WIDTH = 1.8;
const GLOBE_FRAME_INTERVAL = 1000 / 30;
const GLOBE_MIN_ZOOM = 0.68;
const GLOBE_MAX_ZOOM = 1.7;
const GLOBE_ZOOM_STEP = 1.14;
const HORIZON_EPSILON = 1e-9;
const HORIZON_ARC_STEP = Math.PI / 180;
const MAX_CANVAS_PIXELS = 4_000_000;
const TWO_PI = Math.PI * 2;
const WORLD_CAPITAL_DATASET = "WORLD";
const DATE_CAPITAL_DATASET = "DATE";

let frameProjectionCache = new WeakMap();
let globeBaseSize = 0;
const activePointers = new Map();

const state = {
  countries: [],
  capitals: [],
  cableLines: [],
  userMarkers: [],
  markerImages: new Map(),
  showUserMarkers: false,
  selectedCountryIds: new Set(),
  selectedCapitalIds: new Set(),
  showUnderseaCables: false,
  underseaCablesLoaded: false,
  countryQuery: "",
  capitalQuery: "",
  dateCapitalQuery: "",
  rotation: -0.45,
  tilt: -0.28,
  zoom: 1,
  spin: true,
  dragging: false,
  pinching: false,
  pinchStartDistance: 0,
  pinchStartZoom: 1,
  dragStartX: 0,
  dragStartY: 0,
  dragStartRotation: 0,
  dragStartTilt: 0,
  dragDistance: 0,
  hoverX: null,
  hoverY: null,
  hoverCountry: null,
  hoverDirty: false,
  touchLabelPinned: false,
  lastHoverCheck: 0,
  lastFrame: performance.now(),
  colourScheme: window.DateMapperColourSchemes.normalizeId(
    window.DateMapperSettings.get().colourScheme
  ),
};

function currentPalette() {
  return window.DateMapperColourSchemes.get(state.colourScheme).globe;
}

function countryFillStyle(country) {
  return window.DateMapperColourSchemes.countryFill(
    state.colourScheme,
    country,
    "globe"
  );
}

function addGradientStops(gradient, stops) {
  for (const [offset, colour] of stops) {
    gradient.addColorStop(offset, colour);
  }
  return gradient;
}

function createMetallicGradient(stops, radius, centerX, centerY) {
  const gradient = ctx.createLinearGradient(
    centerX - radius * 0.76,
    centerY - radius * 0.82,
    centerX + radius * 0.72,
    centerY + radius * 0.78
  );
  return addGradientStops(gradient, stops);
}

function setColourScheme(colourScheme) {
  state.colourScheme = window.DateMapperColourSchemes.normalizeId(colourScheme);
  document.body.dataset.colourScheme = state.colourScheme;
  drawGlobe();
}

function updateZoomControls() {
  zoomOutButton.disabled = state.zoom <= GLOBE_MIN_ZOOM + 0.001;
  zoomInButton.disabled = state.zoom >= GLOBE_MAX_ZOOM - 0.001;
  globeStage.style.setProperty("--globe-zoom", state.zoom.toFixed(3));
}

function setZoom(zoom) {
  const nextZoom = Math.max(GLOBE_MIN_ZOOM, Math.min(GLOBE_MAX_ZOOM, zoom));
  if (Math.abs(nextZoom - state.zoom) < 0.001) return;
  state.zoom = nextZoom;
  state.hoverDirty = true;
  updateZoomControls();
  drawGlobe();
}

function resizeCanvas() {
  const stageStyle = getComputedStyle(globeStage);
  const horizontalPadding = parseFloat(stageStyle.paddingLeft)
    + parseFloat(stageStyle.paddingRight);
  const verticalPadding = parseFloat(stageStyle.paddingTop)
    + parseFloat(stageStyle.paddingBottom);
  const availableWidth = Math.max(160, globeStage.clientWidth - horizontalPadding);
  const availableHeight = Math.max(160, globeStage.clientHeight - verticalPadding);
  // Full devicePixelRatio looked sharper on a first pass, but large Retina
  // canvases caused frame drops when several detailed layers were visible.
  // const dpr = window.devicePixelRatio || 1;
  const requestedDpr = Math.min(window.devicePixelRatio || 1, 1.5);
  const pixelBudgetDpr = Math.sqrt(
    MAX_CANVAS_PIXELS / (availableWidth * availableHeight)
  );
  const dpr = Math.max(0.75, Math.min(requestedDpr, pixelBudgetDpr));

  globeBaseSize = Math.min(availableWidth, availableHeight, 860);
  globeStage.style.setProperty("--globe-size", `${globeBaseSize}px`);
  canvas.style.width = `${availableWidth}px`;
  canvas.style.height = `${availableHeight}px`;
  canvas.width = Math.floor(availableWidth * dpr);
  canvas.height = Math.floor(availableHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  updateZoomControls();
}

function canvasPointFromEvent(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

function radians(value) {
  return value * Math.PI / 180;
}

function viewVector(position) {
  const longitude = radians(position[0]) + state.rotation;
  const latitude = radians(position[1]);
  const cosLatitude = Math.cos(latitude);

  const x = cosLatitude * Math.sin(longitude);
  const y = Math.sin(latitude);
  const z = cosLatitude * Math.cos(longitude);

  const tiltedY = y * Math.cos(state.tilt) - z * Math.sin(state.tilt);
  const tiltedZ = y * Math.sin(state.tilt) + z * Math.cos(state.tilt);

  return {
    x,
    y: tiltedY,
    z: tiltedZ,
  };
}

function projectedViewPoint(vector, radius, centerX, centerY) {
  return {
    x: centerX + radius * vector.x,
    y: centerY - radius * vector.y,
    z: vector.z,
    visible: vector.z >= 0,
  };
}

function project(position, radius, centerX, centerY) {
  return projectedViewPoint(viewVector(position), radius, centerX, centerY);
}

function walkGeometry(geometry, onLine) {
  if (!geometry) return;

  if (geometry.type === "LineString") {
    onLine(geometry.coordinates);
    return;
  }

  if (geometry.type === "MultiLineString" || geometry.type === "Polygon") {
    geometry.coordinates.forEach(onLine);
    return;
  }

  if (geometry.type === "MultiPolygon") {
    geometry.coordinates.forEach((polygon) => polygon.forEach(onLine));
    return;
  }

  if (geometry.type === "GeometryCollection") {
    geometry.geometries.forEach((item) => walkGeometry(item, onLine));
  }
}

function sameCoordinate(first, second) {
  return Boolean(first && second)
    && Math.abs(first[0] - second[0]) < HORIZON_EPSILON
    && Math.abs(first[1] - second[1]) < HORIZON_EPSILON;
}

function sameProjectedPoint(first, second) {
  return Boolean(first && second)
    && Math.abs(first.x - second.x) < HORIZON_EPSILON
    && Math.abs(first.y - second.y) < HORIZON_EPSILON;
}

function appendProjectedPoint(segment, point) {
  if (!sameProjectedPoint(segment[segment.length - 1], point)) {
    segment.push(point);
  }
}

function horizonIntersection(first, second, radius, centerX, centerY) {
  const denominator = first.z - second.z;
  const amount = Math.abs(denominator) < HORIZON_EPSILON
    ? 0.5
    : first.z / denominator;
  const x = first.x + (second.x - first.x) * amount - centerX;
  const y = first.y + (second.y - first.y) * amount - centerY;
  const length = Math.hypot(x, y);

  if (length < HORIZON_EPSILON) {
    return {
      x: centerX + radius,
      y: centerY,
      z: 0,
      visible: true,
    };
  }

  return {
    x: centerX + radius * (x / length),
    y: centerY + radius * (y / length),
    z: 0,
    visible: true,
  };
}

function buildProjectedPath(line, radius, centerX, centerY, closed) {
  const segments = [];
  let pointCount = line.length;

  if (closed && pointCount > 1 && sameCoordinate(line[0], line[pointCount - 1])) {
    pointCount -= 1;
  }

  if (!pointCount) {
    return { segments, clipped: false };
  }

  const points = line.slice(0, pointCount).map((position) => (
    projectedViewPoint(viewVector(position), radius, centerX, centerY)
  ));
  const clipped = points.some((point) => !point.visible);
  const edgeCount = closed ? pointCount : Math.max(0, pointCount - 1);
  let segment = [];

  if (!edgeCount) {
    if (points[0].visible) {
      segments.push([points[0]]);
    }
    return { segments, clipped };
  }

  for (let index = 0; index < edgeCount; index += 1) {
    const point = points[index];
    const nextPoint = points[(index + 1) % pointCount];

    if (point.visible && !segment.length) {
      appendProjectedPoint(segment, point);
    }

    if (point.visible && nextPoint.visible) {
      appendProjectedPoint(segment, nextPoint);
      continue;
    }

    if (point.visible && !nextPoint.visible) {
      appendProjectedPoint(
        segment,
        horizonIntersection(point, nextPoint, radius, centerX, centerY)
      );
      if (segment.length) {
        segments.push(segment);
        segment = [];
      }
      continue;
    }

    if (!point.visible && nextPoint.visible) {
      appendProjectedPoint(
        segment,
        horizonIntersection(point, nextPoint, radius, centerX, centerY)
      );
      appendProjectedPoint(segment, nextPoint);
    }
  }

  if (segment.length) {
    segments.push(segment);
  }

  if (closed && segments.length > 1) {
    const firstSegment = segments[0];
    const lastSegment = segments[segments.length - 1];
    if (sameProjectedPoint(lastSegment[lastSegment.length - 1], firstSegment[0])) {
      const mergedSegment = lastSegment.concat(firstSegment.slice(1));
      segments.splice(0, 1, mergedSegment);
      segments.pop();
    }
  }

  return { segments, clipped };
}

function projectedPath(line, radius, centerX, centerY, closed = false) {
  let cachedPaths = frameProjectionCache.get(line);
  if (!cachedPaths) {
    cachedPaths = {};
    frameProjectionCache.set(line, cachedPaths);
  }

  const cacheKey = closed ? "closed" : "open";
  if (!cachedPaths[cacheKey]) {
    cachedPaths[cacheKey] = buildProjectedPath(
      line,
      radius,
      centerX,
      centerY,
      closed
    );
  }

  return cachedPaths[cacheKey];
}

function drawProjectedLine(line, radius, centerX, centerY) {
  drawProjectedSegments(line, radius, centerX, centerY);
}

function drawProjectedSegments(line, radius, centerX, centerY) {
  for (const segment of projectedPath(line, radius, centerX, centerY).segments) {
    if (!segment.length) {
      continue;
    }

    ctx.moveTo(segment[0].x, segment[0].y);
    for (const point of segment.slice(1)) {
      ctx.lineTo(point.x, point.y);
    }
  }
}

function walkPolygonRings(geometry, onRing) {
  if (!geometry) return;

  if (geometry.type === "Polygon") {
    geometry.coordinates.forEach(onRing);
    return;
  }

  if (geometry.type === "MultiPolygon") {
    geometry.coordinates.forEach((polygon) => polygon.forEach(onRing));
    return;
  }

  if (geometry.type === "GeometryCollection") {
    geometry.geometries.forEach((item) => walkPolygonRings(item, onRing));
  }
}

function walkPolygons(geometry, onPolygon) {
  if (!geometry) return;

  if (geometry.type === "Polygon") {
    onPolygon(geometry.coordinates);
    return;
  }

  if (geometry.type === "MultiPolygon") {
    geometry.coordinates.forEach(onPolygon);
    return;
  }

  if (geometry.type === "GeometryCollection") {
    geometry.geometries.forEach((item) => walkPolygons(item, onPolygon));
  }
}

function flattenedGeometryLines(geometry) {
  const lines = [];
  walkGeometry(geometry, (line) => lines.push(line));
  return lines;
}

function flattenedGeometryRings(geometry) {
  const rings = [];
  walkPolygonRings(geometry, (ring) => rings.push(ring));
  return rings;
}

function prepareCountryForRendering(country) {
  const lines = [];
  const rings = [];
  const polygons = [];

  for (const geometry of country.geometries || []) {
    lines.push(...flattenedGeometryLines(geometry));
    rings.push(...flattenedGeometryRings(geometry));
    walkPolygons(geometry, (polygon) => polygons.push(polygon));
  }

  return { ...country, lines, rings, polygons };
}

function signedPathArea(points) {
  let area = 0;

  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    const nextPoint = points[(index + 1) % points.length];
    area += point.x * nextPoint.y - nextPoint.x * point.y;
  }

  return area / 2;
}

function horizonArcPoints(from, delta, radius, centerX, centerY) {
  const startAngle = Math.atan2(from.y - centerY, from.x - centerX);
  const steps = Math.max(1, Math.ceil(Math.abs(delta) / HORIZON_ARC_STEP));
  const points = [];

  for (let step = 1; step < steps; step += 1) {
    const angle = startAngle + delta * (step / steps);
    points.push({
      x: centerX + radius * Math.cos(angle),
      y: centerY + radius * Math.sin(angle),
      z: 0,
      visible: true,
    });
  }

  return points;
}

function closeProjectedSegmentAtHorizon(segment, radius, centerX, centerY) {
  if (segment.length < 2) {
    return segment;
  }

  const first = segment[0];
  const last = segment[segment.length - 1];
  const fromAngle = Math.atan2(last.y - centerY, last.x - centerX);
  const toAngle = Math.atan2(first.y - centerY, first.x - centerX);
  const positiveDelta = (toAngle - fromAngle + TWO_PI) % TWO_PI;

  if (positiveDelta < HORIZON_EPSILON || TWO_PI - positiveDelta < HORIZON_EPSILON) {
    return segment;
  }

  const candidates = [positiveDelta, positiveDelta - TWO_PI].map((delta) => {
    const points = segment.concat(
      horizonArcPoints(last, delta, radius, centerX, centerY)
    );
    return {
      points,
      area: Math.abs(signedPathArea(points)),
      arcLength: Math.abs(delta),
    };
  });

  candidates.sort((firstCandidate, secondCandidate) => (
    firstCandidate.area - secondCandidate.area
    || firstCandidate.arcLength - secondCandidate.arcLength
  ));
  return candidates[0].points;
}

function projectedRingPaths(line, radius, centerX, centerY) {
  const path = projectedPath(line, radius, centerX, centerY, true);
  if (!path.clipped) {
    return path.segments;
  }

  return path.segments.map((segment) => (
    closeProjectedSegmentAtHorizon(segment, radius, centerX, centerY)
  ));
}

function traceProjectedRing(line, radius, centerX, centerY) {
  let traced = false;

  for (const segment of projectedRingPaths(line, radius, centerX, centerY)) {
    if (segment.length < 3) {
      continue;
    }

    ctx.moveTo(segment[0].x, segment[0].y);
    for (const point of segment.slice(1)) {
      ctx.lineTo(point.x, point.y);
    }
    ctx.closePath();
    traced = true;
  }

  return traced;
}

function drawCountryRingSegments(country, radius, centerX, centerY, options = {}) {
  const shouldFill = options.fill !== false;
  const shouldStroke = options.stroke !== false;
  const polygons = country.polygons.length
    ? country.polygons
    : country.rings.map((ring) => [ring]);

  for (const polygon of polygons) {
    ctx.beginPath();
    let traced = false;
    for (const ring of polygon) {
      traced = traceProjectedRing(ring, radius, centerX, centerY) || traced;
    }

    if (!traced) {
      continue;
    }

    if (shouldFill) ctx.fill("evenodd");
    if (shouldStroke) ctx.stroke();
  }
}

function strokeCountryLines(country, radius, centerX, centerY) {
  for (const line of country.lines) {
    drawProjectedSegments(line, radius, centerX, centerY);
  }
}

function pointInProjectedPath(x, y, path) {
  let inside = false;

  for (let index = 0, previousIndex = path.length - 1;
    index < path.length;
    previousIndex = index, index += 1) {
    const point = path[index];
    const previousPoint = path[previousIndex];
    const intersects = ((point.y > y) !== (previousPoint.y > y))
      && (x < ((previousPoint.x - point.x) * (y - point.y))
        / (previousPoint.y - point.y) + point.x);

    if (intersects) {
      inside = !inside;
    }
  }

  return inside;
}

function pointInProjectedPolygon(x, y, polygon, radius, centerX, centerY) {
  let inside = false;

  for (const ring of polygon) {
    for (const path of projectedRingPaths(ring, radius, centerX, centerY)) {
      if (path.length < 3) {
        continue;
      }

      if (pointInProjectedPath(x, y, path)) {
        inside = !inside;
      }
    }
  }

  return inside;
}

function pointInProjectedCountry(x, y, country, radius, centerX, centerY) {
  const polygons = country.polygons.length
    ? country.polygons
    : country.rings.map((ring) => [ring]);

  for (const polygon of polygons) {
    if (pointInProjectedPolygon(x, y, polygon, radius, centerX, centerY)) {
      return true;
    }
  }

  return false;
}

function drawGraticule(radius, centerX, centerY) {
  const palette = currentPalette();
  ctx.save();
  ctx.strokeStyle = palette.graticule;
  ctx.lineWidth = 1;

  for (let latitude = -60; latitude <= 60; latitude += 30) {
    ctx.beginPath();
    const line = [];
    for (let longitude = -180; longitude <= 180; longitude += 4) {
      line.push([longitude, latitude]);
    }
    drawProjectedLine(line, radius, centerX, centerY);
    ctx.stroke();
  }

  for (let longitude = -180; longitude < 180; longitude += 30) {
    ctx.beginPath();
    const line = [];
    for (let latitude = -88; latitude <= 88; latitude += 4) {
      line.push([longitude, latitude]);
    }
    drawProjectedLine(line, radius, centerX, centerY);
    ctx.stroke();
  }

  ctx.restore();
}

function drawCountries(radius, centerX, centerY) {
  const palette = currentPalette();
  const worldMetal = palette.countryGradientStops
    ? createMetallicGradient(palette.countryGradientStops, radius, centerX, centerY)
    : null;
  const dateMetal = palette.dateCountryGradientStops
    ? createMetallicGradient(palette.dateCountryGradientStops, radius, centerX, centerY)
    : null;
  ctx.save();
  ctx.lineJoin = "round";
  ctx.lineCap = "round";

  if (palette.countryFill) {
    for (const country of state.countries) {
      ctx.fillStyle = Boolean(country.is_date_dataset) && dateMetal
        ? dateMetal
        : worldMetal || countryFillStyle(country);
      ctx.strokeStyle = ctx.fillStyle;
      ctx.lineWidth = (palette.countryLineWidth * COUNTRY_LINE_WIDTH_SCALE) + COUNTRY_SEAM_OVERPAINT_WIDTH;
      drawCountryRingSegments(country, radius, centerX, centerY);
    }
  }

  for (const isDateDataset of [false, true]) {
    ctx.beginPath();
    ctx.strokeStyle = isDateDataset && palette.dateCountryStroke
      ? palette.dateCountryStroke
      : palette.countryStroke;
    ctx.lineWidth = palette.countryLineWidth * COUNTRY_LINE_WIDTH_SCALE;
    for (const country of state.countries) {
      if (Boolean(country.is_date_dataset) !== isDateDataset) continue;
      strokeCountryLines(country, radius, centerX, centerY);
    }
    ctx.stroke();
  }

  ctx.restore();
}

function drawUnderseaCables(radius, centerX, centerY) {
  if (!state.showUnderseaCables || !state.cableLines.length) {
    return;
  }

  const palette = currentPalette();
  ctx.save();
  ctx.beginPath();
  ctx.strokeStyle = palette.cableStroke;
  ctx.lineWidth = 1.25;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  for (const line of state.cableLines) {
    drawProjectedSegments(line, radius, centerX, centerY);
  }
  ctx.stroke();
  ctx.restore();
}

function selectedCapitals() {
  return state.capitals.filter((capital) => state.selectedCapitalIds.has(capital.id));
}

function selectedCountries() {
  return state.countries.filter((country) => state.selectedCountryIds.has(country.id));
}

function loadMarkerImage(marker) {
  if (!marker.icon_url || state.markerImages.has(marker.id)) {
    return;
  }

  const image = new Image();
  image.decoding = "async";
  image.onload = () => drawGlobe();
  image.onerror = () => {
    state.markerImages.set(marker.id, null);
  };
  state.markerImages.set(marker.id, image);
  image.src = marker.icon_url;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char]));
}

function drawCountryHighlights(radius, centerX, centerY) {
  const countries = selectedCountries();
  if (!countries.length) {
    return;
  }

  ctx.save();
  ctx.fillStyle = "rgba(255, 230, 109, 0.36)";
  ctx.strokeStyle = "rgba(255, 230, 109, 0.98)";
  ctx.lineWidth = 2.2 * COUNTRY_LINE_WIDTH_SCALE;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.shadowColor = "rgba(255, 230, 109, 0.42)";
  ctx.shadowBlur = 10;

  for (const country of countries) {
    ctx.strokeStyle = ctx.fillStyle;
    ctx.lineWidth = (2.2 * COUNTRY_LINE_WIDTH_SCALE) + COUNTRY_HIGHLIGHT_SEAM_OVERPAINT_WIDTH;
    drawCountryRingSegments(country, radius, centerX, centerY);
  }

  ctx.beginPath();
  ctx.strokeStyle = "rgba(255, 230, 109, 0.98)";
  ctx.lineWidth = 2.2 * COUNTRY_LINE_WIDTH_SCALE;
  for (const country of countries) {
    strokeCountryLines(country, radius, centerX, centerY);
  }
  ctx.stroke();

  ctx.restore();
}

function drawUserMarkers(radius, centerX, centerY) {
  if (!state.showUserMarkers || !state.userMarkers.length) {
    return;
  }

  ctx.save();
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.font = "700 12px Inter, system-ui, sans-serif";

  for (const marker of state.userMarkers) {
    const point = project([marker.longitude, marker.latitude], radius, centerX, centerY);
    if (!point.visible) {
      continue;
    }

    const image = state.markerImages.get(marker.id);
    const hasImage = image && image.complete && image.naturalWidth > 0;
    const size = hasImage ? 28 : 11;

    ctx.shadowColor = "rgba(255, 230, 109, 0.34)";
    ctx.shadowBlur = 12;

    if (hasImage) {
      ctx.save();
      ctx.beginPath();
      ctx.arc(point.x, point.y, size / 2 + 3, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255, 255, 255, 0.94)";
      ctx.fill();
      ctx.clip();
      ctx.drawImage(image, point.x - size / 2, point.y - size / 2, size, size);
      ctx.restore();

      ctx.shadowBlur = 0;
      ctx.beginPath();
      ctx.arc(point.x, point.y, size / 2 + 3, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255, 230, 109, 0.92)";
      ctx.lineWidth = 2;
      ctx.stroke();
    } else {
      ctx.beginPath();
      ctx.fillStyle = "#ffe66d";
      ctx.strokeStyle = "#050505";
      ctx.lineWidth = 2;
      ctx.arc(point.x, point.y, size / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }

    if (marker.name) {
      ctx.shadowBlur = 0;
      ctx.fillStyle = "rgba(255,230,109,0.96)";
      ctx.fillText(marker.name, point.x, point.y - size / 2 - 8);
    }
  }

  ctx.restore();
}

function drawCapitalHighlights(radius, centerX, centerY) {
  const capitals = selectedCapitals();
  if (!capitals.length) {
    return;
  }

  ctx.save();
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.font = "600 12px Inter, system-ui, sans-serif";

  for (const capital of capitals) {
    const point = project([capital.longitude, capital.latitude], radius, centerX, centerY);
    if (!point.visible) {
      continue;
    }

    const pulse = 1 + Math.sin(performance.now() * 0.004) * 0.18;
    const isDateCapital = capital.dataset === DATE_CAPITAL_DATASET;
    const markerColour = isDateCapital ? "#5ce1e6" : "#ffe66d";
    const glow = ctx.createRadialGradient(point.x, point.y, 0, point.x, point.y, 26 * pulse);
    glow.addColorStop(0, isDateCapital ? "rgba(92,225,230,0.95)" : "rgba(255,230,109,0.95)");
    glow.addColorStop(0.28, isDateCapital ? "rgba(92,225,230,0.42)" : "rgba(255,230,109,0.42)");
    glow.addColorStop(1, isDateCapital ? "rgba(92,225,230,0)" : "rgba(255,230,109,0)");

    ctx.beginPath();
    ctx.fillStyle = glow;
    ctx.arc(point.x, point.y, 26 * pulse, 0, Math.PI * 2);
    ctx.fill();

    ctx.beginPath();
    ctx.fillStyle = markerColour;
    ctx.strokeStyle = "#050505";
    ctx.lineWidth = 2;
    ctx.arc(point.x, point.y, 5.5 * pulse, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = markerColour;
    ctx.fillText(capital.name, point.x, point.y - 13);
  }

  ctx.restore();
}

function findCountryAtPoint(x, y, radius, centerX, centerY) {
  for (let countryIndex = state.countries.length - 1; countryIndex >= 0; countryIndex -= 1) {
    const country = state.countries[countryIndex];

    if (pointInProjectedCountry(x, y, country, radius, centerX, centerY)) {
      return country;
    }
  }

  return null;
}

function updateHoverCountry(radius, centerX, centerY) {
  if (state.hoverX === null || state.hoverY === null || state.dragging) {
    state.hoverCountry = null;
    state.hoverDirty = false;
    return;
  }

  const now = performance.now();
  if (!state.hoverDirty && now - state.lastHoverCheck < 180) {
    return;
  }

  state.lastHoverCheck = now;
  state.hoverDirty = false;
  state.hoverCountry = findCountryAtPoint(state.hoverX, state.hoverY, radius, centerX, centerY);
}

function drawHoverCountryLabel(width, height) {
  if (!state.hoverCountry || state.hoverX === null || state.hoverY === null) {
    return;
  }

  const palette = currentPalette();
  const label = state.hoverCountry.name;
  const paddingX = 12;
  const paddingY = 8;

  ctx.save();
  ctx.font = "700 13px Inter, system-ui, sans-serif";
  const textWidth = ctx.measureText(label).width;
  const boxWidth = textWidth + paddingX * 2;
  const boxHeight = 32;
  const x = Math.min(width - boxWidth - 8, Math.max(8, state.hoverX + 14));
  const y = Math.min(height - boxHeight - 8, Math.max(8, state.hoverY - 44));

  ctx.shadowColor = "rgba(255, 230, 109, 0.25)";
  ctx.shadowBlur = 12;
  ctx.fillStyle = palette.hoverFill;
  ctx.strokeStyle = palette.hoverStroke;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(x, y, boxWidth, boxHeight, 999);
  ctx.fill();
  ctx.stroke();

  ctx.shadowBlur = 0;
  ctx.fillStyle = palette.hoverText;
  ctx.textBaseline = "middle";
  ctx.fillText(label, x + paddingX, y + boxHeight / 2);
  ctx.restore();
}

function drawGlobe() {
  frameProjectionCache = new WeakMap();
  const palette = currentPalette();
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const radius = (globeBaseSize || Math.min(width, height, 860)) * 0.44 * state.zoom;
  const centerX = width / 2;
  const centerY = height / 2;
  updateHoverCountry(radius, centerX, centerY);

  ctx.clearRect(0, 0, width, height);

  let gradient;
  if (palette.globeGradientStops) {
    gradient = createMetallicGradient(
      palette.globeGradientStops,
      radius,
      centerX,
      centerY
    );
  } else {
    gradient = ctx.createRadialGradient(
      centerX - radius * 0.34,
      centerY - radius * 0.38,
      radius * 0.1,
      centerX,
      centerY,
      radius
    );
    gradient.addColorStop(0, palette.globeStops[0]);
    gradient.addColorStop(0.44, palette.globeStops[1]);
    gradient.addColorStop(1, palette.globeStops[2]);
  }

  ctx.save();
  ctx.beginPath();
  ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.clip();

  drawGraticule(radius, centerX, centerY);
  drawCountries(radius, centerX, centerY);
  drawUnderseaCables(radius, centerX, centerY);
  drawCountryHighlights(radius, centerX, centerY);
  drawCapitalHighlights(radius, centerX, centerY);
  drawUserMarkers(radius, centerX, centerY);
  ctx.restore();

  ctx.beginPath();
  ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
  ctx.strokeStyle = palette.rim;
  ctx.lineWidth = 1.6;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(centerX, centerY, radius * 1.045, 0, Math.PI * 2);
  ctx.strokeStyle = palette.halo;
  ctx.lineWidth = 18;
  ctx.stroke();

  drawHoverCountryLabel(width, height);
}

function animate(now) {
  const elapsed = now - state.lastFrame;
  if (elapsed < GLOBE_FRAME_INTERVAL) {
    requestAnimationFrame(animate);
    return;
  }

  state.lastFrame = now - (elapsed % GLOBE_FRAME_INTERVAL);

  if (state.spin) {
    state.rotation += elapsed * 0.00011;
  }

  drawGlobe();
  requestAnimationFrame(animate);
}

async function loadCountries() {
  countryCount.textContent = "Loading countries…";
  const response = await fetch("/api/globe/countries");
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Unable to load globe countries.");
  }

  state.countries = (payload.countries || []).map(prepareCountryForRendering);
  countryCount.textContent = `${payload.count} country files`;
  renderCountryOptions();
}

function countryMatchesQuery(country) {
  if (!state.countryQuery) {
    return true;
  }

  return country.name.toLowerCase().includes(state.countryQuery);
}

function capitalMatchesQuery(capital, query) {
  if (!query) {
    return true;
  }

  const haystack = `${capital.name} ${capital.country} ${capital.region}`.toLowerCase();
  return haystack.includes(query);
}

function selectedItemCount(items, selectedIds) {
  return items.reduce(
    (count, item) => count + (selectedIds.has(item.id) ? 1 : 0),
    0
  );
}

function selectAllOptionMarkup(scope, ariaLabel, detail, items, selectedIds) {
  const selectedCount = selectedItemCount(items, selectedIds);
  const checked = items.length > 0 && selectedCount === items.length ? "checked" : "";
  const disabled = items.length ? "" : "disabled";
  return `
    <label class="highlight-option highlight-option-all">
      <input
        type="checkbox"
        data-select-all="${escapeHtml(scope)}"
        aria-label="${escapeHtml(ariaLabel)}"
        ${checked}
        ${disabled}
      />
      <span>
        <span class="highlight-name">All</span>
        <span class="highlight-detail">${escapeHtml(detail)}</span>
      </span>
    </label>
  `;
}

function syncSelectAllState(listElement, items, selectedIds) {
  const checkbox = listElement.querySelector("input[data-select-all]");
  if (!checkbox) return;

  const selectedCount = selectedItemCount(items, selectedIds);
  checkbox.checked = items.length > 0 && selectedCount === items.length;
  checkbox.indeterminate = selectedCount > 0 && selectedCount < items.length;
}

function setItemsSelected(items, selectedIds, selected) {
  for (const item of items) {
    if (selected) {
      selectedIds.add(item.id);
    } else {
      selectedIds.delete(item.id);
    }
  }
}

function renderCountryOptions() {
  const allCountries = state.countries;
  const countries = state.countries.filter(countryMatchesQuery);
  const options = countries.length ? countries.map((country) => {
    const checked = state.selectedCountryIds.has(country.id) ? "checked" : "";
    return `
      <label class="highlight-option">
        <input type="checkbox" value="${escapeHtml(country.id)}" ${checked} />
        <span>
          <span class="highlight-name">${escapeHtml(country.name)}</span>
          <span class="highlight-detail">Country fill</span>
        </span>
      </label>
    `;
  }).join("") : `<span class="highlight-empty">No matching countries.</span>`;

  countryList.innerHTML = `
    ${selectAllOptionMarkup(
      "countries",
      "All countries",
      `${allCountries.length} countries`,
      allCountries,
      state.selectedCountryIds
    )}
    ${options}
  `;
  syncSelectAllState(countryList, allCountries, state.selectedCountryIds);
}

function renderCapitalOptions(listElement, dataset, query) {
  const allCapitals = state.capitals.filter((capital) => capital.dataset === dataset);
  const capitals = allCapitals.filter((capital) => capitalMatchesQuery(capital, query));
  const options = capitals.length ? capitals.map((capital) => {
    const checked = state.selectedCapitalIds.has(capital.id) ? "checked" : "";
    const detail = [capital.country, capital.region].filter(Boolean).join(" · ");
    return `
      <label class="highlight-option">
        <input type="checkbox" value="${escapeHtml(capital.id)}" ${checked} />
        <span>
          <span class="highlight-name">${escapeHtml(capital.name)}</span>
          <span class="highlight-detail">${escapeHtml(detail)}</span>
        </span>
      </label>
    `;
  }).join("") : `<span class="highlight-empty">No matching capitals.</span>`;

  const isDateDataset = dataset === DATE_CAPITAL_DATASET;
  listElement.innerHTML = `
    ${selectAllOptionMarkup(
      isDateDataset ? "date-capitals" : "world-capitals",
      isDateDataset ? "All DATE capital cities" : "All capital cities",
      `${allCapitals.length} cities`,
      allCapitals,
      state.selectedCapitalIds
    )}
    ${options}
  `;
  syncSelectAllState(listElement, allCapitals, state.selectedCapitalIds);
}

function renderWorldCapitalOptions() {
  renderCapitalOptions(capitalList, WORLD_CAPITAL_DATASET, state.capitalQuery);
}

function renderDateCapitalOptions() {
  renderCapitalOptions(dateCapitalList, DATE_CAPITAL_DATASET, state.dateCapitalQuery);
}

async function loadCapitals() {
  capitalList.innerHTML = `<span class="highlight-empty">Loading capitals…</span>`;
  dateCapitalList.innerHTML = `<span class="highlight-empty">Loading DATE capitals…</span>`;
  const response = await fetch("/api/globe/capitals");
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Unable to load capital cities.");
  }

  state.capitals = payload.capitals || [];
  renderWorldCapitalOptions();
  renderDateCapitalOptions();
}

async function loadUnderseaCables() {
  if (state.underseaCablesLoaded) {
    return state.cableLines.length;
  }

  underseaCableCount.textContent = "Loading…";
  showUnderseaCablesToggle.disabled = true;
  const response = await fetch("/api/globe/cables");
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Unable to load undersea cables.");
  }

  state.cableLines = (payload.cables || []).flatMap((cable) => (
    flattenedGeometryLines(cable.geometry)
  ));
  state.underseaCablesLoaded = true;
  underseaCableCount.textContent = `${payload.count} routes`;
  showUnderseaCablesToggle.disabled = payload.count === 0;
  return state.cableLines.length;
}

async function loadUserMarkers() {
  userMarkerCount.textContent = "Loading…";
  const response = await fetch("/api/globe/markers");
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(payload.error || "Unable to load user markers.");
  }

  state.userMarkers = payload.markers || [];
  state.markerImages.clear();
  state.userMarkers.forEach(loadMarkerImage);
  userMarkerCount.textContent = `${payload.count} saved`;
  showUserMarkersToggle.disabled = payload.count === 0;
}

function setSpin(spinning) {
  state.spin = spinning;
  pauseButton.textContent = state.spin ? "Pause" : "Resume";
}

pauseButton.addEventListener("click", () => setSpin(!state.spin));
zoomInButton.addEventListener("click", () => setZoom(state.zoom * GLOBE_ZOOM_STEP));
zoomOutButton.addEventListener("click", () => setZoom(state.zoom / GLOBE_ZOOM_STEP));

canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  setZoom(state.zoom * Math.exp(-event.deltaY * 0.0015));
}, { passive: false });

function activePointerDistance() {
  const points = [...activePointers.values()];
  if (points.length < 2) return 0;
  return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
}

canvas.addEventListener("pointerdown", (event) => {
  activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  canvas.setPointerCapture(event.pointerId);
  state.touchLabelPinned = false;
  state.hoverCountry = null;

  if (activePointers.size >= 2) {
    state.pinching = true;
    state.pinchStartDistance = activePointerDistance();
    state.pinchStartZoom = state.zoom;
    state.dragging = false;
    state.dragDistance = 8;
    canvas.classList.remove("is-dragging");
    return;
  }

  state.dragging = true;
  state.dragDistance = 0;
  state.dragStartX = event.clientX;
  state.dragStartY = event.clientY;
  state.dragStartRotation = state.rotation;
  state.dragStartTilt = state.tilt;
  canvas.classList.add("is-dragging");
});

canvas.addEventListener("pointermove", (event) => {
  if (activePointers.has(event.pointerId)) {
    activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  }

  if (state.pinching) {
    const distance = activePointerDistance();
    if (state.pinchStartDistance > 0 && distance > 0) {
      setZoom(state.pinchStartZoom * (distance / state.pinchStartDistance));
    }
    return;
  }

  if (!state.dragging) {
    const point = canvasPointFromEvent(event);
    state.hoverX = point.x;
    state.hoverY = point.y;
    state.hoverDirty = true;
    return;
  }

  const width = Math.max(1, canvas.clientWidth);
  const height = Math.max(1, canvas.clientHeight);
  const deltaX = event.clientX - state.dragStartX;
  const deltaY = event.clientY - state.dragStartY;
  state.dragDistance = Math.max(state.dragDistance, Math.hypot(deltaX, deltaY));

  state.rotation = state.dragStartRotation + (deltaX / width) * Math.PI * 2;
  state.tilt = Math.max(
    -Math.PI / 2.6,
    Math.min(Math.PI / 2.6, state.dragStartTilt + (deltaY / height) * Math.PI)
  );
});

canvas.addEventListener("pointerleave", () => {
  if (!state.dragging && !state.touchLabelPinned) {
    state.hoverX = null;
    state.hoverY = null;
    state.hoverCountry = null;
    state.hoverDirty = false;
  }
});

function finishDrag(event, allowTap = false) {
  if (!state.dragging) {
    return;
  }

  const isTap = allowTap
    && state.dragDistance < 8
    && Number.isFinite(event?.clientX)
    && Number.isFinite(event?.clientY);
  state.dragging = false;
  canvas.classList.remove("is-dragging");

  if (isTap) {
    const point = canvasPointFromEvent(event);
    state.hoverX = point.x;
    state.hoverY = point.y;
    state.hoverDirty = true;
    state.touchLabelPinned = event.pointerType !== "mouse";
    drawGlobe();
  }
}

function finishPointer(event, allowTap = false) {
  const wasPinching = state.pinching;
  activePointers.delete(event.pointerId);

  if (wasPinching) {
    state.dragging = false;
    canvas.classList.remove("is-dragging");
    state.pinching = activePointers.size >= 2;
    if (state.pinching) {
      state.pinchStartDistance = activePointerDistance();
      state.pinchStartZoom = state.zoom;
    }
  } else {
    finishDrag(event, allowTap);
  }

  if (canvas.hasPointerCapture(event.pointerId)) {
    canvas.releasePointerCapture(event.pointerId);
  }
}

canvas.addEventListener("pointerup", (event) => finishPointer(event, true));
canvas.addEventListener("pointercancel", (event) => finishPointer(event));
canvas.addEventListener("lostpointercapture", (event) => finishPointer(event));

canvas.addEventListener("keydown", (event) => {
  const rotationStep = Math.PI / 18;
  const tiltStep = Math.PI / 24;

  if (event.key === "ArrowLeft") {
    state.rotation -= rotationStep;
  } else if (event.key === "ArrowRight") {
    state.rotation += rotationStep;
  } else if (event.key === "ArrowUp") {
    state.tilt = Math.max(-Math.PI / 2.6, state.tilt - tiltStep);
  } else if (event.key === "ArrowDown") {
    state.tilt = Math.min(Math.PI / 2.6, state.tilt + tiltStep);
  } else if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    setZoom(state.zoom * GLOBE_ZOOM_STEP);
    return;
  } else if (event.key === "-" || event.key === "_") {
    event.preventDefault();
    setZoom(state.zoom / GLOBE_ZOOM_STEP);
    return;
  } else if (event.key === "0") {
    event.preventDefault();
    setZoom(1);
    return;
  } else if (event.key === " " || event.key === "Enter") {
    setSpin(!state.spin);
  } else {
    return;
  }

  event.preventDefault();
  state.hoverCountry = null;
  state.hoverDirty = true;
  drawGlobe();
});

capitalSearch.addEventListener("input", () => {
  state.capitalQuery = capitalSearch.value.trim().toLowerCase();
  renderWorldCapitalOptions();
});

dateCapitalSearch.addEventListener("input", () => {
  state.dateCapitalQuery = dateCapitalSearch.value.trim().toLowerCase();
  renderDateCapitalOptions();
});

countrySearch.addEventListener("input", () => {
  state.countryQuery = countrySearch.value.trim().toLowerCase();
  renderCountryOptions();
});

function updateCapitalSelection(event) {
  const checkbox = event.target;
  if (!(checkbox instanceof HTMLInputElement) || checkbox.type !== "checkbox") {
    return;
  }

  const listElement = event.currentTarget;
  const dataset = listElement === dateCapitalList
    ? DATE_CAPITAL_DATASET
    : WORLD_CAPITAL_DATASET;
  const capitals = state.capitals.filter((capital) => capital.dataset === dataset);

  if (checkbox.dataset.selectAll) {
    setItemsSelected(capitals, state.selectedCapitalIds, checkbox.checked);
    renderCapitalOptions(
      listElement,
      dataset,
      dataset === DATE_CAPITAL_DATASET ? state.dateCapitalQuery : state.capitalQuery
    );
    drawGlobe();
    return;
  }

  if (checkbox.checked) {
    state.selectedCapitalIds.add(checkbox.value);
  } else {
    state.selectedCapitalIds.delete(checkbox.value);
  }
  syncSelectAllState(listElement, capitals, state.selectedCapitalIds);
  drawGlobe();
}

capitalList.addEventListener("change", updateCapitalSelection);
dateCapitalList.addEventListener("change", updateCapitalSelection);

countryList.addEventListener("change", (event) => {
  const checkbox = event.target;
  if (!(checkbox instanceof HTMLInputElement) || checkbox.type !== "checkbox") {
    return;
  }

  if (checkbox.dataset.selectAll) {
    setItemsSelected(state.countries, state.selectedCountryIds, checkbox.checked);
    renderCountryOptions();
    drawGlobe();
    return;
  }

  if (checkbox.checked) {
    state.selectedCountryIds.add(checkbox.value);
  } else {
    state.selectedCountryIds.delete(checkbox.value);
  }
  syncSelectAllState(countryList, state.countries, state.selectedCountryIds);
  drawGlobe();
});

clearCapitalsButton.addEventListener("click", (event) => {
  event.stopPropagation();
  for (const capital of state.capitals) {
    if (capital.dataset === WORLD_CAPITAL_DATASET) {
      state.selectedCapitalIds.delete(capital.id);
    }
  }
  renderWorldCapitalOptions();
  drawGlobe();
});

clearDateCapitalsButton.addEventListener("click", (event) => {
  event.stopPropagation();
  for (const capital of state.capitals) {
    if (capital.dataset === DATE_CAPITAL_DATASET) {
      state.selectedCapitalIds.delete(capital.id);
    }
  }
  renderDateCapitalOptions();
  drawGlobe();
});

clearCountriesButton.addEventListener("click", (event) => {
  event.stopPropagation();
  state.selectedCountryIds.clear();
  renderCountryOptions();
  drawGlobe();
});

showUserMarkersToggle.addEventListener("change", () => {
  state.showUserMarkers = showUserMarkersToggle.checked;
  drawGlobe();
});

showUnderseaCablesToggle.addEventListener("change", async () => {
  if (!showUnderseaCablesToggle.checked) {
    state.showUnderseaCables = false;
    drawGlobe();
    return;
  }

  try {
    const lineCount = await loadUnderseaCables();
    state.showUnderseaCables = lineCount > 0;
    showUnderseaCablesToggle.checked = state.showUnderseaCables;
    drawGlobe();
  } catch (error) {
    state.showUnderseaCables = false;
    showUnderseaCablesToggle.checked = false;
    showUnderseaCablesToggle.disabled = false;
    underseaCableCount.textContent = "Unavailable";
    console.warn(error);
  }
});

window.addEventListener("storage", (event) => {
  if (event.key === window.DateMapperSettings.storageKey) {
    setColourScheme(window.DateMapperSettings.get().colourScheme);
  }
});

window.addEventListener("pageshow", () => {
  setColourScheme(window.DateMapperSettings.get().colourScheme);
});

window.addEventListener("resize", () => {
  resizeCanvas();
  drawGlobe();
});

setColourScheme(state.colourScheme);
resizeCanvas();
requestAnimationFrame(animate);
loadCountries().catch((error) => {
  countryCount.textContent = String(error.message || error);
});
loadCapitals().catch((error) => {
  capitalList.innerHTML = `<span class="highlight-empty">${String(error.message || error)}</span>`;
  dateCapitalList.innerHTML = `<span class="highlight-empty">${String(error.message || error)}</span>`;
});
loadUserMarkers().catch((error) => {
  userMarkerCount.textContent = "Unavailable";
  showUserMarkersToggle.disabled = true;
  showUserMarkersToggle.checked = false;
  state.showUserMarkers = false;
  console.warn(error);
});
