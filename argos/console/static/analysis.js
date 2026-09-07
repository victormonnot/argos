/* Historical reception analysis. All network requests are read-only. */
(() => {
  "use strict";
  const root = document.getElementById("recording-analysis-root");
  if (!root) return;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const nonnegative = (value) => finite(value) && value >= 0;
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const nullable = (value) => value === null || nonnegative(value);
  const format = new Intl.NumberFormat("fr-FR", { maximumSignificantDigits: 5 });
  const fmt = (value) => finite(value) ? format.format(value) : "Inconnu";
  const aggregationBins = (duration) => Math.max(1, Math.min(160, Math.ceil(duration)));
  const identifier = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const revision = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const make = (tag, className = "", text = null) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== null) element.textContent = text;
    return element;
  };
  const svgNode = (tag, attributes = {}, text = null) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== null) element.textContent = text;
    return element;
  };
  const setText = (element, value) => { if (element.textContent !== value) element.textContent = value; };
  const filters = make("form", "analysis-filters");
  filters.setAttribute("aria-label", "Filtres de l’analyse du journal");
  const sourceLabel = make("label", "", "Source enregistrée");
  const source = make("select");
  source.id = "analysis-source";
  source.append(new Option("Toutes les sources", ""));
  sourceLabel.append(source);
  const typeLabel = make("label", "", "Type de message");
  const type = make("select");
  type.id = "analysis-type";
  type.append(new Option("Tous les types", ""));
  typeLabel.append(type);
  const thresholdLabel = make("label", "", "Sans réception à partir de (s)");
  const thresholdRow = make("span", "analysis-threshold-row");
  const threshold = make("input");
  threshold.id = "analysis-gap-threshold";
  threshold.type = "number";
  threshold.min = ".001";
  threshold.max = "86400";
  threshold.step = "any";
  threshold.value = "1";
  threshold.required = true;
  const apply = make("button", "button button-outline", "Appliquer");
  apply.type = "submit";
  thresholdRow.append(threshold, apply);
  thresholdLabel.append(thresholdRow);
  filters.append(sourceLabel, typeLabel, thresholdLabel);
  const status = make("p", "analysis-status");
  status.id = "analysis-status";
  status.setAttribute("role", "status");
  const retry = make("button", "button button-outline", "Réessayer l’analyse");
  retry.type = "button";
  retry.hidden = true;
  const results = make("div", "analysis-results");
  results.hidden = true;
  const summary = make("dl", "analysis-summary");
  const summaryNodes = {};
  for (const [name, label] of [["events", "Messages filtrés"], ["mean", "Cadence moyenne"], ["gap", "Plus longue période sans réception"]]) {
    const row = make("div");
    const output = summaryNodes[name] = make("dd");
    row.append(make("dt", "", label), output);
    summary.append(row);
  }
  const charts = make("div", "analysis-charts");
  const chartSpecs = [
    { field: "rate_hz", title: "Cadence des réceptions", unit: "Hz", className: "analysis-rate", description: "Nombre de messages reçus par seconde, moyenné dans chaque intervalle." },
    { field: "max_age_s", title: "Âge maximal de réception", unit: "s", className: "analysis-age", description: "Temps depuis la dernière réception filtrée, au maximum de chaque intervalle ; âge inconnu avant la première réception. Il ne s’agit pas de l’âge de mesure du capteur." },
  ];
  for (const spec of chartSpecs) {
    spec.figure = make("figure", `analysis-chart ${spec.className}`);
    const heading = make("figcaption", "", spec.title);
    spec.svg = svgNode("svg", { role: "img", "aria-label": `${spec.title}, en ${spec.unit}, selon le temps depuis le début de la capture.` });
    spec.empty = make("p", "analysis-chart-empty");
    const description = make("p", "analysis-note", spec.description);
    spec.figure.append(heading, spec.svg, spec.empty, description);
    charts.append(spec.figure);
  }
  const cursorControls = make("div", "analysis-cursor");
  const cursorLabel = make("label", "", "Lire un intervalle");
  cursorLabel.htmlFor = "analysis-bin-cursor";
  const cursor = make("input");
  cursor.type = "range";
  cursor.id = "analysis-bin-cursor";
  cursor.min = "0";
  cursor.step = "1";
  const readout = make("p", "analysis-bin-readout");
  cursorControls.append(cursorLabel, cursor, readout);
  const gapsSection = make("section", "analysis-gaps");
  const gapsTitle = make("h3", "", "Périodes sans réception");
  const gapsNote = make("p", "analysis-note");
  const distinction = make("p", "analysis-note", "Une période sans réception ne prouve pas une perte de paquets.");
  const gapsTable = make("table", "analysis-gap-table");
  const tableHead = make("thead");
  const headers = make("tr");
  for (const label of ["Dans la capture", "Durée", "Position"]) {
    const cell = make("th", "", label);
    cell.scope = "col";
    headers.append(cell);
  }
  tableHead.append(headers);
  const gapRows = make("tbody");
  gapsTable.append(tableHead, gapRows);
  gapsSection.append(gapsTitle, gapsNote, distinction, gapsTable);
  results.append(summary, charts, cursorControls, gapsSection);
  const contextDetails = make("details", "analysis-context");
  contextDetails.append(make("summary", "", "Contexte enregistré pendant la capture"));
  const contextStatus = make("p", "analysis-note");
  const contextFields = make("dl", "analysis-context-fields");
  contextDetails.append(contextStatus, contextFields);
  root.append(filters, status, retry, results, contextDetails);

  let metadata = null;
  let visible = false;
  let data = null;
  let request = null;
  let epoch = 0;
  let binIndex = 0;
  let queryKey = null;
  let menuKey = null;
  let resizeFrame = null;
  const active = () => visible && Boolean(metadata) && !document.hidden;

  function cancel() {
    epoch += 1;
    request?.abort();
    request = null;
    root.setAttribute("aria-busy", "false");
  }

  function clear(message = "") {
    data = null;
    queryKey = null;
    results.hidden = true;
    retry.hidden = true;
    setText(status, message);
  }

  function selection() {
    const seconds = threshold.valueAsNumber;
    if (!finite(seconds) || seconds < .001 || seconds > 86400) return null;
    const pair = source.value ? source.value.split(":").map(Number) : [null, null];
    return { system: pair[0], component: pair[1], message_id: type.value === "" ? null : Number(type.value), gap_threshold_s: seconds };
  }

  function valid(value, selected) {
    if (!value || value.id !== metadata.id || value.revision !== metadata.revision || !nonnegative(value.duration_s) ||
      value.duration_s !== metadata.duration_s || !integer(value.events) || !value.filter ||
      !["system", "component", "message_id"].every((key) => value.filter[key] === selected[key]) ||
      value.gap_threshold_s !== selected.gap_threshold_s || !value.summary || !nullable(value.summary.mean_rate_hz) || !nullable(value.summary.max_gap_s) ||
      !integer(value.gap_count) || typeof value.gaps_truncated !== "boolean" || !Array.isArray(value.bins) || value.bins.length !== aggregationBins(value.duration_s) ||
      !Array.isArray(value.gaps) || value.gaps.length > 100 || value.gaps.length > value.gap_count ||
      !Array.isArray(value.sources) || !Array.isArray(value.message_types)) return false;
    const interval = (item) => item && nonnegative(item.start_s) && nonnegative(item.end_s) && item.start_s <= item.end_s && item.end_s <= value.duration_s;
    return value.bins.every((item, index) => interval(item) && integer(item.events) && nullable(item.rate_hz) && nullable(item.max_age_s) &&
      (index === 0 || item.start_s === value.bins[index - 1].end_s)) &&
      value.bins[0].start_s === 0 && value.bins[value.bins.length - 1].end_s === value.duration_s &&
      value.bins.reduce((total, item) => total + item.events, 0) === value.events &&
      value.gaps.every((item) => interval(item) && nonnegative(item.duration_s) && ["start", "end", "interior", "whole"].includes(item.boundary)) &&
      value.sources.every((item) => item && integer(item.system) && item.system <= 255 && integer(item.component) && item.component <= 255 && integer(item.events)) &&
      value.message_types.every((item) => item && integer(item.message_id) && item.message_id <= 0xffffff && integer(item.events) &&
        (item.type_name === undefined || (typeof item.type_name === "string" && item.type_name.length <= 128)));
  }

  function fillMenus(value) {
    const signature = JSON.stringify([value.sources, value.message_types]);
    if (menuKey === signature) return;
    menuKey = signature;
    const sourceValue = source.value, typeValue = type.value;
    source.replaceChildren(new Option("Toutes les sources", ""));
    for (const item of value.sources) source.append(new Option(`Système ${item.system} · composant ${item.component}`, `${item.system}:${item.component}`));
    type.replaceChildren(new Option("Tous les types", ""));
    for (const item of value.message_types) type.append(new Option(`${item.type_name ? `${item.type_name} · ` : "Message "}${item.message_id} · ${fmt(item.events)} reçus`, String(item.message_id)));
    source.value = sourceValue;
    type.value = typeValue;
  }

  function renderContext() {
    contextFields.replaceChildren();
    const recorded = metadata?.context_status === "recorded" && metadata.context;
    if (!recorded) {
      setText(contextStatus, metadata?.context_status === "unavailable" ? "Ce journal ne contient pas de contexte de capture exploitable." : "Le contexte de capture est inconnu pour ce journal.");
      return;
    }
    setText(contextStatus, "Paramètres enregistrés avec le journal. Les filtres et le seuil ci-dessus sont des réglages de cette analyse.");
    const configuration = recorded.configuration || {};
    const entries = [
      ["Début de capture · UTC", recorded.captured_at_utc], ["Observation", recorded.run_id],
      ["Environnement", ({ simulation: "Simulation déclarée", real: "Réel déclaré", unconfigured: "Non configuré" })[configuration.environment] || configuration.environment],
      ["Liaison reçue", recorded.telemetry_endpoint], ["Transport MAVLink", configuration.mavlink_transport],
      ["Source des mesures", configuration.system == null ? null : `${configuration.system} / ${configuration.component}`],
      ["Comptage des séquences", configuration.sequence_scope],
      ["Source vidéo", configuration.video_source], ["Point d’entrée vidéo", configuration.video_endpoint],
      ["Adresse UDP locale", configuration.mavlink_bind], ["Correspondant UDP", configuration.mavlink_peer],
      ["Adresse TCP", configuration.mavlink_tcp], ["Port série", configuration.mavlink_device],
      ["Débit série", configuration.mavlink_transport === "serial" ? configuration.baudrate : null],
    ];
    const names = { video: "image", heartbeat: "mode déclaré", attitude: "attitude", local_position_ned: "position", battery: "batterie" };
    for (const [name, value] of Object.entries(recorded.age_limits_s || {})) entries.push([`Seuil de réception · ${names[name] || name}`, finite(value) ? `${fmt(value)} s` : value]);
    for (const [label, value] of entries) {
      if (value === null || value === undefined || value === "") continue;
      const row = make("div");
      row.append(make("dt", "", label), make("dd", "", typeof value === "object" ? JSON.stringify(value) : String(value)));
      contextFields.append(row);
    }
  }

  function axisMaximum(value) {
    if (!(value > 0)) return 1;
    const scale = 10 ** Math.floor(Math.log10(value));
    const normalized = value / scale;
    const maximum = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * scale;
    return finite(maximum) && maximum > 0 ? maximum : value;
  }

  function drawCharts() {
    if (!data || !visible || results.hidden) return;
    for (const spec of chartSpecs) {
      const width = Math.max(260, Math.floor(spec.figure.clientWidth));
      const height = 190;
      const left = 52, right = width - 12, top = 23, bottom = height - 34;
      const svg = spec.svg;
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      svg.replaceChildren();
      const values = data.bins.map((bin) => bin[spec.field]).filter(finite);
      const known = values.length > 0 && data.duration_s > 0;
      spec.empty.hidden = known;
      setText(spec.empty, data.duration_s === 0 ? "Durée nulle : cette courbe n’est pas définie." : spec.field === "max_age_s" ? "Aucune réception filtrée pour calculer cet âge." : "Aucun intervalle de cadence disponible.");
      svg.toggleAttribute("hidden", !known);
      if (!known) continue;
      const maximum = axisMaximum(Math.max(...values));
      const x = (time) => left + (right - left) * time / data.duration_s;
      const y = (value) => bottom - (bottom - top) * (value / maximum);
      for (const fraction of [0, .5, 1]) {
        const value = maximum * fraction;
        svg.append(svgNode("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "analysis-gridline" }));
        svg.append(svgNode("text", { x: left - 7, y: y(value) + 4, "text-anchor": "end", class: "analysis-axis" }, fmt(value)));
      }
      svg.append(svgNode("text", { x: left, y: 14, class: "analysis-axis" }, spec.unit));
      for (const fraction of [0, .5, 1]) svg.append(svgNode("text", {
        x: x(data.duration_s * fraction), y: bottom + 23,
        "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle", class: "analysis-axis",
      }, `${fmt(data.duration_s * fraction)} s`));
      let path = "";
      let continuing = false;
      for (const bin of data.bins) {
        const value = bin[spec.field];
        if (!finite(value)) { continuing = false; continue; }
        const start = x(bin.start_s), end = x(bin.end_s);
        if (spec.field === "rate_hz") {
          svg.append(svgNode("rect", { x: start, y: y(value), width: Math.max(0, end - start - .4), height: Math.max(0, bottom - y(value)), class: "analysis-bar" }));
        } else {
          path += `${continuing ? "L" : "M"}${start},${y(value)} L${end},${y(value)} `;
          continuing = true;
        }
      }
      if (path) svg.append(svgNode("path", { d: path, class: "analysis-line" }));
      spec.marker = svgNode("line", { y1: top, y2: bottom, class: "analysis-marker" });
      svg.append(spec.marker);
      spec.plot = { left, right, width };
    }
    renderCursor();
  }

  function renderCursor() {
    const bin = data?.bins[binIndex];
    cursorControls.hidden = !bin;
    if (!bin) return;
    cursor.max = String(data.bins.length - 1);
    cursor.value = String(binIndex);
    cursor.disabled = data.bins.length < 2;
    setText(readout, `${fmt(bin.start_s)} → ${fmt(bin.end_s)} s · ${fmt(bin.events)} messages · ${bin.rate_hz === null ? "cadence inconnue" : `${fmt(bin.rate_hz)} Hz`} · ${bin.max_age_s === null ? "âge de réception inconnu" : `âge maximal ${fmt(bin.max_age_s)} s`}`);
    cursor.setAttribute("aria-valuetext", readout.textContent);
    for (const spec of chartSpecs) if (spec.marker && spec.plot && data.duration_s > 0) {
      const middle = (bin.start_s + bin.end_s) / 2;
      const position = spec.plot.left + (spec.plot.right - spec.plot.left) * middle / data.duration_s;
      spec.marker.setAttribute("x1", position);
      spec.marker.setAttribute("x2", position);
    }
  }

  function render() {
    if (!data) return;
    results.hidden = false;
    setText(summaryNodes.events, fmt(data.events));
    setText(summaryNodes.mean, data.summary.mean_rate_hz === null ? "Non définie" : `${fmt(data.summary.mean_rate_hz)} Hz`);
    setText(summaryNodes.gap, data.summary.max_gap_s === null ? "Inconnue" : `${fmt(data.summary.max_gap_s)} s`);
    setText(gapsNote, data.gap_count ? `${fmt(data.gap_count)} périodes d’au moins ${fmt(data.gap_threshold_s)} s.${data.gaps_truncated ? ` Les ${data.gaps.length} plus longues sont affichées.` : " De la plus longue à la plus courte."}` : `Aucune période sans réception d’au moins ${fmt(data.gap_threshold_s)} s pour ces filtres.`);
    gapRows.replaceChildren();
    const boundaries = { start: "Avant la première réception", end: "Après la dernière réception", interior: "Entre deux réceptions", whole: "Capture entière · aucun message filtré" };
    for (const gap of data.gaps) {
      const row = make("tr");
      row.append(make("td", "", `${fmt(gap.start_s)} → ${fmt(gap.end_s)} s`), make("td", "", `${fmt(gap.duration_s)} s`), make("td", "", boundaries[gap.boundary]));
      gapRows.append(row);
    }
    gapsTable.hidden = data.gaps.length === 0;
    drawCharts();
  }

  async function load(force = false) {
    if (!active()) return;
    const selected = selection();
    if (!selected) { cancel(); clear("Le seuil doit être compris entre 0,001 et 86 400 secondes."); return; }
    const key = JSON.stringify([metadata.id, metadata.revision, selected]);
    if (!force && queryKey === key && (request || data)) return;
    cancel();
    clear("Calcul de l’analyse du journal…");
    queryKey = key;
    const token = epoch;
    const controller = request = new AbortController();
    root.setAttribute("aria-busy", "true");
    const parameters = new URLSearchParams({ revision: metadata.revision, bins: String(aggregationBins(metadata.duration_s)), gap_threshold_s: String(selected.gap_threshold_s) });
    for (const name of ["system", "component", "message_id"]) if (selected[name] !== null) parameters.set(name, String(selected[name]));
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/recordings/${metadata.id}/analysis?${parameters}`, { signal: controller.signal, cache: "no-store" });
      const value = await response.json();
      if (token !== epoch || !active()) return;
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "L’analyse n’a pas pu être ouverte.");
      if (!valid(value, selected)) throw new Error("Réponse d’analyse invalide. Aucun résultat affiché.");
      data = value;
      binIndex = 0;
      fillMenus(value);
      const width = value.duration_s / value.bins.length;
      setText(status, `${fmt(value.duration_s)} s de capture · ${value.bins.length} intervalle${value.bins.length > 1 ? "s" : ""} ${width > 0 ? `d’environ ${fmt(width)} s` : "de durée nulle"} · temps relatif au début du journal.`);
      render();
    } catch (error) {
      if (token !== epoch || !active()) return;
      clear(error.name === "AbortError" ? "L’analyse ne répond pas. Vous pouvez réessayer." : error.message);
      retry.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (token === epoch) { request = null; root.setAttribute("aria-busy", "false"); }
    }
  }

  filters.addEventListener("submit", (event) => { event.preventDefault(); void load(); });
  source.addEventListener("change", () => void load());
  type.addEventListener("change", () => void load());
  threshold.addEventListener("input", () => { cancel(); clear(selection() ? "Appliquer le seuil pour recalculer les périodes sans réception." : "Le seuil doit être compris entre 0,001 et 86 400 secondes."); });
  retry.addEventListener("click", () => void load(true));
  cursor.addEventListener("input", () => { binIndex = Number(cursor.value); renderCursor(); });
  for (const spec of chartSpecs) spec.svg.addEventListener("pointermove", (event) => {
    if (!data?.bins.length || !spec.plot) return;
    const rectangle = spec.svg.getBoundingClientRect();
    if (!rectangle.width) return;
    const position = (event.clientX - rectangle.left) * spec.plot.width / rectangle.width;
    const fraction = Math.max(0, Math.min(1, (position - spec.plot.left) / (spec.plot.right - spec.plot.left)));
    const time = fraction * data.duration_s;
    binIndex = data.bins.findIndex((bin) => time < bin.end_s);
    if (binIndex < 0) binIndex = data.bins.length - 1;
    renderCursor();
  });
  document.addEventListener("argos:archive-analysis", (event) => {
    const next = event.detail || {};
    const candidate = next.metadata;
    const validMetadata = candidate && identifier(candidate.id) && revision(candidate.revision) && nonnegative(candidate.duration_s);
    const changed = (validMetadata ? `${candidate.id}:${candidate.revision}` : null) !== (metadata ? `${metadata.id}:${metadata.revision}` : null);
    const wasVisible = visible;
    visible = Boolean(next.visible);
    metadata = validMetadata ? candidate : null;
    if (changed) {
      cancel(); clear(); menuKey = null; source.replaceChildren(new Option("Toutes les sources", "")); type.replaceChildren(new Option("Tous les types", ""));
      contextDetails.open = false;
      renderContext();
    }
    if (!visible || !metadata) { cancel(); return; }
    if (changed || !wasVisible) void load(true);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancel();
    else if (active() && !request) void load(true);
  });
  new ResizeObserver(() => {
    if (resizeFrame !== null) return;
    resizeFrame = requestAnimationFrame(() => { resizeFrame = null; drawCharts(); });
  }).observe(root);
  renderContext();
})();
