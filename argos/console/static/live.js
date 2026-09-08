/* Optional passive live inspection. It never sends a MAVLink or HTTP mutation. */
(() => {
  "use strict";
  const root = document.getElementById("live-messages-root");
  if (!root) return;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const fmt = (value) => finite(value) ? number.format(value) : "—";
  const key = (item) => `${item.system}:${item.component}:${item.message_id}`;
  const sourceKey = (item) => `${item.system}:${item.component}`;
  const setText = (element, value) => { if (element.textContent !== value) element.textContent = value; };
  function make(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = value;
    return element;
  }
  function button(value, className) {
    const element = make("button", className, value);
    element.type = "button";
    return element;
  }
  const heading = make("div", "live-heading");
  const identity = make("div", "live-identity");
  const title = make("h2", "live-title", "Decoded messages");
  title.id = "live-messages-title";
  const endpoint = make("p", "live-endpoint", "Link not configured");
  identity.append(title, endpoint);
  const actions = make("div", "live-actions");
  const freeze = button("Freeze", "button button-outline live-freeze");
  freeze.setAttribute("aria-pressed", "false");
  const returnButton = button("Back to Observation", "button button-outline live-return");
  returnButton.addEventListener("click", () => document.dispatchEvent(new Event("argos:show-observation")));
  actions.append(freeze, returnButton);
  heading.append(identity, actions);
  const status = make("p", "live-status", "Opening this view starts inspection.");
  status.setAttribute("role", "status");
  const mobileNavigation = make("div", "live-mobile-navigation");
  mobileNavigation.setAttribute("role", "group");
  mobileNavigation.setAttribute("aria-label", "Message view");
  const showTypes = button("Received types", "button button-outline");
  const showFields = button("View fields", "button button-outline");
  mobileNavigation.append(showTypes, showFields);
  const caption = make("p", "live-note", "This view shows all received sources before Observation measurement filtering. Fields and frame correspond to the last received message of the selected type.");
  const columns = make("div", "live-columns");
  const browser = make("section", "live-browser");
  browser.setAttribute("aria-label", "Received message types");
  const filters = make("div", "live-filters");
  const sourceLabel = make("label", "", "Received source");
  const source = make("select");
  source.id = "live-source-filter";
  source.append(new Option("All sources", "*"));
  sourceLabel.append(source);
  const searchLabel = make("label", "", "Type or ID");
  const search = make("input");
  search.id = "live-type-filter";
  search.type = "search";
  search.placeholder = "e.g. ATTITUDE, 30";
  search.maxLength = 80;
  search.autocomplete = "off";
  searchLabel.append(search);
  filters.append(sourceLabel, searchLabel);
  const listSummary = make("p", "live-list-summary");
  const list = make("div", "live-type-list");
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", "Received MAVLink types; select to view fields");
  const empty = make("p", "live-note live-empty", "No decoded messages.");
  const detailPane = make("div", "live-detail-pane");
  detailPane.tabIndex = 0;
  detailPane.setAttribute("role", "region");
  detailPane.setAttribute("aria-label", "Fields, frame and reception counters");
  const detailEmpty = make("p", "live-note live-detail-empty", "Fields will appear when a message arrives.");
  const selected = make("section", "live-selected");
  selected.setAttribute("aria-labelledby", "live-selected-title");
  selected.hidden = true;
  const selectedTitle = make("h3", "live-selected-title");
  selectedTitle.id = "live-selected-title";
  selectedTitle.tabIndex = -1;
  const selectedMeta = make("p", "live-note live-selected-meta");
  const payload = make("details", "live-payload");
  payload.open = true;
  payload.append(make("summary", "", "Decoded fields"));
  const fields = make("dl", "live-fields");
  payload.append(fields);
  const wire = make("details", "live-wire");
  wire.append(make("summary", "", "Hexadecimal frame"));
  const hex = make("pre", "live-hex");
  wire.append(hex);
  selected.append(selectedTitle, selectedMeta, payload, wire);
  const diagnostics = make("details", "live-diagnostics");
  diagnostics.append(make("summary", "", "Counters for this connection"));
  const counts = make("dl", "detail-list");
  const counterNodes = {};
  for (const [name, label] of [["rx_messages", "Decoded messages"], ["rx_bytes", "Received bytes"],
    ["bad_bytes", "Invalid bytes"], ["unsupported_frames", "Unsupported frames"],
    ["read_errors", "Read errors"]]) {
    const row = make("div");
    const value = counterNodes[name] = make("dd", "", "—");
    row.append(make("dt", "", label), value);
    counts.append(row);
  }
  const diagnosticNote = make("p", "live-note", "Invalid bytes are counted separately. Only decoded frames can be inspected in this view.");
  diagnostics.append(counts, diagnosticNote);
  const limits = make("p", "live-note live-limits");
  const explanation = make("details", "live-explanation");
  explanation.append(make("summary", "", "Understanding this data"), caption, limits);
  const toolbar = make("div", "live-toolbar");
  toolbar.append(heading, status, mobileNavigation);
  browser.append(filters, listSummary, list, empty);
  detailPane.append(detailEmpty, selected, diagnostics, explanation);
  columns.append(browser, detailPane);
  root.append(toolbar, columns);
  root.dataset.mobileView = "types";

  let context = { visible: false, serviceFresh: false, run_id: null, connection_id: null };
  let data = null;
  let chosen = null;
  let paused = false;
  let epoch = 0;
  let timer = null;
  let request = null;
  let error = "";
  let acceptedAt = 0;
  let progressedAt = 0;
  let transit = 0;
  let sourceSignature = "";
  const rows = new Map();
  const fieldRows = new Map();
  const observing = () => context.visible && document.body.dataset.view === "messages" && !document.hidden;
  const canFetch = () => observing() && context.serviceFresh && !paused && Boolean(context.run_id && context.connection_id);

  function cancel() {
    epoch += 1;
    clearTimeout(timer);
    timer = null;
    request?.abort();
    request = null;
  }

  function valid(value) {
    return value && typeof value.run_id === "string" && typeof value.connection_id === "string" && finite(value.at) &&
      typeof value.state === "string" && finite(value.window_s) && value.window_s > 0 &&
      finite(value.rate_resolution_s) && value.rate_resolution_s > 0 && integer(value.max_types) &&
      integer(value.evicted_types) && Array.isArray(value.types) && value.types.length <= value.max_types &&
      value.max_types <= 4096 && ["rx_messages", "rx_bytes", "bad_bytes", "unsupported_frames", "read_errors"].every((name) => integer(value[name])) &&
      value.types.every((item) => item && integer(item.system) && item.system <= 255 &&
        integer(item.component) && item.component <= 255 && integer(item.message_id) &&
        item.message_id <= 0xffffff && typeof item.type_name === "string" && item.type_name.length <= 128 &&
        integer(item.count) && integer(item.sequence) && item.sequence <= 255 &&
        finite(item.last_received_at) && item.last_received_at <= value.at && finite(item.rx_age_s) && item.rx_age_s >= 0 &&
        finite(item.hz) && item.hz >= 0 && item.fields && typeof item.fields === "object" && !Array.isArray(item.fields) &&
        typeof item.frame_hex === "string" && /^[0-9a-f]*$/i.test(item.frame_hex) &&
        integer(item.frame_bytes) && item.frame_bytes <= 280 && item.frame_hex.length === 2 * item.frame_bytes &&
        [1, 2].includes(item.wire_version));
  }

  function age(item) {
    if (!data) return null;
    return data.at - item.last_received_at + (paused ? 0 : Math.max(0, performance.now() - progressedAt) / 1000 + transit);
  }

  function fresh() {
    return context.serviceFresh && !error && data && performance.now() - acceptedAt < 1800 && performance.now() - progressedAt < 1800;
  }

  function updateSources() {
    if (!data) return;
    const choices = [...new Set(data.types.map(sourceKey))].sort((a, b) => {
      const first = a.split(":").map(Number), second = b.split(":").map(Number);
      return first[0] - second[0] || first[1] - second[1];
    });
    const signature = choices.join("|");
    if (sourceSignature === signature) return;
    sourceSignature = signature;
    const previous = source.value;
    source.replaceChildren(new Option("All sources", "*"));
    for (const choice of choices) source.append(new Option(`System ${choice.split(":")[0]} · component ${choice.split(":")[1]}`, choice));
    source.value = choices.includes(previous) ? previous : "*";
  }

  function rowFor(item) {
    const identity = key(item);
    if (rows.has(identity)) return rows.get(identity);
    const control = button("", "live-type");
    control.dataset.key = identity;
    const name = make("span", "live-type-name");
    const rate = make("span", "live-type-rate");
    const origin = make("span", "live-type-source");
    const ageLabel = make("span", "live-type-age");
    control.append(name, rate, origin, ageLabel);
    control.addEventListener("click", () => {
      const changed = chosen !== identity;
      chosen = identity;
      render();
      if (changed) detailPane.scrollTop = 0;
      if (matchMedia("(max-width:760px)").matches) {
        mobileView("fields");
        detailPane.scrollTop = 0;
        selectedTitle.focus({ preventScroll: true });
      }
    });
    const row = { control, name, rate, origin, ageLabel };
    rows.set(identity, row);
    list.append(control);
    return row;
  }

  function renderFields(item) {
    const names = new Set(Object.keys(item.fields));
    for (const [name, row] of fieldRows) if (!names.has(name)) { row.element.remove(); fieldRows.delete(name); }
    for (const [name, value] of Object.entries(item.fields)) {
      let row = fieldRows.get(name);
      if (!row) {
        const element = make("div");
        const output = make("dd");
        element.append(make("dt", "", name), output);
        fields.append(element);
        fieldRows.set(name, row = { element, output });
      }
      setText(row.output, typeof value === "string" ? value : JSON.stringify(value));
    }
  }

  function render() {
    setText(endpoint, context.endpoint || "Link not configured");
    freeze.disabled = !data;
    freeze.setAttribute("aria-pressed", String(paused));
    setText(freeze, paused ? "Resume" : "Freeze");
    if (paused && data) setText(status, `Snapshot frozen at t + ${fmt(data.at)} s. Only this display is frozen.${context.serviceFresh ? "" : " Local service unavailable."}`);
    else if (!context.serviceFresh) setText(status, "Local service unavailable. Last received fields remain viewable; rates are no longer refreshed.");
    else if (error) setText(status, error);
    else if (!data) setText(status, "Reading messages…");
    else if (!fresh()) setText(status, "Inspection not refreshed. Last received fields retained.");
    else if (data.state === "unconfigured") setText(status, "No MAVLink link configured.");
    else if (data.state === "reconnecting") setText(status, "Reopening link…");
    else if (data.state === "error") setText(status, "Link interrupted. Last received messages retained.");
    else if (!data.types.length) setText(status, "Waiting for the first decoded message.");
    else setText(status, "Live · latest reception of each type.");
    root.dataset.stale = String(!paused && !fresh());
    updateSources();
    const query = search.value.trim().toUpperCase();
    const items = data?.types || [];
    const shown = items.filter((item) => (source.value === "*" || sourceKey(item) === source.value) &&
      (!query || item.type_name.toUpperCase().includes(query) || String(item.message_id).includes(query)));
    const available = new Set(items.map(key));
    const visibleKeys = new Set(shown.map(key));
    if (!visibleKeys.has(chosen)) chosen = shown.length ? key(shown[0]) : null;
    for (const [identity, row] of rows) {
      if (!available.has(identity)) { row.control.remove(); rows.delete(identity); }
      else {
        row.control.hidden = !visibleKeys.has(identity);
        row.control.setAttribute("aria-pressed", String(identity === chosen));
      }
    }
    for (const item of shown) {
      const row = rowFor(item);
      row.control.hidden = false;
      row.control.setAttribute("aria-pressed", String(key(item) === chosen));
      setText(row.name, `${item.type_name} · ${item.message_id}`);
      const hz = paused ? item.hz : fresh() ? age(item) >= data.window_s ? 0 : item.hz : null;
      setText(row.rate, hz == null ? "— Hz" : `${hz > 0 ? "≈ " : ""}${fmt(hz)} Hz`);
      setText(row.origin, `${item.system} / ${item.component} · ${fmt(item.count)} received`);
      setText(row.ageLabel, `Dernier : ${fmt(age(item))} s`);
    }
    setText(listSummary, data ? `${shown.length} / ${items.length} types · system / component` : "");
    empty.hidden = shown.length > 0;
    setText(empty, items.length ? "No types match these filters." : "No decoded messages for this connection.");
    const item = items.find((entry) => key(entry) === chosen);
    selected.hidden = !item;
    detailEmpty.hidden = Boolean(item);
    setText(detailEmpty, items.length ? "Select a type in the list to inspect its fields." : "Fields will appear when a message arrives.");
    showFields.disabled = !item;
    if (item) {
      setText(selectedTitle, `${item.type_name} · ${item.message_id}`);
      setText(selectedMeta, `System ${item.system} · component ${item.component} · sequence ${item.sequence} · MAVLink ${item.wire_version} · ${item.frame_bytes} bytes\nReceived at t + ${fmt(item.last_received_at)} s${paused ? " · frozen snapshot" : ""}`);
      renderFields(item);
      setText(hex, item.frame_hex.match(/.{1,2}/g)?.join(" ") || "");
    }
    for (const [name, element] of Object.entries(counterNodes)) setText(element, data ? fmt(data[name]) : "—");
    if (data) setText(limits, `Approximate rate over ${fmt(data.window_s)} s, resolution ${fmt(data.rate_resolution_s)} s. ${data.max_types} source/type pairs maximum.${data.evicted_types ? ` ${data.evicted_types} evictions; the counter resets to zero when a type returns.` : ""} 64-bit integers and non-finite values may be displayed as text to preserve their value.`);
  }

  async function poll() {
    if (!canFetch() || request) return;
    const token = epoch;
    const controller = request = new AbortController();
    const started = performance.now();
    const timeout = setTimeout(() => controller.abort(), 1500);
    try {
      const response = await fetch("/api/mavlink/messages", { signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error("Inspection indisponible. Nouvelle tentative automatique.");
      const value = await response.json();
      if (token !== epoch || !canFetch()) return;
      if (!valid(value)) throw new Error("Invalid inspection response. Last received fields retained.");
      if (value.run_id !== context.run_id || value.connection_id !== context.connection_id) return;
      if (data && value.at < data.at) throw new Error("Inconsistent inspection clock. Last received fields retained.");
      acceptedAt = performance.now();
      if (!data || value.at > data.at) {
        progressedAt = acceptedAt;
        transit = (acceptedAt - started) / 1000;
        data = value;
      }
      error = "";
    } catch (cause) {
      if (token !== epoch || !canFetch()) return;
      error = cause.name === "AbortError" ? "Inspection not responding. Last received fields retained." : cause.message;
    } finally {
      clearTimeout(timeout);
      if (token === epoch) {
        request = null;
        render();
        if (canFetch()) timer = setTimeout(poll, Math.max(0, 500 - (performance.now() - started)));
      }
    }
  }

  function refreshActivity() {
    cancel();
    render();
    if (canFetch()) void poll();
  }

  function mobileView(view) {
    root.dataset.mobileView = view;
    showTypes.setAttribute("aria-pressed", String(view === "types"));
    showFields.setAttribute("aria-pressed", String(view === "fields"));
  }

  showTypes.addEventListener("click", () => mobileView("types"));
  showFields.addEventListener("click", () => {
    mobileView("fields");
    detailPane.scrollTop = 0;
  });
  mobileView("types");

  freeze.addEventListener("click", () => {
    paused = !paused;
    refreshActivity();
  });
  source.addEventListener("change", render);
  search.addEventListener("input", render);
  document.addEventListener("argos:live-inspector", (event) => {
    const next = event.detail || {};
    const changed = next.run_id !== context.run_id || next.connection_id !== context.connection_id;
    const activityChanged = changed || Boolean(next.visible) !== context.visible || Boolean(next.serviceFresh) !== context.serviceFresh;
    const endpointChanged = context.endpoint !== next.endpoint;
    context = { visible: Boolean(next.visible), serviceFresh: Boolean(next.serviceFresh), run_id: next.run_id, connection_id: next.connection_id, endpoint: next.endpoint };
    if (changed) {
      data = null;
      chosen = null;
      paused = false;
      error = "";
      sourceSignature = "";
      source.replaceChildren(new Option("All sources", "*"));
      mobileView("types");
    }
    if (activityChanged) refreshActivity();
    else if (endpointChanged) render();
  });
  document.addEventListener("visibilitychange", refreshActivity);
  let workspace = document.body.dataset.view;
  new MutationObserver(() => {
    const next = document.body.dataset.view;
    if (next !== workspace) { workspace = next; refreshActivity(); }
  }).observe(document.body, { attributes: true, attributeFilter: ["data-view"] });
  // Age and stale indicators continue to advance between HTTP samples. No HTTP
  // request runs while the view is closed, in Sessions, paused or tab-hidden.
  setInterval(() => { if (observing()) render(); }, 250);
  render();
})();
