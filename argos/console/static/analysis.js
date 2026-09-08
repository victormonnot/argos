/* Historical reception analysis. All network requests are read-only. */
(() => {
  "use strict";
  const root = document.getElementById("recording-analysis-root");
  if (!root) return;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const nonnegative = (value) => finite(value) && value >= 0;
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const nullable = (value) => value === null || nonnegative(value);
  const format = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 5 });
  const fmt = (value) => finite(value) ? format.format(value) : "Unknown";
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
  filters.setAttribute("aria-label", "Recording analysis filters");
  const sourceLabel = make("label", "", "Recorded source");
  const source = make("select");
  source.id = "analysis-source";
  source.append(new Option("All sources", ""));
  sourceLabel.append(source);
  const typeLabel = make("label", "", "Message type");
  const type = make("select");
  type.id = "analysis-type";
  type.append(new Option("All types", ""));
  typeLabel.append(type);
  const thresholdLabel = make("label", "", "Reception gap threshold (s)");
  const thresholdRow = make("span", "analysis-threshold-row");
  const threshold = make("input");
  threshold.id = "analysis-gap-threshold";
  threshold.type = "number";
  threshold.min = ".001";
  threshold.max = "86400";
  threshold.step = "any";
  threshold.value = "1";
  threshold.required = true;
  const apply = make("button", "button button-outline", "Apply");
  apply.type = "submit";
  thresholdRow.append(threshold, apply);
  thresholdLabel.append(thresholdRow);
  filters.append(sourceLabel, typeLabel, thresholdLabel);
  const status = make("p", "analysis-status");
  status.id = "analysis-status";
  status.setAttribute("role", "status");
  const retry = make("button", "button button-outline", "Retry analysis");
  retry.type = "button";
  retry.hidden = true;
  const results = make("div", "analysis-results");
  results.hidden = true;
  const summary = make("dl", "analysis-summary");
  const summaryNodes = {};
  for (const [name, label] of [["events", "Filtered messages"], ["mean", "Mean rate"], ["gap", "Longest reception gap"]]) {
    const row = make("div");
    const output = summaryNodes[name] = make("dd");
    row.append(make("dt", "", label), output);
    summary.append(row);
  }
  const charts = make("div", "analysis-charts");
  const chartSpecs = [
    { field: "rate_hz", title: "Reception rate", unit: "Hz", className: "analysis-rate", description: "Messages received per second, averaged within each interval." },
    { field: "max_age_s", title: "Maximum reception age", unit: "s", className: "analysis-age", description: "Time since the last filtered reception, at the maximum within each interval; age is unknown before the first reception. This is not sensor measurement age." },
  ];
  for (const spec of chartSpecs) {
    spec.figure = make("figure", `analysis-chart ${spec.className}`);
    const heading = make("figcaption", "", spec.title);
    spec.svg = svgNode("svg", { role: "img", "aria-label": `${spec.title}, in ${spec.unit}, over time since capture start.` });
    spec.empty = make("p", "analysis-chart-empty");
    const description = make("p", "analysis-note", spec.description);
    spec.figure.append(heading, spec.svg, spec.empty, description);
    charts.append(spec.figure);
  }
  const cursorControls = make("div", "analysis-cursor");
  const cursorLabel = make("label", "", "Inspect an interval");
  cursorLabel.htmlFor = "analysis-bin-cursor";
  const cursor = make("input");
  cursor.type = "range";
  cursor.id = "analysis-bin-cursor";
  cursor.min = "0";
  cursor.step = "1";
  const readout = make("p", "analysis-bin-readout");
  cursorControls.append(cursorLabel, cursor, readout);
  const gapsSection = make("section", "analysis-gaps");
  const gapsTitle = make("h3", "", "Reception gaps");
  const gapsNote = make("p", "analysis-note");
  const distinction = make("p", "analysis-note", "A reception gap does not prove packet loss.");
  const gapsTable = make("table", "analysis-gap-table");
  const tableHead = make("thead");
  const headers = make("tr");
  for (const label of ["Within capture", "Duration", "Position"]) {
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
  contextDetails.append(make("summary", "", "Context recorded during capture"));
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
    source.replaceChildren(new Option("All sources", ""));
    for (const item of value.sources) source.append(new Option(`System ${item.system} · component ${item.component}`, `${item.system}:${item.component}`));
    type.replaceChildren(new Option("All types", ""));
    for (const item of value.message_types) type.append(new Option(`${item.type_name ? `${item.type_name} · ` : "Message "}${item.message_id} · ${fmt(item.events)} received`, String(item.message_id)));
    source.value = sourceValue;
    type.value = typeValue;
  }

  function renderContext() {
    contextFields.replaceChildren();
    const recorded = metadata?.context_status === "recorded" && metadata.context;
    if (!recorded) {
      setText(contextStatus, metadata?.context_status === "unavailable" ? "This recording contains no usable capture context." : "Capture context is unknown for this recording.");
      return;
    }
    setText(contextStatus, "Settings saved with the recording. The filters and threshold above apply to this analysis.");
    const configuration = recorded.configuration || {};
    const entries = [
      ["Capture start · UTC", recorded.captured_at_utc], ["Observation", recorded.run_id],
      ["Environment", ({ simulation: "Reported as simulation", real: "Reported as real", unconfigured: "Not configured" })[configuration.environment] || configuration.environment],
      ["Received link", recorded.telemetry_endpoint], ["MAVLink transport", configuration.mavlink_transport],
      ["Measurement source", configuration.system == null ? null : `${configuration.system} / ${configuration.component}`],
      ["Sequence counting", configuration.sequence_scope],
      ["Video source", configuration.video_source], ["Video endpoint", configuration.video_endpoint],
      ["Adresse UDP locale", configuration.mavlink_bind], ["Correspondant UDP", configuration.mavlink_peer],
      ["TCP address", configuration.mavlink_tcp], ["Serial port", configuration.mavlink_device],
      ["Baud rate", configuration.mavlink_transport === "serial" ? configuration.baudrate : null],
    ];
    const names = { video: "image", heartbeat: "reported mode", attitude: "attitude", local_position_ned: "position", battery: "battery" };
    for (const [name, value] of Object.entries(recorded.age_limits_s || {})) entries.push([`Reception threshold · ${names[name] || name}`, finite(value) ? `${fmt(value)} s` : value]);
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
      setText(spec.empty, data.duration_s === 0 ? "Zero duration: this chart is undefined." : spec.field === "max_age_s" ? "No filtered reception available to calculate this age." : "No rate interval available.");
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
    setText(readout, `${fmt(bin.start_s)} → ${fmt(bin.end_s)} s · ${fmt(bin.events)} messages · ${bin.rate_hz === null ? "unknown rate" : `${fmt(bin.rate_hz)} Hz`} · ${bin.max_age_s === null ? "unknown reception age" : `maximum age ${fmt(bin.max_age_s)} s`}`);
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
    setText(summaryNodes.mean, data.summary.mean_rate_hz === null ? "Undefined" : `${fmt(data.summary.mean_rate_hz)} Hz`);
    setText(summaryNodes.gap, data.summary.max_gap_s === null ? "Unknown" : `${fmt(data.summary.max_gap_s)} s`);
    setText(gapsNote, data.gap_count ? `${fmt(data.gap_count)} gaps of at least ${fmt(data.gap_threshold_s)} s.${data.gaps_truncated ? ` Showing the ${data.gaps.length} longest gaps.` : " Longest to shortest."}` : `No reception gap of at least ${fmt(data.gap_threshold_s)} s for these filters.`);
    gapRows.replaceChildren();
    const boundaries = { start: "Before the first reception", end: "After the last reception", interior: "Between two receptions", whole: "Entire capture · no filtered messages" };
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
    if (!selected) { cancel(); clear("The threshold must be between 0.001 and 86,400 seconds."); return; }
    const key = JSON.stringify([metadata.id, metadata.revision, selected]);
    if (!force && queryKey === key && (request || data)) return;
    cancel();
    clear("Computing recording analysis…");
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
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Unable to open analysis.");
      if (!valid(value, selected)) throw new Error("Invalid analysis response. No results displayed.");
      data = value;
      binIndex = 0;
      fillMenus(value);
      const width = value.duration_s / value.bins.length;
      setText(status, `${fmt(value.duration_s)} s of capture · ${value.bins.length} interval${value.bins.length > 1 ? "s" : ""} ${width > 0 ? `approximately ${fmt(width)} s` : "with zero duration"} · time relative to recording start.`);
      render();
    } catch (error) {
      if (token !== epoch || !active()) return;
      clear(error.name === "AbortError" ? "Analysis is not responding. You can retry." : error.message);
      retry.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (token === epoch) { request = null; root.setAttribute("aria-busy", "false"); }
    }
  }

  filters.addEventListener("submit", (event) => { event.preventDefault(); void load(); });
  source.addEventListener("change", () => void load());
  type.addEventListener("change", () => void load());
  threshold.addEventListener("input", () => { cancel(); clear(selection() ? "Apply the threshold to recalculate reception gaps." : "The threshold must be between 0.001 and 86,400 seconds."); });
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
      cancel(); clear(); menuKey = null; source.replaceChildren(new Option("All sources", "")); type.replaceChildren(new Option("All types", ""));
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
