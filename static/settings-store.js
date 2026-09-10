// Aim: Persist shared DATE Mapper settings and notify active views when they change.
// Author: Benjamin Turnbull

(() => {
  const storageKey = "dateMapper.settings";
  const supportedColourSchemes = new Set([
    "basic",
    "modern",
    "neon-night",
    "mirrorwave",
    "wireframe",
    "white-fill",
    "inverted",
    "natural",
    "nordic",
    "pacific",
    "graphite",
    "solar",
    "aurora",
    "transit",
  ]);
  const defaults = Object.freeze({
    flipAntarcticaVertically: false,
    colourScheme: "basic",
  });

  function normalize(settings) {
    const colourScheme = String(settings?.colourScheme || "").trim().toLowerCase();
    return {
      ...defaults,
      ...(settings || {}),
      flipAntarcticaVertically: Boolean(settings?.flipAntarcticaVertically),
      colourScheme: supportedColourSchemes.has(colourScheme)
        ? colourScheme
        : defaults.colourScheme,
    };
  }

  function get() {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
      return normalize(stored);
    } catch {
      return { ...defaults };
    }
  }

  function update(changes) {
    const settings = normalize({ ...get(), ...changes });
    localStorage.setItem(storageKey, JSON.stringify(settings));
    window.dispatchEvent(new CustomEvent("date-mapper-settings-change", {
      detail: settings,
    }));
    return settings;
  }

  window.DateMapperSettings = { get, storageKey, update };
})();
