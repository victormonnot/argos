/* Recorded framing observations. Reading and seeking never issue flight commands. */
(() => {
  "use strict";
  const details = document.getElementById("framing-report");
  const root = document.getElementById("framing-report-root");
  if (!details || !root) return;
  const finite = n => typeof n === "number" && Number.isFinite(n);
  const nonnegative = n => finite(n) && n >= 0;
  const integer = n => Number.isSafeInteger(n) && n >= 0;
  const hash = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const format = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const fmt = n => finite(n) ? format.format(n) : "Unknown";
  const ticks = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 3 });
  const compactTicks = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 3, notation: "compact" });
  const tick = n => (Math.abs(n) >= 10000 ? compactTicks : ticks).format(n);
  const make = (tag, className = "", text = null) => {
    const element = document.createElement(tag); element.className = className;
    if (text !== null) element.textContent = text;
    return element;
  };
  const svg = (tag, attributes = {}, text = null) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== null) element.textContent = text;
    return element;
  };
  const labels = { manual: "Manual", full: "Full framing", pilot_throttle: "Manual-throttle framing", unknown_profile: "Framing · profile unknown", paused: "Framing paused", takeover: "Manual takeover requested", inactive: "Assistance inactive", unknown: "Unobserved / unknown" };
  const profileLabel = profile => ({ full: "Full framing", pilot_throttle: "Manual throttle" })[profile] || "Profile not recorded";
  const responseLabel = response => ({ gentle: "Gentle", normal: "Normal", responsive: "Responsive" })[response] || "Response not recorded";
  const status = make("p", "framing-report-status"); status.id = "framing-report-status"; status.setAttribute("role", "status");
  const retry = make("button", "button button-outline", "Retry report"); retry.type = "button"; retry.hidden = true;
  const results = make("div", "framing-report-results"); results.hidden = true;
  const summary = make("dl", "framing-report-summary");
  const coverage = make("p", "microcopy"); coverage.id = "framing-report-coverage";
  const scope = make("p", "microcopy", "Sampled service observations, not proof of executed motion or correct target identity. Image size is relative, not distance in meters. Vertical centering is not scored; the pilot controls altitude in manual-throttle framing.");
  const timelineTitle = make("h3", "", "Assistance timeline");
  const timeline = svg("svg", { class: "framing-report-timeline", role: "img", "aria-label": "Recorded assistance states over time. Use the report time control or interval list to inspect." });
  const timelineNote = make("p", "microcopy", "Select the timeline or a curve to seek the recorded flight. Blank curve sections have no valid measurement. Changes of target, profile, response or reference start a new curve segment.");
  const plots = make("div", "framing-report-plots");
  const specs = [
    { field: "error_x", title: "Horizontal centering", unit: "% of half image width", note: "Zero is centered; negative is left, positive is right. ±100% reaches an image edge.", scale: 100 },
    { field: "size_error", title: "Apparent size vs reference", unit: "% of reference height", note: "Zero matches the requested size. Positive means larger in the image; negative means smaller. Closer / Farther changes the reference.", scale: 100 }
  ];
  for (const spec of specs) {
    const figure = make("figure", "framing-report-plot");
    spec.svg = svg("svg", { role: "img", "aria-label": `${spec.title} over time, ${spec.unit}.` });
    spec.empty = make("p", "microcopy");
    figure.append(make("figcaption", "", spec.title), spec.svg, spec.empty, make("p", "microcopy", spec.note));
    plots.append(figure);
  }
  const inspector = make("div", "framing-report-inspector");
  const timeLabel = make("label", "", "Inspect report time"); timeLabel.htmlFor = "framing-report-time";
  const time = make("input"); Object.assign(time, { id: "framing-report-time", type: "range", min: "0", step: "any", value: "0" });
  const readout = make("p", "framing-report-readout"); readout.id = "framing-report-readout";
  const view = make("button", "button button-outline", "View this moment in replay"); view.type = "button";
  inspector.append(timeLabel, time, readout, view);
  const intervalsDetails = make("details", "framing-report-intervals");
  intervalsDetails.append(make("summary", "", "Recorded intervals and changes"));
  const intervalNote = make("p", "microcopy");
  const intervalList = make("ol", "framing-report-list"); intervalList.id = "framing-report-interval-list";
  const more = make("button", "button button-outline", "Show more intervals"); more.type = "button";
  intervalsDetails.append(intervalNote, intervalList, more);
  const eventDetails = make("details", "framing-report-event-details");
  eventDetails.append(make("summary", "", "Session events and interruptions"));
  const eventNote = make("p", "microcopy");
  const eventList = make("ol", "framing-report-list"); eventList.id = "framing-report-event-list";
  eventDetails.append(eventNote, eventList);
  results.append(summary, coverage, scope, timelineTitle, timeline, timelineNote, plots, inspector, intervalsDetails, eventDetails);
  root.append(status, retry, results);

  let metadata = null, visible = false, data = null, request = null, key = null, epoch = 0, inspected = 0, cursor = 0, shownIntervals = 0, failed = false, drawnWidth = 0;
  const active = () => visible && details.open && !document.hidden;
  const ready = () => metadata?.visual?.state === "complete" && hash(metadata.visual.revision);
  const binding = () => ready() ? `${metadata.id}:${metadata.revision}:${metadata.visual.revision}` : null;
  function cancel() { epoch += 1; request?.abort(); request = null; root.setAttribute("aria-busy", "false"); }
  function clear(message = "") { data = null; results.hidden = true; retry.hidden = true; status.textContent = message; }
  const contextValid = value => [null, "full", "pilot_throttle"].includes(value.profile)
    && [null, "gentle", "normal", "responsive"].includes(value.range_response)
    && (value.target_id === null || (integer(value.target_id) && value.target_id > 0))
    && (value.reference_height === null || (finite(value.reference_height) && value.reference_height > 0 && value.reference_height <= 1));
  function valid(value, expected) {
    if (!value || value.state !== "complete" || value.id !== expected.id || value.revision !== expected.revision || value.visual_revision !== expected.visual.revision
      || (!nonnegative(value.duration_s) || Math.abs(value.duration_s - expected.duration_s) > 1e-6) || !value.coverage || !["sampled_s", "unknown_s", "metric_s"].every(k => nonnegative(value.coverage[k]) && value.coverage[k] <= value.duration_s + 1e-6)
      || !integer(value.coverage.samples) || !value.durations_s || !Object.keys(labels).every(k => nonnegative(value.durations_s[k]) && value.durations_s[k] <= value.duration_s + 1e-6)
      || !Array.isArray(value.intervals) || value.intervals.length > 1500 || !Array.isArray(value.series) || value.series.length > 1200
      || !Array.isArray(value.events) || value.events.length > 500
      || !value.limits || !nonnegative(value.limits.sample_age_s) || value.limits.sample_age_s > 1
      || !integer(value.intervals_count) || !integer(value.series_count) || !integer(value.events_count)
      || typeof value.intervals_truncated !== "boolean" || typeof value.series_downsampled !== "boolean" || typeof value.events_truncated !== "boolean") return false;
    let previous = -1;
    if (!value.intervals.every(item => {
      const ok = item && nonnegative(item.start_s) && item.start_s >= previous && nonnegative(item.end_s) && item.end_s >= item.start_s && item.end_s <= value.duration_s + 1e-6
        && Object.hasOwn(labels, item.state) && contextValid(item) && (item.reason === null || typeof item.reason === "string" && item.reason.length <= 4096);
      previous = item?.end_s; return ok;
    })) return false;
    previous = -1;
    if (!value.series.every(item => {
      const ok = item && nonnegative(item.at_s) && item.at_s >= previous && item.at_s <= value.duration_s && typeof item.valid === "boolean" && integer(item.segment)
        && contextValid(item) && ["error_x", "error_y", "height", "size_error"].every(k => item[k] === null || finite(item[k]))
        && (!item.valid || [item.error_x, item.size_error, item.height, item.reference_height].every(finite));
      previous = item?.at_s; return ok;
    })) return false;
    previous = -1;
    return value.events.every(item => {
      const ok = item && nonnegative(item.at_s) && item.at_s >= previous && item.at_s <= value.duration_s && typeof item.kind === "string" && item.kind.length <= 100
        && typeof item.detail === "string" && item.detail.length <= 4096 && (item.status === null || typeof item.status === "string");
      previous = item?.at_s; return ok;
    });
  }
  function context(item) {
    return `${profileLabel(item.profile)} · ${responseLabel(item.range_response)} · ${item.target_id === null ? "No target recorded" : `Person #${item.target_id}`} · reference ${finite(item.reference_height) ? `${fmt(item.reference_height * 100)}% of image height` : "not recorded"}`;
  }
  function seek(at) {
    if (!active() || !data) return;
    document.dispatchEvent(new CustomEvent("argos:archive-framing-seek", { detail: { id: data.id, revision: data.revision, visual_revision: data.visual_revision, at_s: at } }));
  }
  function inspect(at) {
    if (!data) return;
    inspected = Math.max(0, Math.min(data.duration_s, at)); time.value = String(inspected);
    const interval = data.intervals.find(item => item.start_s <= inspected && (item.end_s > inspected || inspected === data.duration_s && item.end_s === inspected));
    const point = data.series.reduce((closest, item) => Math.abs(item.at_s - inspected) < Math.abs((closest?.at_s ?? Infinity) - inspected) ? item : closest, null);
    let text = `+${fmt(inspected)} s · ${interval ? labels[interval.state] : "Interval not included in this report"}`;
    if (interval) text += `. ${context(interval)}${interval.reason ? `. ${interval.reason}` : ""}`;
    if (point && point.valid && Math.abs(point.at_s - inspected) <= data.limits.sample_age_s) text += `. Measurement at +${fmt(point.at_s)} s: horizontal ${fmt(point.error_x * 100)}%, size error ${fmt(point.size_error * 100)}%`;
    else text += ". No nearby valid plotted measurement";
    readout.textContent = text;
    time.setAttribute("aria-valuetext", `At ${fmt(inspected)} seconds. ${interval ? labels[interval.state] : "Unobserved"}`);
    for (const element of root.querySelectorAll(".framing-report-inspect-line")) {
      const width = element.ownerSVGElement.viewBox.baseVal.width;
      element.setAttribute("x1", String(64 + inspected / Math.max(data.duration_s, 1e-12) * (width - 86)));
      element.setAttribute("x2", element.getAttribute("x1"));
    }
  }
  function configurePointer(element) {
    element.addEventListener("click", event => {
      if (!data || !active()) return;
      const bounds = element.getBoundingClientRect();
      const width = element.viewBox.baseVal.width;
      const at = Math.max(0, Math.min(1, ((event.clientX - bounds.left) / bounds.width * width - 64) / (width - 86))) * data.duration_s;
      inspect(at); seek(inspected);
    });
  }
  function chart(spec) {
    const W = Math.max(180, Math.round(spec.svg.getBoundingClientRect().width)), H = 172, left = 64, right = W - 22, top = 16, bottom = 132;
    const points = data.series.filter(item => item.valid && finite(item[spec.field]));
    const extent = Math.max(spec.field === "error_x" ? 10 : 5, ...points.map(item => Math.abs(item[spec.field] * spec.scale))) * 1.1;
    const x = at => left + at / Math.max(data.duration_s, 1e-12) * (right - left);
    const y = value => (top + bottom) / 2 - value * spec.scale / extent * (bottom - top) / 2;
    spec.svg.setAttribute("viewBox", `0 0 ${W} ${H}`); spec.svg.replaceChildren();
    for (const level of [-extent, 0, extent]) {
      const ypos = (top + bottom) / 2 - level / extent * (bottom - top) / 2;
      spec.svg.append(svg("line", { x1: left, x2: right, y1: ypos, y2: ypos, class: level === 0 ? "framing-report-zero" : "framing-report-grid" }), svg("text", { x: left - 7, y: ypos + 4, "text-anchor": "end" }, `${tick(level)}%`));
    }
    for (const at of [0, data.duration_s / 2, data.duration_s]) spec.svg.append(svg("text", { x: x(at), y: 154, "text-anchor": "middle" }, `${fmt(at)} s`));
    let segment = null, path = "", count = 0, last = null;
    const flush = () => {
      if (count === 1) spec.svg.append(svg("circle", { cx: x(last.at_s), cy: y(last[spec.field]), r: 2.5, class: "framing-report-line" }));
      else if (count) spec.svg.append(svg("path", { d: path, class: "framing-report-line" }));
      path = ""; count = 0;
    };
    for (const item of data.series) {
      if (!item.valid || !finite(item[spec.field])) { flush(); segment = null; continue; }
      if (segment !== item.segment) flush();
      path += `${count ? "L" : "M"}${x(item.at_s)} ${y(item[spec.field])} `; count += 1; last = item; segment = item.segment;
    }
    flush();
    spec.svg.append(svg("line", { class: "framing-report-inspect-line", x1: x(inspected), x2: x(inspected), y1: top, y2: bottom }));
    spec.empty.textContent = points.length ? `${points.length} valid plotted observations${data.series_downsampled ? ` · reduced from ${fmt(data.series_count)} samples; interval extrema retained` : ""} · ${spec.unit}` : "No valid recorded measurements for this curve.";
  }
  function renderIntervals() {
    const end = Math.min(data.intervals.length, shownIntervals + 30);
    for (const item of data.intervals.slice(shownIntervals, end)) {
      const row = make("li"), button = make("button"); button.type = "button"; button.dataset.state = item.state;
      button.append(make("span", "framing-report-list-time", `+${fmt(item.start_s)}–${fmt(item.end_s)} s`), make("strong", "", labels[item.state]), make("span", "", context(item)), make("span", "", item.reason || ""));
      button.addEventListener("click", () => { inspect(item.start_s); seek(item.start_s); }); row.append(button); intervalList.append(row);
    }
    shownIntervals = end; more.hidden = shownIntervals >= data.intervals.length;
  }
  function drawPlots() {
    if (!data || !active()) return;
    const width = Math.max(180, Math.round(timeline.getBoundingClientRect().width));
    const x = at => 64 + at / Math.max(data.duration_s, 1e-12) * (width - 86);
    timeline.setAttribute("viewBox", `0 0 ${width} 46`); timeline.replaceChildren();
    for (const item of data.intervals) {
      const rect = svg("rect", { x: x(item.start_s), y: 6, width: x(item.end_s) - x(item.start_s), height: 20, "data-state": item.state });
      rect.append(svg("title", {}, `+${fmt(item.start_s)}–${fmt(item.end_s)} s · ${labels[item.state]} · ${context(item)}`)); timeline.append(rect);
    }
    for (const at of [0, data.duration_s / 2, data.duration_s]) timeline.append(svg("text", { x: x(at), y: 42, "text-anchor": "middle" }, `${fmt(at)} s`));
    timeline.append(svg("line", { class: "framing-report-inspect-line", x1: x(inspected), x2: x(inspected), y1: 2, y2: 29 }));
    for (const spec of specs) chart(spec);
    drawnWidth = root.getBoundingClientRect().width;
  }
  function render() {
    results.hidden = false;
    summary.replaceChildren();
    for (const [state, label] of Object.entries(labels)) {
      const row = make("div"); row.dataset.state = state;
      row.append(make("dt", "", label), make("dd", "", `${fmt(data.durations_s[state])} s`)); summary.append(row);
    }
    coverage.textContent = `${fmt(data.coverage.sampled_s)} s of sampled control coverage · ${fmt(data.coverage.unknown_s)} s unobserved / unknown · ${fmt(data.coverage.metric_s)} s with valid framing measurements · ${fmt(data.coverage.samples)} samples. Durations hold an observation for at most ${fmt(data.limits.sample_age_s)} s; recording gaps remain unknown.`;
    drawPlots();
    time.max = String(data.duration_s); time.disabled = data.duration_s === 0;
    intervalList.replaceChildren(); shownIntervals = 0; renderIntervals();
    intervalNote.textContent = `${data.intervals.length} intervals shown${data.intervals_truncated ? ` of ${fmt(data.intervals_count)}; later intervals omitted` : ""}. A new interval preserves each recorded state, target, profile, response, reference or reason change.`;
    eventList.replaceChildren();
    for (const event of data.events) {
      const row = make("li"), button = make("button"); button.type = "button";
      button.append(make("span", "framing-report-list-time", `+${fmt(event.at_s)} s`), make("strong", "", event.kind.replaceAll("_", " ").replaceAll(".", " ")), make("span", "", event.detail), make("span", "microcopy", ({ accepted: "Request accepted", refused: "Request refused", sampled: "Sampled observation" })[event.status] || event.status));
      button.addEventListener("click", () => { inspect(event.at_s); seek(event.at_s); }); row.append(button); eventList.append(row);
    }
    eventNote.textContent = data.events.length ? `${data.events_truncated ? `Latest ${data.events.length} of ${fmt(data.events_count)}` : data.events.length} recorded session / control events. Select an event to seek the flight. Requests do not prove execution.` : "No framing or interruption events recorded. Interval reasons remain available above.";
    inspect(cursor); results.hidden = false; status.textContent = data.coverage.samples ? "" : "No control samples in this recording. Video may still be available.";
  }
  async function load() {
    if (!active() || !ready() || data || request || failed) return;
    cancel(); const token = epoch, expected = metadata, expectedKey = binding();
    const controller = request = new AbortController();
    root.setAttribute("aria-busy", "true"); status.textContent = "Reading recorded framing observations…"; retry.hidden = true;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const query = new URLSearchParams({ revision: expected.revision, visual_revision: expected.visual.revision });
      const response = await fetch(`/api/recordings/${expected.id}/framing-report?${query}`, { signal: controller.signal, cache: "no-store" });
      const value = await response.json();
      if (token !== epoch || expectedKey !== binding() || !active()) return;
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "The framing report could not be read.");
      if (!valid(value, expected)) throw new Error("Invalid framing report response. Reopen the recording to retry.");
      data = value; render();
    } catch (error) {
      if (token !== epoch || !active()) return;
      clear(error.name === "AbortError" ? "The framing report did not respond in time." : error.message); failed = true; retry.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (token === epoch) { request = null; root.setAttribute("aria-busy", "false"); }
    }
  }
  function update() {
    if (!active()) { cancel(); return; }
    if (!ready()) { clear("No verified visual archive is available for a framing report."); return; }
    void load();
  }
  document.addEventListener("argos:archive-framing-report", event => {
    metadata = event.detail.metadata; visible = event.detail.visible; cursor = event.detail.cursor;
    const next = binding();
    if (next !== key) { cancel(); clear(); key = next; failed = false; }
    update();
  });
  details.addEventListener("toggle", update);
  retry.addEventListener("click", () => { failed = false; void load(); });
  more.addEventListener("click", renderIntervals);
  time.addEventListener("input", () => inspect(Number(time.value)));
  view.addEventListener("click", () => seek(inspected));
  configurePointer(timeline); for (const spec of specs) configurePointer(spec.svg);
  new ResizeObserver(() => {
    if (data && active() && Math.abs(root.getBoundingClientRect().width - drawnWidth) > .5) drawPlots();
  }).observe(root);
})();
