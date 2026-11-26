// Behavioral coverage for the map UI without a browser or third-party packages.
// Run with: node --test tests/test_main.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/main.js"), "utf8");
const MARKERS = "user_defined_markers";
const flush = () => new Promise(setImmediate);

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.checked = false;
    this.disabled = false;
    this.value = "";
    this.files = [];
    this.classes = new Set();
    this.classList = {
      toggle: (name, enabled) => {
        const next = enabled ?? !this.classes.has(name);
        if (next) this.classes.add(name);
        else this.classes.delete(name);
        return next;
      },
      contains: (name) => this.classes.has(name),
    };
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.append(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = children;
  }

  set textContent(value) {
    this.children = [String(value)];
  }

  get textContent() {
    return this.children.map((child) => (
      typeof child === "string" ? child : child.textContent
    )).join("");
  }

  set innerHTML(value) {
    // Clearing a container is safe. Any markup construction should fail these
    // tests so user-controlled strings cannot silently become parsed HTML.
    assert.equal(value, "", "use DOM text nodes instead of assigning HTML");
    this.children = [];
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  querySelectorAll(selector) {
    const matches = (element) => {
      if (selector === "[data-layer-group]") return "layerGroup" in element.dataset;
      if (selector === "[data-group-count]") return "groupCount" in element.dataset;
      return element.tagName.toLowerCase() === selector;
    };
    return this.children.flatMap((child) => {
      if (!(child instanceof Element)) return [];
      return [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)];
    });
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

class Layer {
  constructor(data = {}) {
    this.data = data;
    this.children = [];
  }

  addTo(map) { map.layers.add(this); return this; }
  bindPopup(content) { this.popup = content; return this; }
  bindTooltip(content) { this.tooltip = content; return this; }
  on() { return this; }
  eachLayer(callback) { this.children.forEach(callback); }
  getBounds() { return this.data.bounds || [0, 0]; }
  setStyle() {}
}

function fixture() {
  const elements = new Map();
  const body = new Element("body");
  const document = {
    body,
    createElement: (tag) => new Element(tag),
    createTextNode: (value) => String(value),
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) {
        const element = new Element();
        elements.set(id, element);
        body.append(element);
      }
      return elements.get(id);
    },
    querySelectorAll(selector) {
      if (selector === "#layerPanelList details") {
        return this.getElementById("layerPanelList").querySelectorAll("details");
      }
      return body.querySelectorAll(selector);
    },
  };
  const map = {
    layers: new Set(),
    fitCalls: [],
    setView() { return this; },
    hasLayer(layer) { return this.layers.has(layer); },
    removeLayer(layer) { this.layers.delete(layer); },
    getContainer() { return document.getElementById("map"); },
    fitBounds(bounds) { this.fitCalls.push(bounds); },
    on() {},
  };
  const requests = [];
  const context = vm.createContext({
    document,
    AbortController,
    FormData,
    console,
    window: {
      addEventListener() {},
      DateMapperSettings: { get: () => ({ colourScheme: "basic" }) },
      DateMapperColourSchemes: {
        get: () => ({ id: "basic", map: {} }),
        countryFill: () => "#ffffff",
        countryStroke: () => "#000000",
      },
    },
    L: {
      map: () => map,
      marker: () => new Layer(),
      circleMarker: () => new Layer(),
      icon: (options) => options,
      geoJSON(data, options) {
        const layer = new Layer(data);
        for (const feature of data.features || [data]) {
          const child = feature.geometry?.type === "Point"
            ? options.pointToLayer(feature, feature.geometry.coordinates)
            : new Layer();
          child.feature = feature;
          options.onEachFeature(feature, child);
          layer.children.push(child);
        }
        return layer;
      },
    },
    fetch(url, options = {}) {
      // Deliberately ignore abort events. Real responses can already be in
      // flight or decoding JSON when cancelled, so generation guards still
      // need to reject their eventual completion.
      return new Promise((resolve, reject) => {
        const request = {
          url, options, settled: false,
          respond(payload, status = 200) {
            assert.equal(this.settled, false, "request already settled");
            this.settled = true;
            resolve({ ok: status < 400, status, json: async () => payload });
          },
          fail(error) {
            assert.equal(this.settled, false, "request already settled");
            this.settled = true;
            reject(error);
          },
        };
        requests.push(request);
      });
    },
  });
  vm.runInContext(source, context, { filename: "static/main.js" });
  vm.runInContext(`
    initMap();
    globalThis.mapState = { activeLayers, layerRows, layerLoadPromises,
      get selectedCountry() { return selectedCountry; } };
  `, context);
  return {
    context, document, map, requests, state: context.mapState,
    pending: (url) => requests.filter((request) => !request.settled && (!url || request.url === url)),
  };
}

function item(id, overrides = {}) {
  return { id, name: id, available: true, category: "Country", ...overrides };
}

function geojson(version, properties = {}, geometry = { type: "Point", coordinates: [1, 2] }) {
  return { type: "FeatureCollection", version, features: [{ type: "Feature", properties, geometry }] };
}

async function load(harness, catalog) {
  const loading = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond(catalog);
  await flush();
  while (harness.pending().length) {
    for (const request of harness.pending()) request.respond(geojson(request.url));
    await flush();
  }
  await loading;
}

test("GeoJSON popup keys, values, and tooltips remain text", () => {
  const harness = fixture();
  const key = '<img src=x onerror="alert(1)">';
  const name = '<svg onload="alert(2)">';
  const description = '<script>alert(3)</script> & <b>notes</b>';
  const data = geojson("hostile", { [key]: description, name }, {
    type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]],
  });
  const layer = harness.context.makeGeoJsonLayer("country", "Country", data, item("country"));
  const { popup, tooltip } = layer.children[0];

  assert.ok(popup instanceof Element, "Leaflet popup receives a DOM node, not HTML");
  assert.ok(popup.textContent.includes(key));
  assert.ok(popup.textContent.includes(description));
  assert.ok(popup.textContent.includes(name));
  assert.equal(popup.querySelectorAll("img").length, 0);
  assert.equal(popup.querySelectorAll("script").length, 0);
  assert.equal(popup.querySelectorAll("svg").length, 0);
  assert.ok(tooltip instanceof Element, "Leaflet tooltip receives a DOM node, not HTML");
  assert.equal(tooltip.textContent, harness.context.featureHoverLabel(data.features[0], "Country"));
  assert.equal(tooltip.querySelectorAll("svg").length, 0);
});

test("a stale catalog response cannot replace the newest catalog", async () => {
  const harness = fixture();
  const first = harness.context.loadCatalogAndLayers();
  const oldCatalog = harness.pending("/api/layers")[0];
  const second = harness.context.loadCatalogAndLayers();
  const newCatalog = harness.pending("/api/layers")[1];

  assert.equal(oldCatalog.options.signal?.aborted, true);
  newCatalog.respond([item("new", { default_visible: false })]);
  await second;
  oldCatalog.respond([item("old")]);
  await first;

  assert.deepEqual([...harness.state.layerRows.keys()], ["new"]);
  assert.equal(harness.requests.filter((request) => request.url.startsWith("/api/layer/")).length, 0);
});

for (const staleResult of ["success", "failure"]) {
  test(`a stale layer ${staleResult} cannot clean up or overwrite its replacement request`, async () => {
    const harness = fixture();
    const first = harness.context.loadCatalogAndLayers();
    harness.pending("/api/layers")[0].respond([item("country")]);
    await flush();
    const oldLayer = harness.pending("/api/layer/country")[0];

    const second = harness.context.loadCatalogAndLayers();
    harness.pending("/api/layers")[0].respond([item("country")]);
    await flush();
    const newLayer = harness.pending("/api/layer/country")[1];
    assert.ok(newLayer, "replacement request starts while old response is outstanding");
    assert.equal(oldLayer.options.signal?.aborted, true);

    if (staleResult === "success") oldLayer.respond(geojson("old"));
    else oldLayer.fail(new Error("old request failed"));
    await first;
    const row = harness.state.layerRows.get("country");
    assert.equal(row.checkbox.checked, true);
    assert.equal(row.element.classList.contains("has-error"), false);

    const duplicate = harness.context.setLayerVisibility("country", true);
    await flush();
    assert.equal(harness.requests.filter((request) => request.url === "/api/layer/country").length, 2,
      "stale cleanup must not erase replacement request deduplication");
    newLayer.respond(geojson("new"));
    await Promise.all([second, duplicate]);

    assert.equal(harness.map.layers.size, 1);
    assert.equal(harness.state.activeLayers.get("country").data.version, "new");
    assert.equal(harness.state.layerLoadPromises.size, 0);
  });
}

test("a late old layer cannot appear after the replacement finishes", async () => {
  const harness = fixture();
  const first = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond([item("country")]);
  await flush();
  const oldLayer = harness.pending("/api/layer/country")[0];
  const second = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond([item("country")]);
  await flush();
  harness.pending("/api/layer/country")[1].respond(geojson("new"));
  await second;
  oldLayer.respond(geojson("old"));
  await first;

  assert.equal(harness.map.layers.size, 1);
  assert.equal([...harness.map.layers][0].data.version, "new");
  assert.equal(harness.state.activeLayers.get("country"), [...harness.map.layers][0]);
});

for (const markersVisible of [true, false]) {
  test(`refreshing ${markersVisible ? "visible" : "hidden"} markers preserves other layers and visibility`, async () => {
    const harness = fixture();
    await load(harness, [item("country"), item("hidden"), item(MARKERS, { category: "User-Defined" })]);
    await harness.context.setLayerVisibility("hidden", false);
    if (!markersVisible) await harness.context.setLayerVisibility(MARKERS, false);
    harness.context.selectCountry(item("country"), { properties: { NAME_SHORT: "Selected" } }, "Country");
    const country = harness.state.activeLayers.get("country");
    const hidden = harness.state.activeLayers.get("hidden");
    const oldMarkers = harness.state.activeLayers.get(MARKERS);
    const selection = harness.state.selectedCountry;
    const previousRequestCount = harness.requests.length;

    const refreshing = harness.context.refreshUserMarkers();
    await flush();
    assert.deepEqual(harness.requests.slice(previousRequestCount).map((request) => request.url),
      markersVisible ? [`/api/layer/${MARKERS}`] : []);
    if (markersVisible) {
      harness.pending(`/api/layer/${MARKERS}`)[0].respond(geojson("updated markers"));
    }
    await refreshing;

    assert.equal(harness.state.activeLayers.get("country"), country);
    assert.equal(harness.state.activeLayers.get("hidden"), hidden);
    assert.equal(harness.state.selectedCountry, selection);
    assert.equal(harness.map.hasLayer(country), true);
    assert.equal(harness.map.hasLayer(hidden), false);
    assert.equal(harness.map.hasLayer(oldMarkers), false);
    assert.equal(harness.state.layerRows.get(MARKERS).checkbox.checked, markersVisible);

    if (!markersVisible) {
      assert.equal(harness.state.activeLayers.has(MARKERS), false,
        "hidden markers are invalidated without an unnecessary request");
      const showing = harness.context.setLayerVisibility(MARKERS, true);
      await flush();
      harness.pending(`/api/layer/${MARKERS}`)[0].respond(geojson("updated markers"));
      await showing;
    }
    const newMarkers = harness.state.activeLayers.get(MARKERS);
    assert.notEqual(newMarkers, oldMarkers);
    assert.equal(newMarkers.data.version, "updated markers");
    assert.equal(harness.map.hasLayer(newMarkers), true);
  });
}

test("reloading after all layers are hidden preserves the user's choices", async () => {
  const harness = fixture();
  const catalog = [item("country"), item(MARKERS)];
  await load(harness, catalog);
  for (const entry of catalog) await harness.context.setLayerVisibility(entry.id, false);
  const previousRequestCount = harness.requests.length;
  const reloading = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond(catalog);
  await reloading;

  assert.deepEqual(harness.requests.slice(previousRequestCount).map((request) => request.url), ["/api/layers"]);
  assert.equal(harness.map.layers.size, 0);
  for (const row of harness.state.layerRows.values()) assert.equal(row.checkbox.checked, false);
});

test("hiding markers during a refresh cannot restore their old cached data", async () => {
  const harness = fixture();
  await load(harness, [item(MARKERS, { category: "User-Defined" })]);
  const original = harness.state.activeLayers.get(MARKERS);
  const refreshing = harness.context.refreshUserMarkers();
  await flush();
  const obsoleteRequest = harness.pending(`/api/layer/${MARKERS}`)[0];
  await harness.context.setLayerVisibility(MARKERS, false);
  obsoleteRequest.respond(geojson("cancelled refresh"));
  await refreshing;

  assert.equal(harness.map.hasLayer(original), false);
  const showing = harness.context.setLayerVisibility(MARKERS, true);
  await flush();
  const latestRequest = harness.pending(`/api/layer/${MARKERS}`)[0];
  assert.ok(latestRequest, "showing markers must fetch saved data instead of reusing the old overlay");
  latestRequest.respond(geojson("latest markers"));
  await showing;
  assert.notEqual(harness.state.activeLayers.get(MARKERS), original);
  assert.equal(harness.state.activeLayers.get(MARKERS).data.version, "latest markers");
  assert.equal(harness.map.layers.size, 1);
});

test("a failed catalog reload allows the previous layer load to finish", async () => {
  const harness = fixture();
  const initial = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond([item("country")]);
  await flush();
  const initialLayer = harness.pending("/api/layer/country")[0];
  const reloading = harness.context.loadCatalogAndLayers();
  const rejected = assert.rejects(reloading, /Unable to load layer catalog/);
  harness.pending("/api/layers")[0].respond({ error: "temporary failure" }, 500);
  await rejected;

  assert.equal(initialLayer.options.signal.aborted, false,
    "working layer requests remain valid until a replacement catalog is available");
  initialLayer.respond(geojson("original catalog"));
  await initial;
  assert.equal(harness.state.layerRows.get("country").checkbox.checked, true);
  assert.equal(harness.map.hasLayer(harness.state.activeLayers.get("country")), true);
  assert.equal(harness.state.layerLoadPromises.size, 0);
});

test("a cancelled group load cannot overwrite a newer hidden status", async () => {
  const harness = fixture();
  await load(harness, [item("optional", { default_visible: false })]);
  const showing = harness.context.setLayerGroupVisibility(["optional"], true);
  await flush();
  const request = harness.pending("/api/layer/optional")[0];
  await harness.context.setLayerGroupVisibility(["optional"], false);
  const status = harness.document.getElementById("status").textContent;
  assert.match(status, /Hidden/);
  request.respond(geojson("cancelled group load"));
  await showing;

  assert.equal(harness.state.layerRows.get("optional").checkbox.checked, false);
  assert.equal(harness.map.layers.size, 0);
  assert.equal(harness.document.getElementById("status").textContent, status);
});

for (const refreshSucceeds of [true, false]) {
  test(`the marker form reports a saved marker when its overlay refresh ${refreshSucceeds ? "succeeds" : "fails"}`, async () => {
    const harness = fixture();
    await load(harness, [item("country"), item(MARKERS, { category: "User-Defined" })]);
    harness.context.bindMarkerControls();
    harness.document.getElementById("markerName").value = "New marker";
    harness.document.getElementById("markerDescription").value = "Notes";
    harness.document.getElementById("markerLat").value = "1";
    harness.document.getElementById("markerLng").value = "2";
    const country = harness.state.activeLayers.get("country");
    const previousRequestCount = harness.requests.length;
    const submit = harness.document.getElementById("markerForm").listeners.get("submit")[0];
    const submitting = submit({ preventDefault() {} });
    const saving = harness.pending("/api/markers")[0];
    assert.equal(saving.options.method, "POST");
    assert.equal(saving.options.body.get("name"), "New marker");
    saving.respond({ properties: { name: "New marker" } });
    await flush();
    const refreshing = harness.pending(`/api/layer/${MARKERS}`)[0];
    if (refreshSucceeds) refreshing.respond(geojson("saved marker"));
    else refreshing.fail(new Error("temporary network failure"));
    await submitting;

    assert.deepEqual(harness.requests.slice(previousRequestCount).map((request) => request.url),
      ["/api/markers", `/api/layer/${MARKERS}`]);
    assert.equal(harness.state.activeLayers.get("country"), country);
    assert.equal(harness.map.hasLayer(country), true);
    assert.equal(harness.document.getElementById("markerName").value, "");
    assert.match(harness.document.getElementById("status").textContent, /^Saved marker: New marker\./);
    if (!refreshSucceeds) {
      assert.match(harness.document.getElementById("status").textContent, /Unable to refresh markers/);
    }
  });
}

test("catalog loading uses bounded parallel requests and finishes every visible layer", async () => {
  const harness = fixture();
  const catalog = Array.from({ length: 12 }, (_, index) => item(`country-${index}`));
  const loading = harness.context.loadCatalogAndLayers();
  harness.pending("/api/layers")[0].respond(catalog);
  await flush();
  const concurrency = harness.pending().length;
  assert.ok(concurrency > 1, "independent layers load concurrently");
  assert.ok(concurrency < catalog.length, "the entire catalog must not load at once");

  let completed = 0;
  while (harness.pending().length) {
    assert.ok(harness.pending().length <= concurrency, "request limit stays bounded");
    harness.pending()[0].respond(geojson(`loaded-${completed++}`));
    await flush();
  }
  await loading;
  assert.equal(completed, catalog.length);
  assert.equal(harness.map.layers.size, catalog.length);
  assert.equal(harness.state.activeLayers.size, catalog.length);
  assert.equal(harness.document.getElementById("status").textContent, "Ready.");
});
