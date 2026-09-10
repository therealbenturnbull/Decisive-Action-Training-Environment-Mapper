// Aim: Bind settings-page controls to DATE Mapper's shared persistent preferences.
// Author: Benjamin Turnbull

document.addEventListener("DOMContentLoaded", () => {
  const flipAntarctica = document.getElementById("flipAntarcticaSetting");
  const colourSchemeOptions = document.getElementById("colourSchemeOptions");
  const status = document.getElementById("settingsStatus");
  const settings = window.DateMapperSettings.get();

  for (const scheme of window.DateMapperColourSchemes.all()) {
    const option = document.createElement("label");
    option.className = "colour-scheme-option";
    option.htmlFor = `colourScheme-${scheme.id}`;

    const input = document.createElement("input");
    input.id = `colourScheme-${scheme.id}`;
    input.type = "radio";
    input.name = "colourScheme";
    input.value = scheme.id;
    input.checked = scheme.id === settings.colourScheme;

    const swatch = document.createElement("span");
    swatch.className = "colour-scheme-swatch";
    swatch.setAttribute("aria-hidden", "true");
    for (const colour of scheme.swatches) {
      const segment = document.createElement("span");
      segment.style.backgroundColor = colour;
      swatch.appendChild(segment);
    }

    const copy = document.createElement("span");
    const name = document.createElement("span");
    const description = document.createElement("span");
    name.className = "setting-name";
    name.textContent = scheme.label;
    description.className = "setting-description";
    description.textContent = scheme.description;
    copy.append(name, description);

    input.addEventListener("change", () => {
      if (!input.checked) return;
      window.DateMapperSettings.update({ colourScheme: input.value });
      status.textContent = "Settings saved.";
    });

    option.append(input, swatch, copy);
    colourSchemeOptions.appendChild(option);
  }

  flipAntarctica.checked = Boolean(settings.flipAntarcticaVertically);
  flipAntarctica.addEventListener("change", () => {
    window.DateMapperSettings.update({
      flipAntarcticaVertically: flipAntarctica.checked,
    });
    status.textContent = "Settings saved.";
  });
});
