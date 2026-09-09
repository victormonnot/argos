"use strict";

(() => {
  const STATE_INTERVAL_MS = 100;
  const FRAME_INTERVAL_MS = 125;
  const REQUEST_TIMEOUT_MS = 2000;
  const WATCHDOG_MS = 1500;
  const element = (id) => document.getElementById(id);
  const text = (id, value) => { const node = element(id); const next = String(value); if (node.textContent !== next) node.textContent = next; };
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const delay = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const ageText = (age) => finite(age) ? `${number.format(Math.max(0, age))} s` : "—";
  const numeric = (value, unit = "") => finite(value) ? `${number.format(value)}${unit}` : "—";
  const states = { absent: "Missing", recent: "Recent", stale: "Stale", unconfigured: "Not configured", waiting: "Waiting", receiving: "Receiving", error: "Error", reconnecting: "Reopening" };
  const bootLabels = { first: "First sample", advanced: "Advancing", repeated: "Repeated value", decreased: "Decreasing value" };
  let current = null;
  let lastReceived = null;
  let lastAdvance = null;
  let stateTransitMs = 0;
  let requestFailure = "";
  let imageFailure = "";
  let frame = null;
  let visionEnabled = false;
  let frameEpoch = 0;
  let visionSelection = { allowed: false, target_id: null, active: false, paused: false };
  let visionStateSignature = "";
  const visionIdentities = new WeakMap();
  let stopped = false;
  let mutation = null;
  let stateEpoch = 0;
  let sourcesDirty = false;
  let configurationSignature = null;
  const eventSignatures = new Map();
  let inspectorPanel = "telemetry";
  let telemetrySection = "mode";
  let inspectorOpener = null;
  let workspaceOpener = null;
  let panelChosen = false;
  let serviceLostAt = null;
  let serviceRecovery = null;
  let issueSignature = "";
  let pollResumeAt = null;
  const pollingAllowed = () => !mutation || mutation.startsWith("reconnect-");
  const expectedPause = (now = performance.now()) => !pollingAllowed() || (pollResumeAt !== null && now - pollResumeAt <= REQUEST_TIMEOUT_MS);

  function showWorkspace(view, { opener = null, restore = false } = {}) {
    if (!["observation", "control", "messages", "sessions"].includes(view)) return;
    const previous = document.body.dataset.view;
    if (previous === view) return;
    if (view === "messages") workspaceOpener = opener || document.activeElement;
    document.body.dataset.view = view;
    for (const [name, id] of [["observation", "workspace"], ["control", "workspace"], ["messages", "messages-workspace"], ["sessions", "sessions-workspace"]]) {
      element(id).hidden = id === "workspace" ? !["observation", "control"].includes(view) : name !== view;
      const button = element(`view-${name}`);
      if (name === view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    element("control-panel").hidden = view !== "control";
    const title = { observation: "Observation", control: "Flight controls", messages: "Live MAVLink", sessions: "Sessions" }[view];
    text("view-title", title);
    document.title = `ARGOS · ${title}`;
    text("view-context", { observation: "Incoming data", control: "Manual flight · GPS-free", messages: "Current stream inspection", sessions: "History · replay" }[view]);
    const skip = document.querySelector(".skip-link");
    skip.href = { observation: "#workspace", control: "#control-panel", messages: "#messages-workspace", sessions: "#sessions-workspace" }[view];
    skip.textContent = { observation: "Skip to observation", control: "Skip to flight controls", messages: "Skip to live messages", sessions: "Skip to sessions" }[view];
    document.dispatchEvent(new CustomEvent("argos:workspace-changed", { detail: { view, previous } }));
    renderPanel();
    if (view === "messages") element("messages-workspace").focus({ preventScroll: true });
    else if (restore && previous === "messages") {
      const target = workspaceOpener?.isConnected && workspaceOpener.getClientRects().length ? workspaceOpener : element("view-observation");
      target.focus({ preventScroll: true });
    }
  }

  function renderPanel() {
    const titles = { telemetry: "MAVLink telemetry", video: "Video", recording: "MAVLink recording", reception: "Reception status", sources: "Configure sources" };
    text("inspector-kind", inspectorPanel === "sources" ? "SETTINGS" : "INSPECTION");
    text("inspector-title", titles[inspectorPanel] || "Inspector");
    element("inspector-empty").hidden = inspectorPanel !== null;
    element("inspector-close").hidden = inspectorPanel === null;
    for (const node of document.querySelectorAll("[data-panel-content]")) node.hidden = node.dataset.panelContent !== inspectorPanel;
    for (const node of document.querySelectorAll("[data-telemetry-content]")) node.hidden = node.dataset.telemetryContent !== telemetrySection;
    for (const button of document.querySelectorAll("[data-panel]")) {
      const expanded = inspectorPanel === button.dataset.panel && document.body.dataset.view === "observation" && !document.body.classList.contains("focus-mode");
      button.setAttribute("aria-expanded", String(expanded));
      if (button.hasAttribute("aria-pressed")) button.setAttribute("aria-pressed", String(expanded && button.dataset.section === telemetrySection));
    }
    for (const button of document.querySelectorAll("[data-telemetry-section]")) button.setAttribute("aria-pressed", String(button.dataset.telemetrySection === telemetrySection));
    element("sources-dirty").hidden = !sourcesDirty;
    document.dispatchEvent(new CustomEvent("argos:live-inspector", { detail: {
      visible: document.body.dataset.view === "messages",
      run_id: current?.run_id ?? null, connection_id: current?.telemetry.connection_id ?? null,
      serviceFresh: serviceFresh(), endpoint: current?.telemetry.endpoint ?? null,
    } }));
  }

  function focusView(expanded) {
    document.body.classList.toggle("focus-mode", expanded);
    element("focus-button").setAttribute("aria-pressed", String(expanded));
    text("focus-button", expanded ? "Restore panel" : "Expanded view");
    renderPanel();
  }

  function openInspector(panel, section, opener) {
    document.dispatchEvent(new Event("argos:show-observation"));
    const familyChanged = inspectorPanel !== panel || document.body.classList.contains("focus-mode");
    inspectorPanel = panel;
    if (section) telemetrySection = section;
    panelChosen = true;
    // Keep the original visible trigger when moving between sections inside
    // the same inspector, so Escape always has somewhere useful to return.
    if (opener && !opener.closest("#inspector")) inspectorOpener = opener;
    focusView(false);
    if (panel === "telemetry") element("telemetry-details").scrollTop = 0;
    if (familyChanged) document.querySelector(`[data-panel-content="${panel}"]`).scrollTop = 0;
    if (!opener?.hasAttribute("data-telemetry-section")) {
      const target = panel === "telemetry" ? document.querySelector(`[data-telemetry-section="${telemetrySection}"]`) : element("inspector-close");
      target.focus({ preventScroll: true });
      if (window.matchMedia("(max-width: 760px)").matches) element("inspector").scrollIntoView({ block: "start" });
    }
  }

  function closeInspector() {
    inspectorPanel = null;
    panelChosen = true;
    renderPanel();
    const target = inspectorOpener?.isConnected && !inspectorOpener.closest("[hidden]") ? inspectorOpener : element("sources-button");
    target.focus();
  }

  function badge(id, label, tone = "neutral") {
    text(id, label);
    element(id).dataset.tone = tone;
  }

  function serviceFresh(now = performance.now()) {
    return current !== null && lastReceived !== null && lastAdvance !== null && now - lastReceived + stateTransitMs <= WATCHDOG_MS && now - lastAdvance <= WATCHDOG_MS;
  }

  function runTime(now = performance.now()) {
    // The snapshot may have been produced at any point during the request.
    // Adding the complete measured round trip gives a conservative upper bound
    // on elapsed local reception time, including transfer and JSON decoding.
    // This deliberately may expire data early; it says nothing about sensor age.
    return current ? current.at + (stateTransitMs + Math.max(0, now - lastReceived)) / 1000 : null;
  }

  function stateAge(view, now) {
    return finite(view?.rx_age_s) && lastReceived !== null ? view.rx_age_s + (stateTransitMs + Math.max(0, now - lastReceived)) / 1000 : null;
  }

  function viewRecent(view, now) {
    return !["error", "reconnecting"].includes(current.telemetry.state) && view.state === "recent" && (!finite(view.age_limit_s) || stateAge(view, now) <= view.age_limit_s);
  }

  function viewState(view, now) {
    return view.state === "recent" && !viewRecent(view, now) ? "stale" : view.state;
  }

  function telemetryState(now) {
    const telemetry = current.telemetry;
    return telemetry.state === "receiving" && ![telemetry.heartbeat, telemetry.attitude, telemetry.local_position_ned, telemetry.battery].some((view) => viewRecent(view, now)) ? "stale" : telemetry.state;
  }

  function currentFrameAge(now) {
    if (!frame) return null;
    // Repeated JPEG responses cannot refresh the age of the same image.
    return Math.max(0, runTime(now) - frame.receivedAt, frame.initialAge + (now - frame.observedAt) / 1000);
  }

  function clearFrame() {
    if (frame?.url) URL.revokeObjectURL(frame.url);
    frame = null;
    element("camera-image").removeAttribute("src");
    element("camera-image").hidden = true;
    element("vision-layer").replaceChildren();
    element("vision-layer").hidden = true;
  }

  function visionView() {
    const view = current?.vision;
    // Optional perception cannot invalidate the independent telemetry/control
    // snapshot. Treat malformed or absent configuration as locally unavailable.
    if (!view || typeof view !== "object" || Array.isArray(view)) return null;
    if (typeof view.configured !== "boolean" || !["disabled", "starting", "waiting", "recent", "stale", "error"].includes(view.state)
        || typeof view.detail !== "string" || view.detail.length > 1000 || typeof view.model !== "string" || view.model.length > 80
        || !finite(view.age_limit_s) || view.age_limit_s <= 0 || view.age_limit_s > 10
        || !(view.frame_age_s === null || (finite(view.frame_age_s) && view.frame_age_s >= 0))) return null;
    return view;
  }

  function visionRecent(now = performance.now()) {
    const view = visionView();
    return serviceFresh(now) && view?.configured && view.state === "recent" && finite(view.frame_age_s)
      && view.frame_age_s + (stateTransitMs + Math.max(0, now - lastReceived)) / 1000 <= view.age_limit_s;
  }

  function frameLimit() {
    return visionEnabled ? Math.min(current.video.age_limit_s, visionView()?.age_limit_s ?? 1) : current.video.age_limit_s;
  }

  function visionResult(header) {
    if (typeof header !== "string" || header.length > 32768) throw new Error("Missing or oversized vision metadata");
    const result = JSON.parse(header);
    const dimension = value => Number.isSafeInteger(value) && value > 0 && value <= 8192;
    if (!result || !dimension(result.width) || !dimension(result.height) || !Array.isArray(result.detections)
        || result.detections.length > 64 || !finite(result.inference_ms) || result.inference_ms < 0 || result.inference_ms > 60000) throw new Error("Invalid vision metadata");
    const ids = new Set();
    for (const detection of result.detections) {
      if (!detection || !Number.isSafeInteger(detection.track_id) || detection.track_id < 1 || ids.has(detection.track_id)
          || !finite(detection.confidence) || detection.confidence < 0 || detection.confidence > 1
          || !Array.isArray(detection.box) || detection.box.length !== 4
          || !detection.box.every(value => finite(value) && value >= 0 && value <= 1)) throw new Error("Invalid person detection");
      const [x, y, width, height] = detection.box;
      if (width <= 0 || height <= 0 || x + width > 1.000001 || y + height > 1.000001) throw new Error("Detection outside image");
      ids.add(detection.track_id);
    }
    return result;
  }

  function layoutVision() {
    const result = frame?.vision;
    if (!result) return;
    const stage = element("camera-stage"), layer = element("vision-layer");
    const scale = Math.min(stage.clientWidth / result.width, stage.clientHeight / result.height);
    const width = result.width * scale, height = result.height * scale;
    Object.assign(layer.style, { width: `${width}px`, height: `${height}px`, left: `${(stage.clientWidth - width) / 2}px`, top: `${(stage.clientHeight - height) / 2}px` });
    for (const box of layer.children) {
      const label = box.firstElementChild;
      label.style.maxWidth = `${Math.max(0, width - 4)}px`;
      const labelWidth = label.offsetWidth, labelHeight = label.offsetHeight;
      const left = box.offsetLeft, top = box.offsetTop;
      // Labels retain their own dark background even for a distant person only
      // a few pixels wide. Place above when possible and keep inside the image.
      label.style.left = `${Math.max(1 - left, Math.min(0, width - left - labelWidth - 3))}px`;
      label.style.top = `${top >= labelHeight + 4 ? -labelHeight - 4 : Math.max(0, Math.min(box.offsetHeight + 3, height - top - labelHeight - 3))}px`;
    }
  }

  function drawVision(result) {
    const layer = element("vision-layer");
    if (!result) { layer.replaceChildren(); return; }
    const existing = new Map(Array.from(layer.children, box => [Number(box.dataset.trackId), box]));
    for (const detection of result.detections) {
      const [x, y, width, height] = detection.box;
      let box = existing.get(detection.track_id);
      existing.delete(detection.track_id);
      if (!box) {
        box = document.createElement("div");
        const label = document.createElement("span"), hit = document.createElement("button");
        box.className = "vision-box";
        box.setAttribute("role", "listitem");
        box.dataset.trackId = String(detection.track_id);
        label.className = "vision-box-label";
        hit.className = "vision-hit";
        hit.type = "button";
        hit.hidden = true;
        box.append(label, hit); layer.append(box);
        let pressedIdentity = null;
        box.addEventListener("pointerdown", () => { pressedIdentity = visionIdentities.get(box); });
        box.addEventListener("pointercancel", () => { pressedIdentity = null; });
        box.addEventListener("click", event => {
          if (box.dataset.selectable !== "true" || layer.hidden) return;
          event.preventDefault();
          const identity = event.detail === 0 ? visionIdentities.get(box) : pressedIdentity || visionIdentities.get(box);
          pressedIdentity = null;
          document.dispatchEvent(new CustomEvent("argos:select-person", { detail: { ...identity } }));
        });
      }
      Object.assign(box.style, { left: `${100 * x}%`, top: `${100 * y}%`, width: `${100 * width}%`, height: `${100 * height}%` });
      box.firstElementChild.textContent = `Person #${detection.track_id} · ${Math.round(detection.confidence * 100)}%`;
      box.lastElementChild.setAttribute("aria-label", `Select person #${detection.track_id} for framing`);
      visionIdentities.set(box, { run_id: frame.run_id, video_id: frame.video_id, frame_sequence: frame.sequence, track_id: detection.track_id });
    }
    for (const box of existing.values()) box.remove();
    layoutVision();
  }

  function renderVisionSelection() {
    const visible = !element("vision-layer").hidden;
    const allowed = visible && visionSelection.allowed && document.body.dataset.view === "control";
    for (const box of element("vision-layer").children) {
      const selected = Number(box.dataset.trackId) === visionSelection.target_id;
      box.dataset.selectable = String(allowed);
      box.dataset.selected = String(selected);
      box.lastElementChild.hidden = !allowed;
      box.lastElementChild.setAttribute("aria-pressed", String(selected));
    }
  }

  function renderVision(fresh, now) {
    const view = visionView(), toggle = element("vision-toggle");
    toggle.checked = visionEnabled;
    toggle.disabled = !visionEnabled && (!fresh || !view?.configured);
    const visible = visionEnabled && visionRecent(now) && current.video.state === "recent"
      && frame?.vision && currentFrameAge(now) <= frameLimit();
    element("vision-layer").hidden = !visible;
    if (visible) layoutVision();
    renderVisionSelection();
    const visionState = { enabled: visionEnabled, recent: Boolean(visible), run_id: current?.run_id ?? null, video_id: current?.video.source_id ?? null };
    const signature = JSON.stringify(visionState);
    if (signature !== visionStateSignature) {
      visionStateSignature = signature;
      document.dispatchEvent(new CustomEvent("argos:vision-state", { detail: visionState }));
    }
    let detail = !view?.configured ? "Person detection is not configured" : "Person detection off";
    if (visionEnabled) {
      const state = !fresh ? "Service unavailable" : !view ? "Invalid vision status" : view.state === "recent" && !visionRecent(now) ? "Stale result" : view.state;
      detail = visible ? `${frame.vision.detections.length} ${frame.vision.detections.length === 1 ? "person" : "people"} · ${ageText(currentFrameAge(now))} · ${numeric(frame.vision.inference_ms, " ms")}` : imageFailure || view?.detail || (state === "recent" ? "Waiting for an analyzed image" : state);
      const assistance = visionSelection.active && document.body.dataset.view === "control";
      detail = `${assistance ? visionSelection.paused ? "Framing paused" : "Framing assistance active" : "Visual tracking only"} · ${detail}`;
      if (visible && typeof view.model === "string") detail += ` · ${view.model}`;
    }
    text("vision-status", detail);
  }

  function validState(value) {
    const record = (item) => item !== null && typeof item === "object" && !Array.isArray(item);
    const count = (item) => Number.isSafeInteger(item) && item >= 0;
    const optionalAge = (item) => item === null || (finite(item) && item >= 0);
    const endpoint = (item) => item === null || typeof item === "string";
    const view = (item) => record(item) && ["absent", "recent", "stale"].includes(item.state) && optionalAge(item.rx_age_s) && finite(item.age_limit_s) && item.age_limit_s > 0 && (item.fields === null || record(item.fields)) && (item.state === "absent" || (record(item.fields) && finite(item.rx_age_s))) && (item.boot_progress === null || typeof item.boot_progress === "string");
    const receivedView = (item) => record(item) && ["absent", "recent", "stale"].includes(item.state) && optionalAge(item.rx_age_s) && finite(item.age_limit_s) && item.age_limit_s > 0 && (item.state === "absent" || finite(item.rx_age_s));
    if (!record(value) || value.schema_version !== 1 || typeof value.run_id !== "string" || !value.run_id.length || !finite(value.at) || value.at < 0 || !["simulation", "real", "unconfigured"].includes(value.environment) || !record(value.video) || !record(value.telemetry) || !Array.isArray(value.events)) return false;
    const video = value.video;
    const telemetry = value.telemetry;
    const battery = telemetry.battery;
    const mode = telemetry.mode;
    const config = value.configuration;
    if (!receivedView(battery) || !(battery.fields === null || record(battery.fields)) || (battery.state !== "absent" && !record(battery.fields)) || ![battery.voltage_v, battery.current_a].every((item) => item === null || finite(item)) || !(battery.remaining_percent === null || (Number.isInteger(battery.remaining_percent) && battery.remaining_percent >= 0 && battery.remaining_percent <= 100))) return false;
    if (!receivedView(mode) || typeof mode.label !== "string" || typeof mode.known !== "boolean" || !(mode.custom_mode === null || count(mode.custom_mode))) return false;
    if (!record(config) || !["simulation", "real", "unconfigured"].includes(config.environment) || !["none", "gazebo", "device"].includes(config.video_source) || !["none", "udp", "tcp", "serial"].includes(config.mavlink_transport) || ![config.video_endpoint, config.mavlink_bind, config.mavlink_peer, config.mavlink_tcp, config.mavlink_device].every(endpoint) || !Number.isSafeInteger(config.baudrate) || config.baudrate <= 0 || ![null, "channel", "component"].includes(config.sequence_scope) || ![config.system, config.component].every((item) => Number.isInteger(item) && item >= 1 && item <= 255) || !validRecording(value.recording)) return false;
    return ["gazebo", "device", "none"].includes(video.source) && ["unconfigured", "waiting", "recent", "stale", "error", "reconnecting"].includes(video.state) && typeof video.label === "string" && typeof video.detail === "string" && endpoint(video.endpoint) && count(video.sequence) && optionalAge(video.received_at) && optionalAge(video.rx_age_s) && finite(video.age_limit_s) && video.age_limit_s > 0 && (video.width === null || count(video.width)) && (video.height === null || count(video.height)) && count(video.rejected) && typeof video.last_rejection === "string" && ["unconfigured", "waiting", "receiving", "stale", "error", "reconnecting"].includes(telemetry.state) && typeof telemetry.detail === "string" && endpoint(telemetry.endpoint) && count(telemetry.system) && count(telemetry.component) && ["rx_messages", "rx_bytes", "bad_bytes", "accepted", "ignored_source", "ignored_type", "rejected"].every((key) => count(telemetry[key])) && typeof telemetry.last_rejection === "string" && ["heartbeat", "attitude", "local_position_ned"].every((key) => view(telemetry[key])) && value.events.every((event) => record(event) && count(event.id) && finite(event.at) && event.at >= 0 && ["info", "warning", "error"].includes(event.level) && typeof event.message === "string");
  }

  function validRecording(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !["idle", "recording", "complete", "error"].includes(value.state) || !(value.id === null || /^[a-f0-9]{32}$/.test(value.id)) || !Number.isSafeInteger(value.events) || value.events < 0 || ![value.started_at, value.ended_at].every((item) => item === null || (finite(item) && item >= 0)) || typeof value.error !== "string") return false;
    if (value.end_reason !== undefined && ![null, "stopped", "transport_error", "event_limit", "size_limit", "shutdown"].includes(value.end_reason)) return false;
    if (value.end_detail !== undefined && typeof value.end_detail !== "string") return false;
    if (["max_events", "max_bytes", "size_bytes"].some((key) => value[key] !== undefined && (!Number.isSafeInteger(value[key]) || value[key] < 0))) return false;
    // Only the local, completed journal endpoint may become a download link.
    return value.download_url === null || (value.state === "complete" && typeof value.id === "string" && value.download_url === `/api/recordings/${value.id}/download`);
  }

  function validReception(value) {
    if (value === undefined) return true; // Older service/recorded UI fixtures.
    const source = (item) => ["video", "mavlink"].includes(item);
    const time = (item) => finite(item) && item >= 0;
    const recovery = value?.last_recovery;
    return value !== null && typeof value === "object" && Array.isArray(value.active) && value.active.length <= 2
      && value.active.every((item) => item && Number.isSafeInteger(item.id) && source(item.source) && time(item.since) && ["active", "recovering"].includes(item.state) && typeof item.title === "string" && typeof item.detail === "string" && Array.isArray(item.affected) && item.affected.every((name) => typeof name === "string"))
      && (recovery === null || (recovery && source(recovery.source) && time(recovery.at) && time(recovery.duration_s)));
  }

  function acceptState(body, started) {
    if (!validState(body) || !validReception(body.reception) || (body.reconnecting != null && !["video", "mavlink"].includes(body.reconnecting)) || [body.video.source_id, body.telemetry.connection_id].some((id) => id !== undefined && (typeof id !== "string" || !id.length))) throw new Error("Invalid service response");
    const received = performance.now();
    if (!current && !panelChosen && body.environment === "unconfigured") inspectorPanel = "sources";
    if (!current || body.run_id !== current.run_id) {
      frameEpoch += 1;
      clearFrame();
      lastAdvance = started;
      imageFailure = "";
      eventSignatures.clear();
    } else if (body.at < current.at) {
      throw new Error("Service clock moved backwards");
    } else if (body.at > current.at) {
      lastAdvance = started;
    }
    if (current && body.video.source_id !== current.video.source_id) {
      frameEpoch += 1;
      clearFrame();
      imageFailure = "";
    }
    current = body;
    lastReceived = received;
    stateTransitMs = Math.max(0, received - started);
    requestFailure = "";
    pollResumeAt = null;
  }

  async function getResponse(url) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      // Read the body while the timeout is active, including stalled responses.
      const body = url === "/api/state" ? await response.json() : await response.blob();
      return { response, body };
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function pollState() {
    while (!stopped) {
      const started = performance.now();
      const requestedEpoch = stateEpoch;
      try {
        if (pollingAllowed()) {
          const { body } = await getResponse("/api/state");
          // A request issued before a source/recording change is no longer
          // authoritative, even if its response arrives after the POST.
          if (pollingAllowed() && requestedEpoch === stateEpoch) acceptState(body, started);
        }
      } catch (error) {
        if (pollingAllowed() && requestedEpoch === stateEpoch) requestFailure = error.name === "AbortError" ? "The service did not respond in time." : "Unable to read service status.";
      }
      render();
      await delay(Math.max(0, STATE_INTERVAL_MS - (performance.now() - started)));
    }
  }

  async function decodeImage(url) {
    const probe = new Image();
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => { probe.src = ""; reject(new Error("Decoding timed out")); }, REQUEST_TIMEOUT_MS);
      probe.onload = () => { window.clearTimeout(timer); resolve(); };
      probe.onerror = () => { window.clearTimeout(timer); reject(new Error("Unreadable image")); };
      probe.src = url;
    });
    return probe;
  }

  async function pollFrames() {
    while (!stopped) {
      const started = performance.now();
      if (serviceFresh() && current.video.state === "recent" && (!visionEnabled || visionRecent())) {
        const requestedRun = current.run_id;
        const requestedVideo = current.video.source_id;
        const requestedVision = visionEnabled, requestedEpoch = frameEpoch;
        let candidateUrl = null;
        try {
          const { response, body } = await getResponse(requestedVision ? "/api/vision/frame.jpg" : "/api/frame.jpg");
          const sequenceHeader = response.headers.get("X-Frame-Sequence");
          const receivedHeader = response.headers.get("X-Frame-Received-At");
          const sequence = sequenceHeader === null ? NaN : Number(sequenceHeader);
          const receivedAt = receivedHeader === null ? NaN : Number(receivedHeader);
          const responseRun = response.headers.get("X-Run-Id");
          if (requestedEpoch !== frameEpoch || !serviceFresh() || current.run_id !== requestedRun || responseRun !== requestedRun || current.video.source_id !== requestedVideo || (requestedVideo && response.headers.get("X-Video-Id") !== requestedVideo)) continue;
          if (!Number.isSafeInteger(sequence) || sequence < 0 || !finite(receivedAt) || receivedAt < 0 || receivedAt > runTime(performance.now()) + .05 || !body.type.startsWith("image/jpeg")) throw new Error("Invalid image response");
          if (frame && sequence <= frame.sequence) continue;
          const result = requestedVision ? visionResult(response.headers.get("X-Vision-Result")) : null;
          candidateUrl = URL.createObjectURL(body);
          const decoded = await decodeImage(candidateUrl);
          if (result && (decoded.naturalWidth !== result.width || decoded.naturalHeight !== result.height)) throw new Error("Vision dimensions do not match image");
          if (requestedEpoch !== frameEpoch || !serviceFresh() || current.run_id !== requestedRun || current.video.source_id !== requestedVideo || (requestedVision && !visionRecent())) continue;
          const now = performance.now();
          const previousUrl = frame?.url;
          frame = { sequence, receivedAt, observedAt: now, initialAge: Math.max(0, runTime(now) - receivedAt), url: candidateUrl, vision: result,
            run_id: requestedRun, video_id: requestedVideo };
          // The decoded image and its own result enter the DOM in one turn.
          // Never overlay detections on a newer independently fetched raw JPEG.
          decoded.id = "camera-image";
          decoded.alt = "Image received from the drone camera";
          decoded.hidden = true;
          element("camera-image").replaceWith(decoded);
          drawVision(result);
          candidateUrl = null;
          if (previousUrl) URL.revokeObjectURL(previousUrl);
          imageFailure = "";
        } catch (error) {
          if (requestedEpoch === frameEpoch && current?.run_id === requestedRun && current.video.source_id === requestedVideo) {
            if (requestedVision) clearFrame();
            imageFailure = error.name === "AbortError" ? "The image stream did not respond in time." : requestedVision ? "The analyzed image or its detection metadata is unavailable." : "The image is unavailable or could not be decoded.";
          }
        } finally {
          if (candidateUrl) URL.revokeObjectURL(candidateUrl);
          render();
          // Also rate-limit successful repeated frames and discarded responses.
          await delay(Math.max(0, FRAME_INTERVAL_MS - (performance.now() - started)));
        }
      } else {
        await delay(FRAME_INTERVAL_MS);
      }
    }
  }

  function renderEvents(id, events, limit) {
    const selected = events.slice().sort((a, b) => b.id - a.id).slice(0, limit);
    const signature = JSON.stringify(selected);
    if (eventSignatures.get(id) === signature) return;
    eventSignatures.set(id, signature);
    const list = element(id);
    list.replaceChildren();
    if (!selected.length) {
      const empty = document.createElement("li");
      empty.className = "empty-event";
      empty.textContent = "No events received.";
      list.append(empty);
      return;
    }
    for (const event of selected) {
      const item = document.createElement("li");
      const meta = document.createElement("div");
      meta.className = "event-meta";
      const level = document.createElement("span");
      level.className = "event-level";
      level.dataset.level = event.level;
      level.textContent = { info: "Information", warning: "Warning", error: "Error" }[event.level] || "Event";
      const time = document.createElement("span");
      time.textContent = finite(event.at) ? `T + ${number.format(event.at)} s` : "—";
      meta.append(level, time);
      const message = document.createElement("p");
      message.className = "event-message";
      message.textContent = String(event.message ?? "");
      item.append(meta, message);
      list.append(item);
    }
  }

  const sourceControls = {
    environment: "config-environment", video_source: "config-video-source",
    video_endpoint: "config-video-endpoint", mavlink_transport: "config-transport",
    mavlink_bind: "config-bind", mavlink_peer: "config-peer", mavlink_tcp: "config-tcp",
    mavlink_device: "config-device", baudrate: "config-baudrate",
    sequence_scope: "config-scope", system: "config-system", component: "config-component",
  };

  function configureFormVisibility() {
    const video = element("config-video-source").value;
    const transport = element("config-transport").value;
    const enabled = {
      video: video !== "none", bind: transport === "udp", peer: transport === "udp",
      tcp: transport === "tcp", device: transport === "serial", baudrate: transport === "serial",
      scope: transport !== "none",
    };
    const blocked = !serviceFresh() || mutation !== null || Boolean(current?.reconnecting) || current?.recording.state === "recording";
    for (const [name, shown] of Object.entries(enabled)) {
      const row = element(`config-${name}-row`);
      row.hidden = !shown;
      const control = row.querySelector("input, select");
      control.required = shown;
      control.disabled = !shown || blocked;
    }
    for (const name of ["environment", "video-source", "transport", "system", "component"]) element(`config-${name}`).disabled = blocked;
    text("config-video-label", video === "device" ? "Linux camera device" : "Gazebo image topic");
    const endpoint = element("config-video-endpoint");
    endpoint.placeholder = video === "device" ? "/dev/video0" : "/world/…/sensor/…/image";
    endpoint.pattern = video === "device" ? "/dev/video[0-9]+" : String.raw`/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*`;
  }

  function fillSourcesForm() {
    if (!current) return;
    for (const [key, id] of Object.entries(sourceControls)) element(id).value = current.configuration[key] ?? "";
    configurationSignature = JSON.stringify(current.configuration);
    sourcesDirty = false;
    configureFormVisibility();
  }

  function sourcePayload() {
    const payload = {};
    for (const [key, id] of Object.entries(sourceControls)) payload[key] = element(id).value.trim();
    for (const name of ["baudrate", "system", "component"]) payload[name] = Number(payload[name]);
    if (payload.video_source === "none") payload.video_endpoint = null;
    for (const [key, transport] of Object.entries({ mavlink_bind: "udp", mavlink_peer: "udp", mavlink_tcp: "tcp", mavlink_device: "serial" })) {
      if (payload.mavlink_transport !== transport) payload[key] = null;
    }
    if (payload.mavlink_transport === "none") payload.sequence_scope = null;
    // A hidden baudrate is retained from the loaded configuration, not sent
    // as an empty/invalid value after editing an unrelated transport.
    if (payload.mavlink_transport !== "serial") payload.baudrate = current.configuration.baudrate;
    return payload;
  }

  async function postJson(path, payload) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), credentials: "same-origin", cache: "no-store",
        signal: controller.signal,
      });
      const body = await response.json();
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : typeof body.error === "string" ? body.error : `The service rejected the request (HTTP ${response.status}).`;
        throw new Error(detail);
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error("Response unconfirmed. Check session status before retrying.");
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function renderControls(fresh, now) {
    const previouslyFocused = document.activeElement;
    const journalVisible = document.body.dataset.view === "observation" && inspectorPanel === "recording" && !document.body.classList.contains("focus-mode");
    const recording = current?.recording;
    const active = recording?.state === "recording";
    const blocked = !fresh || mutation !== null || Boolean(current?.reconnecting);
    if (current && !sourcesDirty && !mutation && configurationSignature !== JSON.stringify(current.configuration)) fillSourcesForm();
    configureFormVisibility();
    element("sources-apply").disabled = blocked || active || !sourcesDirty;
    element("sources-reset").disabled = blocked || !sourcesDirty;
    text("sources-form-hint", !fresh ? "Connect to the service to change sources." : mutation === "sources" ? "Applying sources…" : active ? "Stop the MAVLink recording before changing sources." : sourcesDirty ? "Settings changed, not yet applied." : "These settings match the active configuration.");
    element("recording-start").disabled = blocked || active || !current || ["unconfigured", "error", "reconnecting"].includes(current.telemetry.state);
    // Stopping stays available after a telemetry fault while the service is
    // reachable; the server decides whether the journal can be finalized.
    element("recording-stop").disabled = blocked || !active;
    element("recording-start").hidden = active;
    element("recording-stop").hidden = !active;
    const interrupted = recording?.state === "complete" && ["transport_error", "shutdown"].includes(recording.end_reason);
    const atLimit = recording?.state === "complete" && ["event_limit", "size_limit"].includes(recording.end_reason);
    const labels = { idle: "Ready", recording: "Recording", complete: interrupted ? "Interrupted" : atLimit ? "Limit reached" : "Complete", error: "Error" };
    const tone = !fresh ? "neutral" : recording.state === "error" ? "error" : interrupted || atLimit ? "warning" : active ? "positive" : "neutral";
    badge("recording-state", fresh ? labels[recording.state] : "Not refreshed", tone);
    const globalLabel = !fresh ? "Capture · not refreshed" : active ? "Capture in progress" : recording.state === "error" ? "Capture error" : interrupted ? "Capture interrupted" : atLimit ? "Capture · limit reached" : recording.state === "complete" ? "Capture complete" : "Capture ready";
    badge("global-recording-state", globalLabel, tone);
    element("global-recording").dataset.tone = tone;
    element("global-recording-dot").dataset.tone = tone;
    element("global-recording").title = !fresh ? "Capture status is no longer refreshed. Open the MAVLink recording panel." : `${globalLabel}${recording.end_detail || recording.error ? ` · ${recording.end_detail || recording.error}` : ""}. Open the MAVLink recording panel.`;
    text("recording-limits", fresh && Number.isSafeInteger(recording.max_events) && Number.isSafeInteger(recording.max_bytes) ? `Automatically closes at ${integer.format(recording.max_events)} messages or ${numeric(recording.max_bytes / 1048576, " MiB")}. Current size: ${numeric(recording.size_bytes / 1048576, " MiB")}.` : "");
    badge("detail-recording-state", element("recording-state").textContent, element("recording-state").dataset.tone);
    badge("archive-live-recording", !fresh ? "Capture status not refreshed" : active ? `Capture in progress · ${integer.format(recording.events)} frames` : recording.state === "error" ? "Capture error · inspect recording" : interrupted || atLimit ? `${globalLabel} · recording available` : "No capture in progress", element("recording-state").dataset.tone);
    text("journal-detail-title", active ? "Capture in progress" : recording?.id ? "Latest service recording" : "Recording incoming data");
    text("recording-id", recording?.id || "No recording");
    text("recording-events", fresh ? integer.format(recording.events) : "—");
    const end = active ? runTime(now) : recording?.ended_at;
    text("recording-duration", fresh && finite(recording.started_at) && finite(end) ? ageText(end - recording.started_at) : "—");
    text("recording-hint", !fresh ? "Connect to the service to control recording." : mutation?.startsWith("recording") ? "Request in progress…" : recording.state === "error" ? `Write failed: ${recording.error || "the recording cannot be finalized."}` : active ? "Capture in progress. Stop finalizes the downloadable recording." : interrupted || atLimit ? `${interrupted ? "Capture interrupted" : "Capture limit reached"}. ${recording.end_detail || ""} The finalized recording remains available for download and viewing in Sessions.` : current.telemetry.state === "unconfigured" ? "Configure a MAVLink source in Sources to record incoming data." : current.telemetry.state === "error" ? "Restore the MAVLink link before starting a new capture." : recording.state === "complete" ? "Recording finalized. It remains available in Sessions, even after a new capture." : "Ready to record incoming MAVLink data.");
    const download = element("recording-download");
    const downloadable = fresh && recording.state === "complete" && typeof recording.download_url === "string" && recording.download_url === `/api/recordings/${recording.id}/download`;
    download.hidden = !downloadable;
    if (downloadable) download.setAttribute("href", recording.download_url);
    else download.removeAttribute("href");
    // A background failure must not leave keyboard focus on a hidden control.
    if (journalVisible && !mutation && active && previouslyFocused === element("recording-start") && !element("recording-stop").disabled) element("recording-stop").focus();
    if (journalVisible && !mutation && !active && previouslyFocused === element("recording-stop")) {
      (downloadable ? download : element("recording-start").disabled ? document.querySelector('[data-panel="recording"]') : element("recording-start")).focus();
    }
    element("recording-action-status").hidden = !element("recording-action-status").textContent.trim();
  }

  function renderSources(fresh) {
    if (!current) return;
    const { video, telemetry, environment } = current;
    const unavailable = expectedPause() ? "Refresh pending" : "Service unreachable";
    const env = { simulation: "Simulation", real: "Reported as real", unconfigured: "No environment configured" }[environment] || "Unknown environment";
    text("source-environment", fresh ? env : `${env} · last received configuration, ${unavailable.toLowerCase()}`);
    text("source-video-label", video.source === "none" ? "No camera configured" : video.label);
    text("source-video-endpoint", video.endpoint || "Not configured");
    text("source-video-limit", ageText(video.age_limit_s));
    text("source-video-detail", video.detail || "No details available.");
    badge("source-video-state", fresh ? (states[video.state] || video.state) : unavailable, fresh && video.state === "recent" ? "positive" : fresh && video.state === "error" ? "error" : "neutral");
    text("source-telemetry-endpoint", telemetry.endpoint || "Not configured");
    text("source-telemetry-id", `System ${telemetry.system} · component ${telemetry.component}`);
    const effectiveTelemetryState = telemetryState(performance.now());
    badge("source-telemetry-state", fresh ? (states[effectiveTelemetryState] || effectiveTelemetryState) : unavailable, fresh && effectiveTelemetryState === "receiving" ? "positive" : fresh && effectiveTelemetryState === "error" ? "error" : fresh && effectiveTelemetryState === "stale" ? "warning" : "neutral");
    text("active-source-summary", `${video.label} · ${telemetry.endpoint || "MAVLink not configured"}`);
    text("detail-video-size", fresh && finite(video.width) && finite(video.height) ? `${video.width} × ${video.height}` : "—");
    text("detail-video-age", fresh ? ageText(currentFrameAge(performance.now())) : "—");
    badge("source-video-state", element("video-status").textContent, element("video-status").dataset.tone);
    text("video-last-rejection", fresh ? video.last_rejection || "No rejected images." : "Rejections not refreshed.");
  }

  function renderDetails(fresh, now) {
    if (!current) return;
    const t = current.telemetry;
    text("details-service-state", fresh ? "Counters for this observation. They do not measure radio loss or latency." : "Status not refreshed: values are hidden. Retained events describe the last received observation.");
    for (const [id, value] of Object.entries({ "count-rx": t.rx_messages, "count-bytes": t.rx_bytes, "count-accepted": t.accepted, "count-rejected": t.rejected, "count-other-source": t.ignored_source, "count-other-type": t.ignored_type, "count-bad-bytes": t.bad_bytes, "count-video-rejected": current.video.rejected })) text(id, fresh && finite(value) ? integer.format(value) : "—");
    const rejection = t.last_rejection;
    text("last-rejection", fresh ? (rejection || "No rejections reported.") : "Rejections not refreshed.");
    for (const [name, view] of [["heartbeat", t.heartbeat], ["position", t.local_position_ned], ["battery", t.battery], ["attitude", t.attitude]]) {
      const recent = fresh && viewRecent(view, now);
      badge(`detail-${name}-state`, fresh ? (t.state === "error" ? "Link interrupted" : states[viewState(view, now)] || "Missing") : "Not refreshed", recent ? "positive" : fresh && viewState(view, now) === "stale" ? "warning" : "neutral");
      text(`detail-${name}-age`, fresh ? ageText(stateAge(view, now)) : "—");
    }
    const heartbeat = fresh && viewRecent(t.heartbeat, now) ? t.heartbeat.fields : null;
    const mode = fresh && viewRecent(t.mode, now) ? t.mode : null;
    text("detail-mode-label", mode ? mode.known ? mode.label : finite(mode.custom_mode) ? `Unknown · raw ${mode.custom_mode}` : "Unknown" : "—");
    text("detail-armed", Number.isSafeInteger(heartbeat?.base_mode) ? ((heartbeat.base_mode & 128) !== 0 ? "Armed" : "Disarmed") : "—");
    text("detail-autopilot", heartbeat && finite(heartbeat.type) && finite(heartbeat.autopilot) ? `${heartbeat.type} / ${heartbeat.autopilot}` : "—");
    text("detail-custom-mode", heartbeat && finite(heartbeat.custom_mode) ? heartbeat.custom_mode : "—");
    const battery = fresh && viewRecent(t.battery, now) ? t.battery : null;
    text("detail-battery-voltage", numeric(battery?.voltage_v, " V"));
    text("detail-battery-current", numeric(battery?.current_a, " A"));
    text("detail-battery-remaining", numeric(battery?.remaining_percent, " %"));
    const attitude = fresh && viewRecent(t.attitude, now) ? t.attitude.fields : null;
    for (const axis of ["roll", "pitch", "yaw"]) text(`detail-${axis}`, finite(attitude?.[axis]) ? numeric(attitude[axis] * 180 / Math.PI, "°") : "—");
    text("detail-angular-rates", attitude ? [attitude.rollspeed, attitude.pitchspeed, attitude.yawspeed].map((value) => finite(value) ? numeric(value * 180 / Math.PI, "°/s") : "—").join(" / ") : "—");
    const position = fresh && viewRecent(t.local_position_ned, now) ? t.local_position_ned.fields : null;
    text("detail-position", position ? [position.x, position.y, position.z].map((value) => numeric(value, " m")).join(" / ") : "—");
    text("detail-velocity", position ? [position.vx, position.vy, position.vz].map((value) => numeric(value, " m/s")).join(" / ") : "—");
    text("detail-attitude-boot", fresh ? (bootLabels[String(t.attitude.boot_progress).toLowerCase()] || "—") : "—");
    text("detail-position-boot", fresh ? (bootLabels[String(t.local_position_ned.boot_progress).toLowerCase()] || "—") : "—");
    renderAutopilotStatus(fresh, now);
    renderEvents("all-events", current.events, current.events.length);
    text("events-context", fresh ? "The latest 60 reception, camera and MAVLink events." : "Retained history of the last received observation. Incoming data is no longer refreshed.");
  }

  const textReasons = { assembling: "Waiting for chunks", timeout: "Chunk timeout", missing_chunk: "Missing chunk", conflicting_chunk: "Conflicting chunks", severity_changed: "Severity changed during assembly", restarted: "ID reused", limit: "Assembly limit reached", reconnect: "Link reopened before completion" };
  let autopilotTextSignature = "";

  function renderAutopilotStatus(fresh, now) {
    const status = current.telemetry.autopilot_status;
    const declaration = status?.declaration;
    const age = finite(declaration?.rx_age_s) ? Math.max(declaration.rx_age_s + Math.max(0, runTime(now) - current.at), finite(declaration.received_at) ? runTime(now) - declaration.received_at : 0) : null;
    const recent = fresh && !["error", "reconnecting"].includes(current.telemetry.state) && declaration?.state === "recent" && finite(age) && age <= declaration.age_limit_s;
    text("detail-system-status", recent && typeof declaration.label === "string" ? `${declaration.label} · reported` : !fresh ? "Not refreshed" : declaration?.state === "stale" || finite(age) && age > declaration.age_limit_s ? "Stale report" : "Unknown");
    element("detail-system-status").title = recent && typeof declaration.name === "string" ? declaration.name : "";
    const history = status?.texts;
    const entries = Array.isArray(history?.entries) ? history.entries.slice(0, 60).filter((entry) => entry && Number.isSafeInteger(entry.id) && typeof entry.text === "string" && typeof entry.severity_label === "string" && typeof entry.connection_id === "string" && [entry.received_at, entry.first_received_at, entry.rx_age_s].every((value) => finite(value) && value >= 0) && [entry.system, entry.component, entry.severity, entry.chunks].every(Number.isSafeInteger)) : [];
    text("autopilot-texts-summary", `Autopilot messages · ${entries.length}`);
    text("autopilot-texts-state", !history ? "History not provided by this service." : !fresh ? "Retained history · service not refreshed. Current ages are unavailable." : entries.length ? `System ${history.system} · component ${history.component} · newest first.` : "No STATUSTEXT received for this component in this observation.");
    if (!element("autopilot-texts").open) return;
    const signature = JSON.stringify([history?.connection_id, entries.map(({ rx_age_s, ...entry }) => entry)]);
    if (signature !== autopilotTextSignature) {
      autopilotTextSignature = signature;
      const fragment = document.createDocumentFragment();
      for (const entry of entries) {
        const item = document.createElement("li");
        const heading = document.createElement("p");
        heading.className = "autopilot-text-heading";
        heading.textContent = `${entry.severity_label} · severity ${entry.severity}`;
        const body = document.createElement("p");
        body.className = "autopilot-text-body";
        body.textContent = entry.text;
        const origin = document.createElement("p");
        origin.className = "microcopy";
        origin.textContent = `System ${entry.system} · component ${entry.component} · reception T + ${ageText(entry.received_at)}${entry.connection_id !== history.connection_id ? " · previous connection" : ""}`;
        origin.title = `Connection ${entry.connection_id}`;
        const received = document.createElement("p");
        received.className = "microcopy autopilot-text-age";
        received.dataset.id = String(entry.id);
        const completeness = document.createElement("p");
        completeness.className = "microcopy";
        completeness.textContent = `${entry.complete ? "Complete text" : `Incomplete text · ${textReasons[entry.reason] || "assembly incomplete"}`}${entry.utf8_valid === false ? " · Incomplete or invalid UTF-8" : ""}`;
        item.append(heading, body, origin, received, completeness);
        fragment.append(item);
      }
      element("autopilot-text-list").replaceChildren(fragment);
    }
    for (const received of element("autopilot-text-list").querySelectorAll(".autopilot-text-age")) {
      const entry = entries.find((item) => String(item.id) === received.dataset.id);
      if (!entry) continue;
      const value = fresh ? `Received ${ageText(Math.max(entry.rx_age_s, runTime(now) - entry.received_at))} ago` : "Age not refreshed";
      if (received.textContent !== value) received.textContent = value;
    }
    text("autopilot-texts-limits", history ? `History limited to ${Number.isSafeInteger(history.max_entries) ? history.max_entries : 60} texts; ${integer.format(history.evicted_entries || 0)} older entries removed. ${integer.format(history.pending || 0)} pending assemblies; ${integer.format(history.rejected || 0)} rejected chunks.${history.last_rejection ? ` Last rejection: ${history.last_rejection}` : ""}` : "");
  }

  function renderReception(fresh, now) {
    const actionPending = !fresh && expectedPause(now);
    if (!fresh && !actionPending && (current || requestFailure)) serviceLostAt ??= now;
    if (fresh && serviceLostAt !== null) {
      serviceRecovery = { at: now, duration: (now - serviceLostAt) / 1000 };
      serviceLostAt = null;
    }
    const t = current?.telemetry;
    const video = current?.video;
    const issues = fresh ? current?.reception?.active || [] : [];
    const elapsed = runTime(now);
    const recovery = current?.reception?.last_recovery;
    const reconnecting = current?.reconnecting || (mutation?.startsWith("reconnect-") ? mutation.slice(10) : null);
    const imageAvailable = fresh && video.state === "recent" && frame !== null && currentFrameAge(now) <= frameLimit() && (!visionEnabled || visionRecent(now));
    const viewNames = { heartbeat: "mode", battery: "battery", attitude: "attitude", local_position_ned: "local position" };
    const usable = imageAvailable ? ["image"] : [];
    badge("available-video", !fresh ? "Not refreshed" : imageAvailable ? "Recent" : states[video.state] === "Recent" ? "Display pending" : states[video.state], imageAvailable ? "positive" : "neutral");
    for (const name of Object.keys(viewNames)) {
      const recent = fresh && viewRecent(t[name], now);
      if (recent) usable.push(viewNames[name]);
      badge(`available-${name}`, !fresh ? "Not refreshed" : recent ? `Recent · ${ageText(stateAge(t[name], now))}` : t.state === "error" ? "Link interrupted" : t.state === "reconnecting" ? "Reopening" : t[name].state === "absent" ? "Not received" : `Stale · ${ageText(stateAge(t[name], now))}`, recent ? "positive" : fresh && t[name].state === "stale" ? "warning" : "neutral");
    }
    text("reception-summary", actionPending ? "Refresh pending during the requested action. Stale data remains hidden." : !fresh ? "Service unreachable: source availability cannot be verified. Retrying automatically." : usable.length ? `Recent data available: ${usable.join(", ")}.` : "No recent image or measurement currently available.");
    const displayFailure = fresh && video.state === "recent" && !imageAvailable && Boolean(imageFailure || (frame && currentFrameAge(now) > frameLimit()));
    const shown = !fresh && serviceLostAt !== null ? [{ id: "service", source: "service", title: "ARGOS service unreachable", detail: "Check that the local service is running. This does not establish a camera or MAVLink outage.", state: "active" }] : issues.slice();
    if (displayFailure) shown.push({ id: "display", source: "display", title: "Video display interrupted", detail: "The service receives images, but this browser is no longer refreshing them. The console retries automatically.", state: "active" });
    const signature = JSON.stringify(shown.map(({ since, ...item }) => item));
    if (signature !== issueSignature) {
      issueSignature = signature;
      const rows = shown.map((item) => {
        const row = document.createElement("li");
        row.dataset.incident = item.id;
        const title = document.createElement("h4");
        title.textContent = item.state === "recovering" ? "Confirming recovery" : item.title;
        const detail = document.createElement("p");
        detail.className = "section-note";
        detail.textContent = item.detail;
        const age = document.createElement("span");
        age.className = "incident-age";
        row.append(title, detail, age);
        return row;
      });
      element("reception-issues").replaceChildren(...rows);
    }
    for (const row of element("reception-issues").children) {
      const item = shown.find((issue) => String(issue.id) === row.dataset.incident);
      const label = item.source === "service" ? `For ${ageText((now - serviceLostAt) / 1000)}` : finite(item.since) ? `Observed for ${ageText(elapsed - item.since)}` : "Camera reception and display are checked separately.";
      if (row.lastChild.textContent !== label) row.lastChild.textContent = label;
    }
    const recoveryLabel = recovery ? `${recovery.source === "video" ? "Images" : "MAVLink reception"} restored · observed interruption ${ageText(recovery.duration_s)} · T + ${number.format(recovery.at)} s.` : "";
    text("reception-recovery", fresh ? recoveryLabel : "");
    let noticeTitle = "", noticeAge = "", tone = "warning";
    if (actionPending) {
      noticeTitle = "Action in progress · refresh pending";
      tone = "neutral";
    } else if (shown.length) {
      noticeTitle = shown.length > 1 ? `${shown.length} data sources to check` : shown[0].state === "recovering" ? "Confirming recovery" : shown[0].title;
      noticeAge = shown[0].source === "service" ? `for ${ageText((now - serviceLostAt) / 1000)}` : finite(shown[0].since) ? `for ${ageText(elapsed - shown[0].since)}` : "";
    } else if (fresh && reconnecting) {
      noticeTitle = reconnecting === "video" ? "Reopening camera…" : "Reopening MAVLink…";
    } else if (fresh && recovery && elapsed - recovery.at <= 10) {
      noticeTitle = recovery.source === "video" ? "Images receiving again" : "MAVLink reception restored";
      tone = "positive";
    } else if (fresh && serviceRecovery && now - serviceRecovery.at <= 10000) {
      noticeTitle = "Service reachable again";
      noticeAge = `interruption ${ageText(serviceRecovery.duration)}`;
      tone = "positive";
    }
    const recoveryCount = shown.filter((issue) => issue.state === "recovering").length;
    const receptionLabel = actionPending ? "Refresh pending" : !fresh ? current ? "Not refreshed" : "Waiting" : shown.length ? recoveryCount === shown.length ? "Recovering" : `${shown.length} incident${shown.length > 1 ? "s" : ""} active` : reconnecting ? "Reopening" : recovery && elapsed - recovery.at <= 10 ? "Reception restored" : "No reception incidents";
    badge("reception-state", receptionLabel, fresh && shown.length ? "warning" : "neutral");
    element("reception-notice").hidden = !noticeTitle;
    badge("reception-notice-title", noticeTitle, tone);
    element("reception-notice").dataset.tone = tone;
    text("reception-notice-age", noticeAge);
    for (const source of ["video", "mavlink"]) {
      const configured = current && (source === "video" ? current.video.source !== "none" : current.configuration.mavlink_transport !== "none");
      const journal = source === "mavlink" && current?.recording.state === "recording";
      for (const button of document.querySelectorAll(`[data-reconnect="${source}"]`)) {
        button.disabled = !fresh || Boolean(mutation || reconnecting) || !configured || journal;
        button.textContent = reconnecting === source ? "Reopening…" : source === "video" ? "Reopen camera" : "Reopen MAVLink";
      }
      const hint = !fresh ? "Reopening is available when the service is reachable." : !configured ? `${source === "video" ? "Camera" : "MAVLink"} : configure this source.` : journal ? "Stop recording before reopening MAVLink. Capture does not restart automatically." : source === "video" ? "Reopens the camera with the active settings; MAVLink reception continues." : "Reopens the MAVLink receiver; camera reception continues. MAVLink counters restart from zero.";
      for (const node of document.querySelectorAll(`[data-reconnect-hint="${source}"]`)) if (node.textContent !== hint) node.textContent = hint;
    }
  }

  function render() {
    const now = performance.now();
    const fresh = serviceFresh(now);
    if (visionEnabled && frame && (!fresh || current.video.state !== "recent" || !visionRecent(now) || currentFrameAge(now) > frameLimit())) {
      imageFailure = !fresh ? "The service is unavailable." : "The analyzed image is no longer recent.";
      frameEpoch += 1;
      clearFrame();
    }
    renderVision(fresh, now);
    document.dispatchEvent(new CustomEvent("argos:control-state", { detail: {
      control: current?.control ?? null, fresh, run_id: current?.run_id ?? null,
      environment: current?.environment ?? null,
    } }));
    const actionPending = !fresh && expectedPause(now);
    const connecting = !current && !requestFailure;
    renderControls(fresh, now);
    renderPanel();
    renderReception(fresh, now);
    text("service-status", fresh ? "Service connected" : actionPending ? "Action in progress…" : connecting ? "Connecting to service…" : "Service unreachable");
    element("service-dot").dataset.tone = fresh ? "positive" : connecting || actionPending ? "neutral" : "error";
    text("environment", current ? ({ simulation: "SIMULATION", real: "REPORTED AS REAL", unconfigured: "CONFIGURE SOURCES" }[current.environment] || "UNKNOWN ENVIRONMENT") : "No session received");
    if (!fresh) {
      element("camera-image").hidden = true;
      element("camera-image-caption").hidden = true;
      element("camera-empty").hidden = false;
      badge("video-status", connecting ? "Waiting" : "Not refreshed", connecting ? "neutral" : "warning");
      text("camera-empty-title", actionPending ? "Refresh pending" : connecting ? "Connecting to service…" : "The console is no longer receiving status");
      text("camera-empty-detail", actionPending ? "The requested action is in progress. Stale images and values stay hidden until new status arrives." : connecting ? "Waiting for source status." : "Check that the ARGOS service is running and reachable. Images and values stay hidden until reception recovers.");
      text("camera-source", current?.video.label || "Unavailable");
      text("camera-age", "—");
      text("camera-size", "");
      badge("telemetry-status", connecting ? "Waiting" : "Not refreshed", connecting ? "neutral" : "warning");
      text("telemetry-hint", connecting ? "Reception status will appear here." : actionPending ? "Action in progress. Values are waiting for new service status." : "Service unreachable. Last received values are not shown as current.");
      for (const id of ["roll", "pitch", "yaw", "flight-mode", "armed-preview", "battery-remaining", "position-north", "position-east", "position-down"]) text(id, "—");
      text("attitude-age", "Not refreshed");
      text("battery-age", "Not refreshed");
      text("position-age", "Not refreshed");
      text("heartbeat-status", "Not refreshed");
      text("session-status", current ? "Observation interrupted · reception not refreshed" : "No observation received");
      renderSources(false);
      renderDetails(false, now);
      return;
    }

    const { video, telemetry } = current;
    const age = currentFrameAge(now);
    const visible = video.state === "recent" && frame !== null && age <= frameLimit() && (!visionEnabled || visionRecent(now));
    element("camera-image").hidden = !visible;
    element("camera-image-caption").hidden = !visible;
    element("camera-empty").hidden = visible;
    const frameExpired = frame !== null && age > frameLimit();
    let videoLabel = states[video.state] || "Unknown state";
    let emptyTitle = "Waiting for an image";
    let emptyDetail = video.detail || "The source is configured. Check that the drone camera is sending to the endpoint shown in Sources.";
    if (video.state === "unconfigured") {
      emptyTitle = "No camera configured";
      emptyDetail = "Configure the Gazebo or drone camera in Sources. Its image will appear here when received.";
    } else if (video.state === "reconnecting") {
      emptyTitle = "Reopening camera…";
    } else if (video.state === "error") {
      emptyTitle = "The camera reports an error";
    } else if (video.state === "stale" || frameExpired) {
      videoLabel = "Stale image";
      emptyTitle = "The image is no longer updating";
      emptyDetail = "The last image exceeds the age limit. Check the camera stream; display will resume when a recent image arrives.";
    } else if (video.state === "recent" && !frame) {
      videoLabel = "Waiting for image";
      emptyDetail = imageFailure || "The service receives images. The console is waiting for a decodable image.";
    }
    if (visionEnabled && !visible) {
      videoLabel = "Detection image pending";
      emptyTitle = "Waiting for a recent analyzed image";
      emptyDetail = imageFailure || visionView()?.detail || "Person detection must produce a recent image before boxes can be shown. Turn detection off to return to the camera stream.";
    }
    badge("video-status", visible ? "Recent image" : videoLabel, visible ? "positive" : video.state === "error" ? "error" : video.state === "stale" || frameExpired ? "warning" : "neutral");
    text("camera-empty-title", emptyTitle);
    text("camera-empty-detail", emptyDetail);
    text("camera-source", video.source === "none" ? "Not configured" : video.label);
    text("camera-age", frame ? ageText(age) : "—");
    text("camera-size", visible && finite(video.width) && finite(video.height) ? `${video.width} × ${video.height}` : "");
    text("image-sequence", frame ? `n° ${integer.format(frame.sequence)}` : "");
    const attitude = telemetry.attitude;
    const effectiveTelemetryState = telemetryState(now);
    badge("telemetry-status", effectiveTelemetryState === "error" ? "Link interrupted" : states[effectiveTelemetryState] || "Unknown state", effectiveTelemetryState === "receiving" ? "positive" : effectiveTelemetryState === "error" ? "error" : effectiveTelemetryState === "stale" ? "warning" : "neutral");
    text("telemetry-hint", telemetry.detail || "Incoming data is filtered by the selected system and component.");
    text("attitude-age", telemetry.state === "error" ? "Link interrupted" : attitude.state === "absent" ? "No data" : `${states[viewState(attitude, now)] || attitude.state} · ${ageText(stateAge(attitude, now))}`);
    for (const name of ["roll", "pitch", "yaw"]) text(name, viewRecent(attitude, now) && finite(attitude.fields?.[name]) ? numeric(attitude.fields[name] * 180 / Math.PI, "°") : "—");
    text("heartbeat-status", telemetry.state === "error" ? "Link interrupted" : telemetry.heartbeat.state === "absent" ? "Missing" : `${states[viewState(telemetry.heartbeat, now)] || telemetry.heartbeat.state} · ${ageText(stateAge(telemetry.heartbeat, now))}`);
    const heartbeat = viewRecent(telemetry.heartbeat, now) ? telemetry.heartbeat.fields : null;
    text("armed-preview", Number.isSafeInteger(heartbeat?.base_mode) ? `${heartbeat.base_mode & 128 ? "Armed" : "Disarmed"} reported` : "Arming status not refreshed");
    const mode = viewRecent(telemetry.mode, now) ? telemetry.mode : null;
    text("flight-mode", mode ? (mode.known ? (mode.label || "—") : finite(mode.custom_mode) ? `Unknown · raw ${integer.format(mode.custom_mode)}` : "Unknown") : "—");
    text("battery-remaining", viewRecent(telemetry.battery, now) ? numeric(telemetry.battery.remaining_percent, " %") : "—");
    text("battery-age", telemetry.state === "error" ? "Link interrupted" : telemetry.battery.state === "absent" ? "No data" : `${states[viewState(telemetry.battery, now)] || telemetry.battery.state} · ${ageText(stateAge(telemetry.battery, now))}`);
    const position = telemetry.local_position_ned;
    const positionFields = viewRecent(position, now) ? position.fields : null;
    for (const [id, axis] of [["position-north", "x"], ["position-east", "y"], ["position-down", "z"]]) text(id, numeric(positionFields?.[axis], " m"));
    text("position-age", telemetry.state === "error" ? "Link interrupted" : position.state === "absent" ? "No data" : `${states[viewState(position, now)] || position.state} · ${ageText(stateAge(position, now))}`);
    text("session-status", `Observation ${current.run_id.slice(0, 8)} · T + ${integer.format(runTime(now))} s`);
    renderSources(true);
    renderDetails(true, now);
  }

  for (const view of ["observation", "control", "messages", "sessions"]) element(`view-${view}`).addEventListener("click", (event) => showWorkspace(view, { opener: event.currentTarget }));
  for (const button of document.querySelectorAll("[data-open-live]")) button.addEventListener("click", () => showWorkspace("messages", { opener: button }));
  document.addEventListener("argos:show-observation", () => showWorkspace("observation", { restore: true }));
  document.addEventListener("argos:show-workspace", (event) => showWorkspace(event.detail?.view));
  for (const button of document.querySelectorAll("[data-panel]")) button.addEventListener("click", () => openInspector(button.dataset.panel, button.dataset.section, button));
  for (const button of document.querySelectorAll("[data-reconnect]")) button.addEventListener("click", async () => {
    if (button.disabled || mutation || !serviceFresh() || current.reconnecting) return;
    const source = button.dataset.reconnect;
    mutation = `reconnect-${source}`;
    stateEpoch += 1;
    text("reconnect-feedback", "");
    text("action-status", "");
    render();
    try {
      const body = await postJson(`/api/sources/${source}/reconnect`, {});
      if (!validState(body)) throw new Error("Invalid service response.");
      // Keep polling throughout the reopen. A newer GET may already exist;
      // never overwrite it with the state carried by this slower action.
      const state = source === "video" ? body.video.state : body.telemetry.state;
      const detail = source === "video" ? body.video.detail : body.telemetry.detail;
      const message = state === "error" ? `Unable to reopen: ${detail}` : "Receiver reopened. New data reception still needs confirmation.";
      text("reconnect-feedback", message);
      text("action-status", message);
      element("reconnect-feedback").dataset.tone = state === "error" ? "error" : "neutral";
    } catch (error) {
      text("reconnect-feedback", error.message);
      text("action-status", error.message);
      element("reconnect-feedback").dataset.tone = "error";
    } finally {
      mutation = null;
      stateEpoch += 1;
      render();
    }
  });
  for (const button of document.querySelectorAll("[data-telemetry-section]")) button.addEventListener("click", () => openInspector("telemetry", button.dataset.telemetrySection, button));
  element("inspector-close").addEventListener("click", closeInspector);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.dataset.view === "messages" && !["SELECT", "INPUT", "TEXTAREA"].includes(event.target.tagName)) {
      event.preventDefault();
      showWorkspace("observation", { restore: true });
      return;
    }
    if (event.key === "Escape" && document.body.dataset.view === "observation" && inspectorPanel !== null && !document.body.classList.contains("focus-mode") && !["SELECT", "INPUT"].includes(event.target.tagName)) {
      event.preventDefault();
      closeInspector();
    }
  });
  for (const eventName of ["input", "change"]) element("sources-form").addEventListener(eventName, () => {
    sourcesDirty = true;
    text("sources-action-status", "");
    element("sources-action-status").dataset.tone = "neutral";
    render();
  });
  element("sources-reset").addEventListener("click", () => {
    if (!serviceFresh() || mutation) return;
    fillSourcesForm();
    text("sources-action-status", "Active configuration restored. No changes sent to the service.");
    element("sources-action-status").dataset.tone = "neutral";
    render();
  });
  element("sources-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!serviceFresh() || mutation || current.recording.state === "recording" || !sourcesDirty || !element("sources-form").reportValidity()) return;
    const payload = sourcePayload();
    mutation = "sources";
    stateEpoch += 1;
    text("sources-action-status", "");
    render();
    const started = performance.now();
    try {
      const body = await postJson("/api/sources", payload);
      acceptState(body, started);
      fillSourcesForm();
      text("sources-action-status", "Sources applied. A new observation has started.");
      element("sources-action-status").dataset.tone = "neutral";
      text("recording-action-status", "");
    } catch (error) {
      text("sources-action-status", `Sources : ${error.message}`);
      element("sources-action-status").dataset.tone = "error";
    } finally {
      mutation = null;
      pollResumeAt = performance.now();
      stateEpoch += 1;
      render();
    }
  });
  for (const action of ["start", "stop"]) element(`recording-${action}`).addEventListener("click", async () => {
    if (!serviceFresh() || mutation || element(`recording-${action}`).disabled) return;
    const actionButton = element(`recording-${action}`);
    const restoreFocus = document.activeElement === actionButton;
    const requestedRun = current.run_id;
    mutation = `recording-${action}`;
    stateEpoch += 1;
    text("recording-action-status", "");
    render();
    try {
      const body = await postJson(`/api/recordings/${action}`, {});
      if (!validRecording(body)) throw new Error("Invalid service response.");
      if (current.run_id === requestedRun) current.recording = body;
      text("recording-action-status", body.state === "error" ? `Capture : ${body.error || "incomplete recording."}` : "");
      text("action-status", body.state === "error" ? "" : action === "start" ? "Recording started." : "Recording complete.");
    } catch (error) {
      text("recording-action-status", `MAVLink recording : ${error.message}`);
    } finally {
      mutation = null;
      pollResumeAt = performance.now();
      stateEpoch += 1;
      render();
      if (restoreFocus && document.body.dataset.view === "observation" && inspectorPanel === "recording" && !document.body.classList.contains("focus-mode") && [document.body, actionButton].includes(document.activeElement)) {
        const target = current.recording.state === "recording" ? element("recording-stop") : !element("recording-download").hidden ? element("recording-download") : element("recording-start");
        (target.disabled ? document.querySelector('[data-panel="recording"]') : target).focus();
      }
    }
  });
  element("focus-button").addEventListener("click", () => focusView(!document.body.classList.contains("focus-mode")));
  element("vision-toggle").addEventListener("change", () => {
    visionEnabled = element("vision-toggle").checked;
    frameEpoch += 1;
    clearFrame();
    imageFailure = "";
    render();
  });
  document.addEventListener("argos:framing-ui", event => {
    visionSelection = event.detail || { allowed: false, target_id: null, active: false, paused: false };
    renderVisionSelection();
  });
  new ResizeObserver(layoutVision).observe(element("camera-stage"));
  if (!document.fullscreenEnabled) element("fullscreen-button").hidden = true;
  element("fullscreen-button").addEventListener("click", async () => {
    try {
      text("action-status", "");
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (_error) {
      text("action-status", "The browser did not allow full screen. The console remains usable in this window.");
    }
  });
  document.addEventListener("fullscreenchange", () => {
    const full = Boolean(document.fullscreenElement);
    element("fullscreen-button").setAttribute("aria-pressed", String(full));
    element("fullscreen-button").setAttribute("aria-label", full ? "Exit full screen" : "Show console in full screen");
    element("fullscreen-button").title = full ? "Exit full screen" : "Full screen";
  });
  window.addEventListener("pagehide", () => { stopped = true; clearFrame(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  // Independently expires a frozen endpoint even if repeated requests succeed.
  window.setInterval(render, 100);
  render();
  void pollState();
  void pollFrames();
})();
