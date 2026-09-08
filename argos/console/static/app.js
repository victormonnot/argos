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
  const number = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
  const integer = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
  const ageText = (age) => finite(age) ? `${number.format(Math.max(0, age))} s` : "—";
  const numeric = (value, unit = "") => finite(value) ? `${number.format(value)}${unit}` : "—";
  const states = { absent: "Absent", recent: "Récent", stale: "Périmé", unconfigured: "Non configurée", waiting: "En attente", receiving: "En réception", error: "Erreur", reconnecting: "Réouverture en cours" };
  const bootLabels = { first: "Premier échantillon", advanced: "En progression", repeated: "Valeur répétée", decreased: "Valeur en recul" };
  let current = null;
  let lastReceived = null;
  let lastAdvance = null;
  let stateTransitMs = 0;
  let requestFailure = "";
  let imageFailure = "";
  let frame = null;
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
    const title = { observation: "Observation", control: "Pilotage", messages: "MAVLink en direct", sessions: "Sessions" }[view];
    text("view-title", title);
    document.title = `ARGOS · ${title}`;
    text("view-context", { observation: "Réceptions en cours", control: "Vol manuel · sans GPS", messages: "Inspection du flux actuel", sessions: "Historique · relecture" }[view]);
    const skip = document.querySelector(".skip-link");
    skip.href = { observation: "#workspace", control: "#control-panel", messages: "#messages-workspace", sessions: "#sessions-workspace" }[view];
    skip.textContent = { observation: "Aller à l’observation", control: "Aller aux commandes de vol", messages: "Aller aux messages en direct", sessions: "Aller aux sessions" }[view];
    document.dispatchEvent(new CustomEvent("argos:workspace-changed", { detail: { view, previous } }));
    renderPanel();
    if (view === "messages") element("messages-workspace").focus({ preventScroll: true });
    else if (restore && previous === "messages") {
      const target = workspaceOpener?.isConnected && workspaceOpener.getClientRects().length ? workspaceOpener : element("view-observation");
      target.focus({ preventScroll: true });
    }
  }

  function renderPanel() {
    const titles = { telemetry: "Télémétrie MAVLink", video: "Vidéo", recording: "Journal MAVLink", reception: "État des réceptions", sources: "Configurer les sources" };
    text("inspector-kind", inspectorPanel === "sources" ? "RÉGLAGES" : "INSPECTION");
    text("inspector-title", titles[inspectorPanel] || "Inspecteur");
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
    text("focus-button", expanded ? "Rétablir le panneau" : "Vue étendue");
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
    if (!validState(body) || !validReception(body.reception) || (body.reconnecting != null && !["video", "mavlink"].includes(body.reconnecting)) || [body.video.source_id, body.telemetry.connection_id].some((id) => id !== undefined && (typeof id !== "string" || !id.length))) throw new Error("Réponse du service invalide");
    const received = performance.now();
    if (!current && !panelChosen && body.environment === "unconfigured") inspectorPanel = "sources";
    if (!current || body.run_id !== current.run_id) {
      clearFrame();
      lastAdvance = started;
      imageFailure = "";
      eventSignatures.clear();
    } else if (body.at < current.at) {
      throw new Error("Horloge du service en recul");
    } else if (body.at > current.at) {
      lastAdvance = started;
    }
    if (current && body.video.source_id !== current.video.source_id) {
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
        if (pollingAllowed() && requestedEpoch === stateEpoch) requestFailure = error.name === "AbortError" ? "Le service ne répond pas à temps." : "Impossible de lire l’état du service.";
      }
      render();
      await delay(Math.max(0, STATE_INTERVAL_MS - (performance.now() - started)));
    }
  }

  async function decodeImage(url) {
    const probe = new Image();
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => { probe.src = ""; reject(new Error("Décodage trop long")); }, REQUEST_TIMEOUT_MS);
      probe.onload = () => { window.clearTimeout(timer); resolve(); };
      probe.onerror = () => { window.clearTimeout(timer); reject(new Error("Image illisible")); };
      probe.src = url;
    });
  }

  async function pollFrames() {
    while (!stopped) {
      const started = performance.now();
      if (serviceFresh() && current.video.state === "recent") {
        const requestedRun = current.run_id;
        const requestedVideo = current.video.source_id;
        let candidateUrl = null;
        try {
          const { response, body } = await getResponse("/api/frame.jpg");
          const sequenceHeader = response.headers.get("X-Frame-Sequence");
          const receivedHeader = response.headers.get("X-Frame-Received-At");
          const sequence = sequenceHeader === null ? NaN : Number(sequenceHeader);
          const receivedAt = receivedHeader === null ? NaN : Number(receivedHeader);
          const responseRun = response.headers.get("X-Run-Id");
          if (!serviceFresh() || current.run_id !== requestedRun || responseRun !== requestedRun || current.video.source_id !== requestedVideo || (requestedVideo && response.headers.get("X-Video-Id") !== requestedVideo)) continue;
          if (!Number.isSafeInteger(sequence) || sequence < 0 || !finite(receivedAt) || receivedAt < 0 || !body.type.startsWith("image/jpeg")) throw new Error("Réponse image invalide");
          if (frame && sequence <= frame.sequence) continue;
          candidateUrl = URL.createObjectURL(body);
          await decodeImage(candidateUrl);
          if (!serviceFresh() || current.run_id !== requestedRun || current.video.source_id !== requestedVideo) continue;
          const now = performance.now();
          const previousUrl = frame?.url;
          frame = { sequence, receivedAt, observedAt: now, initialAge: Math.max(0, runTime(now) - receivedAt), url: candidateUrl };
          element("camera-image").src = candidateUrl;
          candidateUrl = null;
          if (previousUrl) URL.revokeObjectURL(previousUrl);
          imageFailure = "";
        } catch (error) {
          if (current?.run_id === requestedRun && current.video.source_id === requestedVideo) imageFailure = error.name === "AbortError" ? "Le flux image ne répond pas à temps." : "L’image n’est pas disponible ou n’a pas pu être décodée.";
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
      empty.textContent = "Aucun événement reçu.";
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
      level.textContent = { info: "Information", warning: "Attention", error: "Erreur" }[event.level] || "Événement";
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
    text("config-video-label", video === "device" ? "Périphérique caméra Linux" : "Topic image Gazebo");
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
        const detail = typeof body.detail === "string" ? body.detail : typeof body.error === "string" ? body.error : `Le service a refusé la demande (HTTP ${response.status}).`;
        throw new Error(detail);
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) throw new Error("Réponse non confirmée. Vérifiez l’état de la session avant de réessayer.");
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
    text("sources-form-hint", !fresh ? "Le service doit être connecté pour modifier les sources." : mutation === "sources" ? "Application des sources en cours…" : active ? "Arrêtez le journal MAVLink avant de changer les sources." : sourcesDirty ? "Réglages modifiés, pas encore appliqués." : "Ces réglages correspondent à la configuration active.");
    element("recording-start").disabled = blocked || active || !current || ["unconfigured", "error", "reconnecting"].includes(current.telemetry.state);
    // Stopping stays available after a telemetry fault while the service is
    // reachable; the server decides whether the journal can be finalized.
    element("recording-stop").disabled = blocked || !active;
    element("recording-start").hidden = active;
    element("recording-stop").hidden = !active;
    const interrupted = recording?.state === "complete" && ["transport_error", "shutdown"].includes(recording.end_reason);
    const atLimit = recording?.state === "complete" && ["event_limit", "size_limit"].includes(recording.end_reason);
    const labels = { idle: "Prêt", recording: "Enregistrement", complete: interrupted ? "Interrompu" : atLimit ? "Limite atteinte" : "Terminé", error: "Erreur" };
    const tone = !fresh ? "neutral" : recording.state === "error" ? "error" : interrupted || atLimit ? "warning" : active ? "positive" : "neutral";
    badge("recording-state", fresh ? labels[recording.state] : "Non actualisé", tone);
    const globalLabel = !fresh ? "Capture · non actualisée" : active ? "Capture en cours" : recording.state === "error" ? "Capture en erreur" : interrupted ? "Capture interrompue" : atLimit ? "Capture · limite atteinte" : recording.state === "complete" ? "Capture terminée" : "Capture prête";
    badge("global-recording-state", globalLabel, tone);
    element("global-recording").dataset.tone = tone;
    element("global-recording-dot").dataset.tone = tone;
    element("global-recording").title = !fresh ? "L’état de capture n’est plus actualisé. Ouvrir le journal MAVLink." : `${globalLabel}${recording.end_detail || recording.error ? ` · ${recording.end_detail || recording.error}` : ""}. Ouvrir le journal MAVLink.`;
    text("recording-limits", fresh && Number.isSafeInteger(recording.max_events) && Number.isSafeInteger(recording.max_bytes) ? `Clôture automatique à ${integer.format(recording.max_events)} messages ou ${numeric(recording.max_bytes / 1048576, " Mio")}. Taille actuelle : ${numeric(recording.size_bytes / 1048576, " Mio")}.` : "");
    badge("detail-recording-state", element("recording-state").textContent, element("recording-state").dataset.tone);
    badge("archive-live-recording", !fresh ? "État de la capture non actualisé" : active ? `Capture en cours · ${integer.format(recording.events)} trames` : recording.state === "error" ? "Capture en erreur · consulter le journal" : interrupted || atLimit ? `${globalLabel} · journal disponible` : "Aucune capture en cours", element("recording-state").dataset.tone);
    text("journal-detail-title", active ? "Capture en cours" : recording?.id ? "Dernier journal du service" : "Enregistrement des réceptions");
    text("recording-id", recording?.id || "Aucun journal");
    text("recording-events", fresh ? integer.format(recording.events) : "—");
    const end = active ? runTime(now) : recording?.ended_at;
    text("recording-duration", fresh && finite(recording.started_at) && finite(end) ? ageText(end - recording.started_at) : "—");
    text("recording-hint", !fresh ? "Le service doit être connecté pour agir sur le journal." : mutation?.startsWith("recording") ? "Demande en cours…" : recording.state === "error" ? `Échec de l’écriture : ${recording.error || "le journal ne peut pas être finalisé."}` : active ? "Capture en cours. Arrêter finalise le journal téléchargeable." : interrupted || atLimit ? `${interrupted ? "Capture interrompue" : "Limite de capture atteinte"}. ${recording.end_detail || ""} Le journal finalisé reste téléchargeable et consultable dans Sessions.` : current.telemetry.state === "unconfigured" ? "Configurez une source MAVLink dans Sources pour enregistrer ses réceptions." : current.telemetry.state === "error" ? "La liaison MAVLink doit être rétablie avant une nouvelle capture." : recording.state === "complete" ? "Journal finalisé. Il reste disponible dans Sessions, même après une nouvelle capture." : "Prêt à enregistrer les prochaines réceptions MAVLink.");
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
    const unavailable = expectedPause() ? "Actualisation en attente" : "Service inaccessible";
    const env = { simulation: "Simulation", real: "Réel déclaré", unconfigured: "Aucun environnement configuré" }[environment] || "Environnement inconnu";
    text("source-environment", fresh ? env : `${env} · dernière configuration reçue, ${unavailable.toLowerCase()}`);
    text("source-video-label", video.source === "none" ? "Aucune caméra configurée" : video.label);
    text("source-video-endpoint", video.endpoint || "Non configuré");
    text("source-video-limit", ageText(video.age_limit_s));
    text("source-video-detail", video.detail || "Aucun détail disponible.");
    badge("source-video-state", fresh ? (states[video.state] || video.state) : unavailable, fresh && video.state === "recent" ? "positive" : fresh && video.state === "error" ? "error" : "neutral");
    text("source-telemetry-endpoint", telemetry.endpoint || "Non configuré");
    text("source-telemetry-id", `Système ${telemetry.system} · composant ${telemetry.component}`);
    const effectiveTelemetryState = telemetryState(performance.now());
    badge("source-telemetry-state", fresh ? (states[effectiveTelemetryState] || effectiveTelemetryState) : unavailable, fresh && effectiveTelemetryState === "receiving" ? "positive" : fresh && effectiveTelemetryState === "error" ? "error" : fresh && effectiveTelemetryState === "stale" ? "warning" : "neutral");
    text("active-source-summary", `${video.label} · ${telemetry.endpoint || "MAVLink non configuré"}`);
    text("detail-video-size", fresh && finite(video.width) && finite(video.height) ? `${video.width} × ${video.height}` : "—");
    text("detail-video-age", fresh ? ageText(currentFrameAge(performance.now())) : "—");
    badge("source-video-state", element("video-status").textContent, element("video-status").dataset.tone);
    text("video-last-rejection", fresh ? video.last_rejection || "Aucune image refusée." : "Refus non actualisés.");
  }

  function renderDetails(fresh, now) {
    if (!current) return;
    const t = current.telemetry;
    text("details-service-state", fresh ? "Compteurs de cette observation. Ils ne mesurent ni les pertes ni la latence radio." : "État non actualisé : les valeurs sont masquées. Les événements conservés décrivent la dernière observation reçue.");
    for (const [id, value] of Object.entries({ "count-rx": t.rx_messages, "count-bytes": t.rx_bytes, "count-accepted": t.accepted, "count-rejected": t.rejected, "count-other-source": t.ignored_source, "count-other-type": t.ignored_type, "count-bad-bytes": t.bad_bytes, "count-video-rejected": current.video.rejected })) text(id, fresh && finite(value) ? integer.format(value) : "—");
    const rejection = t.last_rejection;
    text("last-rejection", fresh ? (rejection || "Aucun refus signalé.") : "Refus non actualisés.");
    for (const [name, view] of [["heartbeat", t.heartbeat], ["position", t.local_position_ned], ["battery", t.battery], ["attitude", t.attitude]]) {
      const recent = fresh && viewRecent(view, now);
      badge(`detail-${name}-state`, fresh ? (t.state === "error" ? "Liaison interrompue" : states[viewState(view, now)] || "Absent") : "Non actualisé", recent ? "positive" : fresh && viewState(view, now) === "stale" ? "warning" : "neutral");
      text(`detail-${name}-age`, fresh ? ageText(stateAge(view, now)) : "—");
    }
    const heartbeat = fresh && viewRecent(t.heartbeat, now) ? t.heartbeat.fields : null;
    const mode = fresh && viewRecent(t.mode, now) ? t.mode : null;
    text("detail-mode-label", mode ? mode.known ? mode.label : finite(mode.custom_mode) ? `Inconnu · brut ${mode.custom_mode}` : "Inconnu" : "—");
    text("detail-armed", Number.isSafeInteger(heartbeat?.base_mode) ? ((heartbeat.base_mode & 128) !== 0 ? "Armé" : "Désarmé") : "—");
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
    text("events-context", fresh ? "Les 60 derniers événements de réception, caméra et MAVLink." : "Historique conservé de la dernière observation reçue. Les réceptions ne sont plus actualisées.");
  }

  const textReasons = { assembling: "Fragments attendus", timeout: "Délai entre fragments dépassé", missing_chunk: "Fragment manquant", conflicting_chunk: "Fragments contradictoires", severity_changed: "Sévérité modifiée pendant l’assemblage", restarted: "Identifiant réutilisé", limit: "Limite d’assemblage atteinte", reconnect: "Liaison rouverte avant la fin" };
  let autopilotTextSignature = "";

  function renderAutopilotStatus(fresh, now) {
    const status = current.telemetry.autopilot_status;
    const declaration = status?.declaration;
    const age = finite(declaration?.rx_age_s) ? Math.max(declaration.rx_age_s + Math.max(0, runTime(now) - current.at), finite(declaration.received_at) ? runTime(now) - declaration.received_at : 0) : null;
    const recent = fresh && !["error", "reconnecting"].includes(current.telemetry.state) && declaration?.state === "recent" && finite(age) && age <= declaration.age_limit_s;
    text("detail-system-status", recent && typeof declaration.label === "string" ? `${declaration.label} · déclaré` : !fresh ? "Non actualisé" : declaration?.state === "stale" || finite(age) && age > declaration.age_limit_s ? "Déclaration ancienne" : "Non renseigné");
    element("detail-system-status").title = recent && typeof declaration.name === "string" ? declaration.name : "";
    const history = status?.texts;
    const entries = Array.isArray(history?.entries) ? history.entries.slice(0, 60).filter((entry) => entry && Number.isSafeInteger(entry.id) && typeof entry.text === "string" && typeof entry.severity_label === "string" && typeof entry.connection_id === "string" && [entry.received_at, entry.first_received_at, entry.rx_age_s].every((value) => finite(value) && value >= 0) && [entry.system, entry.component, entry.severity, entry.chunks].every(Number.isSafeInteger)) : [];
    text("autopilot-texts-summary", `Messages de l’autopilote · ${entries.length}`);
    text("autopilot-texts-state", !history ? "Historique non communiqué par ce service." : !fresh ? "Historique conservé · service non actualisé. Les âges actuels ne sont pas disponibles." : entries.length ? `Système ${history.system} · composant ${history.component} · du plus récent au plus ancien.` : "Aucun STATUSTEXT reçu pour ce composant dans cette observation.");
    if (!element("autopilot-texts").open) return;
    const signature = JSON.stringify([history?.connection_id, entries.map(({ rx_age_s, ...entry }) => entry)]);
    if (signature !== autopilotTextSignature) {
      autopilotTextSignature = signature;
      const fragment = document.createDocumentFragment();
      for (const entry of entries) {
        const item = document.createElement("li");
        const heading = document.createElement("p");
        heading.className = "autopilot-text-heading";
        heading.textContent = `${entry.severity_label} · sévérité ${entry.severity}`;
        const body = document.createElement("p");
        body.className = "autopilot-text-body";
        body.textContent = entry.text;
        const origin = document.createElement("p");
        origin.className = "microcopy";
        origin.textContent = `Système ${entry.system} · composant ${entry.component} · réception T + ${ageText(entry.received_at)}${entry.connection_id !== history.connection_id ? " · connexion précédente" : ""}`;
        origin.title = `Connexion ${entry.connection_id}`;
        const received = document.createElement("p");
        received.className = "microcopy autopilot-text-age";
        received.dataset.id = String(entry.id);
        const completeness = document.createElement("p");
        completeness.className = "microcopy";
        completeness.textContent = `${entry.complete ? "Texte complet" : `Texte incomplet · ${textReasons[entry.reason] || "assemblage non terminé"}`}${entry.utf8_valid === false ? " · UTF-8 incomplet ou invalide" : ""}`;
        item.append(heading, body, origin, received, completeness);
        fragment.append(item);
      }
      element("autopilot-text-list").replaceChildren(fragment);
    }
    for (const received of element("autopilot-text-list").querySelectorAll(".autopilot-text-age")) {
      const entry = entries.find((item) => String(item.id) === received.dataset.id);
      if (!entry) continue;
      const value = fresh ? `Reçu il y a ${ageText(Math.max(entry.rx_age_s, runTime(now) - entry.received_at))}` : "Ancienneté non actualisée";
      if (received.textContent !== value) received.textContent = value;
    }
    text("autopilot-texts-limits", history ? `Historique borné à ${Number.isSafeInteger(history.max_entries) ? history.max_entries : 60} textes ; ${integer.format(history.evicted_entries || 0)} anciens retirés. ${integer.format(history.pending || 0)} assemblages en attente ; ${integer.format(history.rejected || 0)} fragments refusés.${history.last_rejection ? ` Dernier refus : ${history.last_rejection}` : ""}` : "");
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
    const imageAvailable = fresh && video.state === "recent" && frame !== null && currentFrameAge(now) <= video.age_limit_s;
    const viewNames = { heartbeat: "mode", battery: "batterie", attitude: "attitude", local_position_ned: "position locale" };
    const usable = imageAvailable ? ["image"] : [];
    badge("available-video", !fresh ? "Non actualisée" : imageAvailable ? "Récente" : states[video.state] === "Récent" ? "Affichage en attente" : states[video.state], imageAvailable ? "positive" : "neutral");
    for (const name of Object.keys(viewNames)) {
      const recent = fresh && viewRecent(t[name], now);
      if (recent) usable.push(viewNames[name]);
      badge(`available-${name}`, !fresh ? "Non actualisé" : recent ? `Récent · ${ageText(stateAge(t[name], now))}` : t.state === "error" ? "Liaison interrompue" : t.state === "reconnecting" ? "Réouverture" : t[name].state === "absent" ? "Non reçu" : `Périmé · ${ageText(stateAge(t[name], now))}`, recent ? "positive" : fresh && t[name].state === "stale" ? "warning" : "neutral");
    }
    text("reception-summary", actionPending ? "Actualisation en attente pendant l’action demandée. Les données trop anciennes restent masquées." : !fresh ? "Service inaccessible : la disponibilité des sources ne peut plus être vérifiée. Réessai automatique en cours." : usable.length ? `Réceptions récentes disponibles : ${usable.join(", ")}.` : "Aucune image ni mesure récente disponible actuellement.");
    const displayFailure = fresh && video.state === "recent" && !imageAvailable && Boolean(imageFailure || (frame && currentFrameAge(now) > video.age_limit_s));
    const shown = !fresh && serviceLostAt !== null ? [{ id: "service", source: "service", title: "Service ARGOS inaccessible", detail: "Vérifiez que le service local est lancé. Cela ne permet pas de conclure à une coupure de la caméra ou de MAVLink.", state: "active" }] : issues.slice();
    if (displayFailure) shown.push({ id: "display", source: "display", title: "Affichage vidéo interrompu", detail: "Le service reçoit des images, mais leur affichage ne se renouvelle plus dans ce navigateur. La console réessaie automatiquement.", state: "active" });
    const signature = JSON.stringify(shown.map(({ since, ...item }) => item));
    if (signature !== issueSignature) {
      issueSignature = signature;
      const rows = shown.map((item) => {
        const row = document.createElement("li");
        row.dataset.incident = item.id;
        const title = document.createElement("h4");
        title.textContent = item.state === "recovering" ? "Reprise en cours de confirmation" : item.title;
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
      const label = item.source === "service" ? `Depuis ${ageText((now - serviceLostAt) / 1000)}` : finite(item.since) ? `Observé depuis ${ageText(elapsed - item.since)}` : "Réception caméra et affichage sont vérifiés séparément.";
      if (row.lastChild.textContent !== label) row.lastChild.textContent = label;
    }
    const recoveryLabel = recovery ? `${recovery.source === "video" ? "Images" : "Réceptions MAVLink"} rétablies · interruption observée ${ageText(recovery.duration_s)} · T + ${number.format(recovery.at)} s.` : "";
    text("reception-recovery", fresh ? recoveryLabel : "");
    let noticeTitle = "", noticeAge = "", tone = "warning";
    if (actionPending) {
      noticeTitle = "Action en cours · actualisation en attente";
      tone = "neutral";
    } else if (shown.length) {
      noticeTitle = shown.length > 1 ? `${shown.length} réceptions à vérifier` : shown[0].state === "recovering" ? "Reprise en cours de confirmation" : shown[0].title;
      noticeAge = shown[0].source === "service" ? `depuis ${ageText((now - serviceLostAt) / 1000)}` : finite(shown[0].since) ? `depuis ${ageText(elapsed - shown[0].since)}` : "";
    } else if (fresh && reconnecting) {
      noticeTitle = reconnecting === "video" ? "Réouverture de la caméra…" : "Réouverture MAVLink…";
    } else if (fresh && recovery && elapsed - recovery.at <= 10) {
      noticeTitle = recovery.source === "video" ? "Images reçues à nouveau" : "Réceptions MAVLink rétablies";
      tone = "positive";
    } else if (fresh && serviceRecovery && now - serviceRecovery.at <= 10000) {
      noticeTitle = "Service de nouveau accessible";
      noticeAge = `interruption ${ageText(serviceRecovery.duration)}`;
      tone = "positive";
    }
    const recoveryCount = shown.filter((issue) => issue.state === "recovering").length;
    const receptionLabel = actionPending ? "Actualisation en attente" : !fresh ? current ? "Non actualisé" : "En attente" : shown.length ? recoveryCount === shown.length ? "Reprise en cours" : `${shown.length} incident${shown.length > 1 ? "s" : ""} en cours` : reconnecting ? "Réouverture en cours" : recovery && elapsed - recovery.at <= 10 ? "Réception rétablie" : "Aucun incident de réception";
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
        button.textContent = reconnecting === source ? "Réouverture en cours…" : source === "video" ? "Réouvrir la caméra" : "Réouvrir MAVLink";
      }
      const hint = !fresh ? "Réouverture disponible lorsque le service est accessible." : !configured ? `${source === "video" ? "Caméra" : "MAVLink"} : source à configurer.` : journal ? "Arrêtez le journal avant de réouvrir MAVLink. La capture ne redémarre pas automatiquement." : source === "video" ? "Réouvre la caméra avec les réglages actifs ; MAVLink reste en réception." : "Réouvre le récepteur MAVLink ; la caméra reste en réception. Les compteurs MAVLink repartent de zéro.";
      for (const node of document.querySelectorAll(`[data-reconnect-hint="${source}"]`)) if (node.textContent !== hint) node.textContent = hint;
    }
  }

  function render() {
    const now = performance.now();
    const fresh = serviceFresh(now);
    document.dispatchEvent(new CustomEvent("argos:control-state", { detail: {
      control: current?.control ?? null, fresh, run_id: current?.run_id ?? null,
      environment: current?.environment ?? null,
    } }));
    const actionPending = !fresh && expectedPause(now);
    const connecting = !current && !requestFailure;
    renderControls(fresh, now);
    renderPanel();
    renderReception(fresh, now);
    text("service-status", fresh ? "Service connecté" : actionPending ? "Action en cours…" : connecting ? "Connexion au service…" : "Service inaccessible");
    element("service-dot").dataset.tone = fresh ? "positive" : connecting || actionPending ? "neutral" : "error";
    text("environment", current ? ({ simulation: "SIMULATION", real: "RÉEL DÉCLARÉ", unconfigured: "SOURCES À CONFIGURER" }[current.environment] || "ENVIRONNEMENT INCONNU") : "Aucune session reçue");
    if (!fresh) {
      element("camera-image").hidden = true;
      element("camera-image-caption").hidden = true;
      element("camera-empty").hidden = false;
      badge("video-status", connecting ? "En attente" : "Non actualisée", connecting ? "neutral" : "warning");
      text("camera-empty-title", actionPending ? "Actualisation en attente" : connecting ? "Connexion au service…" : "La console ne reçoit plus d’état");
      text("camera-empty-detail", actionPending ? "L’action demandée est en cours. L’image et les valeurs trop anciennes sont masquées jusqu’au prochain état reçu." : connecting ? "La console attend l’état de ses sources." : "Vérifiez que le service ARGOS est lancé et accessible. L’image et les valeurs sont masquées jusqu’au rétablissement de la réception.");
      text("camera-source", current?.video.label || "Non disponible");
      text("camera-age", "—");
      text("camera-size", "");
      badge("telemetry-status", connecting ? "En attente" : "Non actualisée", connecting ? "neutral" : "warning");
      text("telemetry-hint", connecting ? "L’état de la réception apparaîtra ici." : actionPending ? "Action en cours. Les valeurs attendent un nouvel état du service." : "Service inaccessible. Les dernières valeurs ne sont pas présentées comme actuelles.");
      for (const id of ["roll", "pitch", "yaw", "flight-mode", "armed-preview", "battery-remaining", "position-north", "position-east", "position-down"]) text(id, "—");
      text("attitude-age", "Non actualisée");
      text("battery-age", "Non actualisée");
      text("position-age", "Non actualisée");
      text("heartbeat-status", "Non actualisé");
      text("session-status", current ? "Observation interrompue · réception non actualisée" : "Aucune observation reçue");
      renderSources(false);
      renderDetails(false, now);
      return;
    }

    const { video, telemetry } = current;
    const age = currentFrameAge(now);
    const visible = video.state === "recent" && frame !== null && age <= video.age_limit_s;
    element("camera-image").hidden = !visible;
    element("camera-image-caption").hidden = !visible;
    element("camera-empty").hidden = visible;
    const frameExpired = frame !== null && age > video.age_limit_s;
    let videoLabel = states[video.state] || "État inconnu";
    let emptyTitle = "En attente d’une image";
    let emptyDetail = video.detail || "La source est configurée. Vérifiez que la caméra du drone transmet vers le point de réception indiqué dans Sources.";
    if (video.state === "unconfigured") {
      emptyTitle = "Aucune caméra configurée";
      emptyDetail = "Configurez la caméra Gazebo ou la caméra du drone dans Sources. Son image apparaîtra ici dès réception.";
    } else if (video.state === "reconnecting") {
      emptyTitle = "Réouverture de la caméra…";
    } else if (video.state === "error") {
      emptyTitle = "La caméra signale une erreur";
    } else if (video.state === "stale" || frameExpired) {
      videoLabel = "Image périmée";
      emptyTitle = "L’image ne se renouvelle plus";
      emptyDetail = "La dernière image dépasse la limite d’ancienneté. Vérifiez le flux de la caméra ; l’affichage reprendra à réception d’une image récente.";
    } else if (video.state === "recent" && !frame) {
      videoLabel = "Image en attente";
      emptyDetail = imageFailure || "Le service reçoit des images. La console attend une image décodable.";
    }
    badge("video-status", visible ? "Image récente" : videoLabel, visible ? "positive" : video.state === "error" ? "error" : video.state === "stale" || frameExpired ? "warning" : "neutral");
    text("camera-empty-title", emptyTitle);
    text("camera-empty-detail", emptyDetail);
    text("camera-source", video.source === "none" ? "Non configurée" : video.label);
    text("camera-age", frame ? ageText(age) : "—");
    text("camera-size", visible && finite(video.width) && finite(video.height) ? `${video.width} × ${video.height}` : "");
    text("image-sequence", frame ? `n° ${integer.format(frame.sequence)}` : "");
    const attitude = telemetry.attitude;
    const effectiveTelemetryState = telemetryState(now);
    badge("telemetry-status", effectiveTelemetryState === "error" ? "Liaison interrompue" : states[effectiveTelemetryState] || "État inconnu", effectiveTelemetryState === "receiving" ? "positive" : effectiveTelemetryState === "error" ? "error" : effectiveTelemetryState === "stale" ? "warning" : "neutral");
    text("telemetry-hint", telemetry.detail || "Les réceptions sont filtrées sur le système et le composant sélectionnés.");
    text("attitude-age", telemetry.state === "error" ? "Liaison interrompue" : attitude.state === "absent" ? "Aucune donnée" : `${states[viewState(attitude, now)] || attitude.state} · ${ageText(stateAge(attitude, now))}`);
    for (const name of ["roll", "pitch", "yaw"]) text(name, viewRecent(attitude, now) && finite(attitude.fields?.[name]) ? numeric(attitude.fields[name] * 180 / Math.PI, "°") : "—");
    text("heartbeat-status", telemetry.state === "error" ? "Liaison interrompue" : telemetry.heartbeat.state === "absent" ? "Absent" : `${states[viewState(telemetry.heartbeat, now)] || telemetry.heartbeat.state} · ${ageText(stateAge(telemetry.heartbeat, now))}`);
    const heartbeat = viewRecent(telemetry.heartbeat, now) ? telemetry.heartbeat.fields : null;
    text("armed-preview", Number.isSafeInteger(heartbeat?.base_mode) ? `${heartbeat.base_mode & 128 ? "Armé" : "Désarmé"} déclaré` : "Armement non actualisé");
    const mode = viewRecent(telemetry.mode, now) ? telemetry.mode : null;
    text("flight-mode", mode ? (mode.known ? (mode.label || "—") : finite(mode.custom_mode) ? `Inconnu · brut ${integer.format(mode.custom_mode)}` : "Inconnu") : "—");
    text("battery-remaining", viewRecent(telemetry.battery, now) ? numeric(telemetry.battery.remaining_percent, " %") : "—");
    text("battery-age", telemetry.state === "error" ? "Liaison interrompue" : telemetry.battery.state === "absent" ? "Aucune donnée" : `${states[viewState(telemetry.battery, now)] || telemetry.battery.state} · ${ageText(stateAge(telemetry.battery, now))}`);
    const position = telemetry.local_position_ned;
    const positionFields = viewRecent(position, now) ? position.fields : null;
    for (const [id, axis] of [["position-north", "x"], ["position-east", "y"], ["position-down", "z"]]) text(id, numeric(positionFields?.[axis], " m"));
    text("position-age", telemetry.state === "error" ? "Liaison interrompue" : position.state === "absent" ? "Aucune donnée" : `${states[viewState(position, now)] || position.state} · ${ageText(stateAge(position, now))}`);
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
      if (!validState(body)) throw new Error("Réponse du service invalide.");
      // Keep polling throughout the reopen. A newer GET may already exist;
      // never overwrite it with the state carried by this slower action.
      const state = source === "video" ? body.video.state : body.telemetry.state;
      const detail = source === "video" ? body.video.detail : body.telemetry.detail;
      const message = state === "error" ? `Réouverture impossible : ${detail}` : "Récepteur réouvert. La réception de nouvelles données reste à vérifier.";
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
    text("sources-action-status", "Configuration active reprise. Aucun changement envoyé au service.");
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
      text("sources-action-status", "Sources appliquées. Une nouvelle observation a commencé.");
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
      if (!validRecording(body)) throw new Error("Réponse du service invalide.");
      if (current.run_id === requestedRun) current.recording = body;
      text("recording-action-status", body.state === "error" ? `Capture : ${body.error || "journal incomplet."}` : "");
      text("action-status", body.state === "error" ? "" : action === "start" ? "Journal démarré." : "Journal terminé.");
    } catch (error) {
      text("recording-action-status", `Journal MAVLink : ${error.message}`);
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
  if (!document.fullscreenEnabled) element("fullscreen-button").hidden = true;
  element("fullscreen-button").addEventListener("click", async () => {
    try {
      text("action-status", "");
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (_error) {
      text("action-status", "Le navigateur n’a pas autorisé le plein écran. La console reste utilisable dans cette fenêtre.");
    }
  });
  document.addEventListener("fullscreenchange", () => {
    const full = Boolean(document.fullscreenElement);
    element("fullscreen-button").setAttribute("aria-pressed", String(full));
    element("fullscreen-button").setAttribute("aria-label", full ? "Quitter le plein écran" : "Afficher la console en plein écran");
    element("fullscreen-button").title = full ? "Quitter le plein écran" : "Plein écran";
  });
  window.addEventListener("pagehide", () => { stopped = true; clearFrame(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
  // Independently expires a frozen endpoint even if repeated requests succeed.
  window.setInterval(render, 100);
  render();
  void pollState();
  void pollFrames();
})();
