"use strict";

// Present archive observations in the console without entering its live clock,
// command authority, detector or connection state. Sessions owns the cursor,
// validation, cancellation and decoded image URL for this atomic observation.
(() => {
  if (document.body.dataset.demo !== "true") return;
  const node = id => document.getElementById(id);
  const text = (id, value) => { const target = node(id); if (target.textContent !== String(value)) target.textContent = String(value); };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const numeric = (value, unit = "") => finite(value) ? `${number.format(value)}${unit}` : "—";
  const degrees = value => numeric(finite(value) ? value * 180 / Math.PI : null, "°");
  const badge = (id, value, tone = "neutral") => { text(id, value); node(id).dataset.tone = tone; };
  const sampleLabel = view => !view || view.state === "absent" ? "Not recorded at cursor"
    : `${view.state === "stale" ? "Stale" : "Recent"} at cursor · ${numeric(view.rx_age_s, " s")}`;
  const modeLabel = mode => ({ 0: "Stabilize", 2: "AltHold", 9: "Land", 20: "Guided NoGPS" })[mode]
    || (Number.isSafeInteger(mode) ? `Mode ${mode}` : "Not recorded");
  let observation = null;
  let displayedFrame = null;
  node("vision-toggle").closest("label").querySelector("span").textContent = "Recorded detections";

  function layoutDetections() {
    if (!displayedFrame) return;
    const stage = node("camera-stage"), layer = node("vision-layer");
    const scale = Math.min(stage.clientWidth / displayedFrame.width, stage.clientHeight / displayedFrame.height);
    const width = displayedFrame.width * scale, height = displayedFrame.height * scale;
    Object.assign(layer.style, { width: `${width}px`, height: `${height}px`, left: `${(stage.clientWidth - width) / 2}px`, top: `${(stage.clientHeight - height) / 2}px` });
    const labelFloor = document.body.classList.contains("demo-mode") ? node("demo-video-context").offsetHeight + 24 : 0;
    for (const box of layer.children) {
      const label = box.firstElementChild;
      label.style.maxWidth = `${Math.max(0, width - 4)}px`;
      const left = box.offsetLeft, top = box.offsetTop, labelHeight = label.offsetHeight;
      label.style.left = `${Math.max(1 - left, Math.min(0, width - left - label.offsetWidth - 3))}px`;
      const above = top >= labelHeight + 4 ? -labelHeight - 4 : Math.max(0, box.offsetHeight + 3);
      label.style.top = `${Math.min(height - top - labelHeight - 3, Math.max(labelFloor - top, above))}px`;
    }
  }

  function renderImage(value, message) {
    const visual = value?.visual, frame = visual?.frame;
    const visible = Boolean(value?.imageURL && frame?.state === "recent");
    displayedFrame = visible ? frame : null;
    node("camera-image").hidden = !visible;
    node("camera-image-caption").hidden = !visible;
    node("camera-empty").hidden = visible;
    node("demo-open-recording").hidden = visible;
    node("vision-layer").hidden = !visible;
    node("vision-layer").replaceChildren();
    node("vision-toggle").checked = visible;
    if (visible) {
      const image = node("camera-image");
      if (image.getAttribute("src") !== value.imageURL) image.src = value.imageURL;
      image.alt = `Recorded onboard camera image at ${numeric(value.at_s, " seconds")}`;
      const caption = document.createTextNode("Recorded image ");
      const sequence = node("image-sequence");
      sequence.textContent = `n° ${frame.sequence}`;
      node("camera-image-caption").replaceChildren(caption, sequence, document.createTextNode(" · "));
      const target = visual.control?.framing?.target_id;
      for (const detection of frame.detections) {
        const box = document.createElement("div"), label = document.createElement("span");
        box.className = "vision-box";
        box.setAttribute("role", "listitem");
        box.dataset.trackId = String(detection.track_id);
        // Recorded selection is an observation, never an interactive command.
        box.dataset.selected = String(detection.track_id === target);
        label.className = "vision-box-label";
        label.textContent = `Person #${detection.track_id} · ${Math.round(detection.confidence * 100)}% · recorded`;
        const [x, y, width, height] = detection.box;
        Object.assign(box.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${width * 100}%`, height: `${height * 100}%` });
        box.append(label); node("vision-layer").append(box);
      }
      layoutDetections();
    } else node("camera-image").removeAttribute("src");
    const absent = !value ? message : frame?.state === "stale" ? "Recorded image is stale at this cursor"
      : "No image recorded at this cursor";
    text("camera-empty-title", absent);
    text("camera-empty-detail", !value ? "Images and measurements are shown together when the recording is ready."
      : "Move the playback cursor to inspect another moment. Live camera input is unavailable in demo mode.");
    badge("video-status", visible ? "Recorded image" : absent, frame?.state === "stale" ? "warning" : "neutral");
    text("camera-source", "Recorded camera · no live source");
    text("camera-age", frame ? `Age at cursor ${numeric(frame.age_s, " s")}` : "—");
    text("camera-size", visible ? `${frame.width} × ${frame.height}` : "");
    text("vision-status", visible && frame.detections.length ? `${frame.detections.length} recorded detection${frame.detections.length === 1 ? "" : "s"}`
      : visible ? "No detections recorded for this image" : "Recorded detections unavailable at cursor");
    badge("available-video", visible ? `Recorded · ${numeric(frame.age_s, " s")} at cursor` : absent);
    badge("source-video-state", visible ? "Recorded image" : absent);
    text("source-video-detail", "Historical camera observation. This demo does not connect to the original source.");
    text("detail-video-size", frame ? `${frame.width} × ${frame.height}` : "—");
    text("detail-video-age", frame ? `${numeric(frame.age_s, " s")} at cursor` : "—");
    text("count-video-rejected", "Not recorded");
    text("video-last-rejection", "Camera rejection history is unavailable in this recording.");
  }

  function renderTelemetry(value, message) {
    const snapshot = value?.snapshot;
    badge("telemetry-status", snapshot ? "Recorded telemetry" : message);
    badge("source-telemetry-state", snapshot ? "Recorded source" : message);
    text("source-telemetry-id", snapshot ? `System ${snapshot.system} · component ${snapshot.component} · recorded` : "No recorded source at cursor");
    text("telemetry-hint", "Historical measurements at the playback cursor. Ages describe reception at that moment; no live MAVLink link is connected.");
    for (const [kind, id] of [["heartbeat", "heartbeat"], ["battery", "battery"], ["attitude", "attitude"], ["local_position_ned", "position"]]) {
      const view = snapshot?.[kind];
      badge(`detail-${id}-state`, view?.state === "recent" ? "Recent at cursor" : view?.state === "stale" ? "Stale at cursor" : "Not recorded", view?.state === "stale" ? "warning" : "neutral");
      text(`detail-${id}-age`, view ? `${numeric(view.rx_age_s, " s")} at cursor` : "—");
      badge(`available-${kind}`, sampleLabel(view), view?.state === "stale" ? "warning" : "neutral");
    }
    const heartbeat = snapshot?.heartbeat?.fields, attitude = snapshot?.attitude?.fields;
    const position = snapshot?.local_position_ned?.fields, battery = snapshot?.battery;
    const armed = Number.isSafeInteger(heartbeat?.base_mode) ? heartbeat.base_mode & 128 ? "Armed" : "Disarmed" : "Not recorded";
    const mode = heartbeat ? snapshot.mode?.label || modeLabel(heartbeat.custom_mode) : "—";
    text("flight-mode", mode);
    text("armed-preview", `${armed} · recorded`);
    text("heartbeat-status", sampleLabel(snapshot?.heartbeat));
    text("detail-mode-label", mode);
    text("detail-armed", armed);
    text("detail-custom-mode", numeric(heartbeat?.custom_mode));
    text("detail-autopilot", heartbeat && finite(heartbeat.type) && finite(heartbeat.autopilot) ? `${heartbeat.type} / ${heartbeat.autopilot}` : "—");
    text("detail-system-status", Number.isSafeInteger(heartbeat?.system_status) ? `MAV_STATE ${heartbeat.system_status} · recorded` : "Not recorded at cursor");
    text("autopilot-texts-state", "Autopilot message history is unavailable in this archive view.");
    for (const axis of ["roll", "pitch", "yaw"]) {
      text(axis, degrees(attitude?.[axis]));
      text(`detail-${axis}`, degrees(attitude?.[axis]));
    }
    text("attitude-age", sampleLabel(snapshot?.attitude));
    text("detail-angular-rates", attitude ? [attitude.rollspeed, attitude.pitchspeed, attitude.yawspeed].map(v => numeric(finite(v) ? v * 180 / Math.PI : null, "°/s")).join(" / ") : "—");
    text("battery-remaining", numeric(battery?.remaining_percent, " %"));
    text("battery-age", sampleLabel(battery));
    for (const [id, key, unit] of [["remaining", "remaining_percent", " %"], ["voltage", "voltage_v", " V"], ["current", "current_a", " A"]]) text(`detail-battery-${id}`, numeric(battery?.[key], unit));
    for (const [id, key] of [["north", "x"], ["east", "y"], ["down", "z"]]) text(`position-${id}`, numeric(position?.[key], " m"));
    text("position-age", sampleLabel(snapshot?.local_position_ned));
    text("detail-position", position ? [position.x, position.y, position.z].map(v => numeric(v, " m")).join(" / ") : "—");
    text("detail-velocity", position ? [position.vx, position.vy, position.vz].map(v => numeric(v, " m/s")).join(" / ") : "—");
    const progress = { first: "First sample", advanced: "Advancing", repeated: "Repeated value", decreased: "Decreasing value" };
    text("detail-attitude-boot", progress[snapshot?.attitude?.boot_progress] || "—");
    text("detail-position-boot", progress[snapshot?.local_position_ned?.boot_progress] || "—");
    for (const [id, key] of [["rx", "received"], ["accepted", "accepted"], ["rejected", "rejected"], ["other-source", "ignored_source"], ["other-type", "ignored_type"]]) text(`count-${id}`, numeric(snapshot?.[key]));
    for (const id of ["count-bytes", "count-bad-bytes"]) text(id, "Not recorded");
    text("last-rejection", snapshot ? snapshot.last_rejection || "No rejected samples at cursor." : "No recorded reception at cursor.");
    text("details-service-state", "Recorded reception counters up to the cursor. These do not measure radio loss or latency.");
  }

  function renderControl(value) {
    const visual = value?.visual, control = visual?.control, framing = control?.framing;
    const age = finite(visual?.sample?.at_s) ? value.at_s - visual.sample.at_s : null;
    const sampleState = !control ? "No control observation recorded at cursor"
      : visual.state === "ended" ? "Capture ended · last recorded observation"
      : visual.state === "gap" || age > .35 ? "Stale control observation at cursor" : "Recorded control observation";
    text("control-mode", `Mode ${control ? modeLabel(control.vehicle?.mode) : "—"} · recorded`);
    text("control-armed", `${control?.vehicle?.armed === true ? "Armed" : control?.vehicle?.armed === false ? "Disarmed" : "Arming unavailable"} · recorded`);
    text("control-feedback", `${sampleState}${finite(age) ? ` · age ${numeric(age, " s")}` : ""}. Flight commands are unavailable in demo mode.`);
    text("framing-target", Number.isSafeInteger(framing?.target_id) ? `Person #${framing.target_id} · recorded` : "No recorded target");
    const phase = framing?.paused ? "Paused" : framing?.active ? "Active" : typeof framing?.phase === "string" ? framing.phase : "Not recorded";
    const reason = typeof framing?.reason === "string" && framing.reason ? `${framing.reason.replace(/[.!?]$/, "")}. ` : "";
    text("framing-status", framing ? `Recorded framing: ${phase}. ${reason}Controls are unavailable in demo mode.` : "Framing observations unavailable at cursor. Controls are unavailable in demo mode.");
    for (const button of document.querySelectorAll("#framing-controls button[aria-pressed]")) button.setAttribute("aria-pressed", "false");
    const error = value => finite(value) && Math.abs(value) <= 1 ? `${value >= 0 ? "+" : ""}${value.toFixed(2)}` : "—";
    text("framing-error", `${error(framing?.error_x)} / ${error(framing?.error_y)}`);
    text("framing-height", `${numeric(finite(framing?.height) ? framing.height * 100 : null, " %")} / ${numeric(finite(framing?.reference_height) ? framing.reference_height * 100 : null, " %")}`);
    // The radio-oriented Demo view has no Pocket/AP1 reports in this recording.
    // Its camera, archived mode and target are valid independent observations.
    text("demo-mode", control ? `${modeLabel(control.vehicle?.mode)} · recorded` : "Not recorded");
    text("demo-target-state", Number.isSafeInteger(framing?.target_id) ? "Recorded target" : "No recorded target");
    text("demo-target", Number.isSafeInteger(framing?.target_id) ? `Person #${framing.target_id}` : "Unavailable");
  }

  function renderEvents(value) {
    const events = value?.visual?.events || [];
    node("all-events").replaceChildren();
    for (const event of events.slice().reverse()) {
      const row = document.createElement("li"), time = document.createElement("div"), detail = document.createElement("p");
      time.className = "event-meta";
      detail.className = "event-message";
      time.textContent = `+${numeric(event.at_s, " s")}`;
      detail.textContent = `${event.detail}${event.status === "accepted" ? " · Request accepted" : event.status === "refused" ? " · Request refused" : event.status === "sampled" ? " · Sampled observation" : ""}`;
      row.append(time, detail); node("all-events").append(row);
    }
    text("events-context", events.length ? "Recorded requests and observations up to the cursor. A request is not proof that an action was executed." : "No flight events recorded at this cursor.");
  }

  function render() {
    const ready = observation?.phase === "ready";
    const value = ready ? observation : null;
    const message = observation?.phase === "error" ? "Recorded flight unavailable" : "Loading recorded flight…";
    const context = observation?.metadata?.context;
    renderImage(value, message);
    renderTelemetry(value, ready ? "No recorded telemetry component" : message);
    renderControl(value);
    renderEvents(value);
    text("source-telemetry-endpoint", context?.telemetry_endpoint ? `${context.telemetry_endpoint} · capture source` : "Capture endpoint not recorded");
    text("source-video-label", context?.configuration?.video_source === "gazebo" ? "Gazebo camera · recorded" : "Recorded camera");
    text("source-video-endpoint", context?.configuration?.video_endpoint || "Capture endpoint not recorded");
    text("source-video-limit", finite(context?.age_limits_s?.video) ? `${numeric(context.age_limits_s.video, " s")} · capture limit` : "Not recorded");
    badge("reception-state", ready ? "Recorded observations" : message);
    text("reception-summary", ready ? "Availability describes the recording at this cursor. Live source connections are unavailable in demo mode."
      : observation?.error || "Waiting for the recording. No live source is connected.");
    text("session-status", ready ? `Recorded flight · ${observation.playing ? "playing" : "paused"} at +${numeric(observation.at_s, " s")}` : message);
  }

  document.addEventListener("argos:recorded-state", event => {
    observation = event.detail;
    render();
  });
  document.addEventListener("argos:workspace-changed", () => { requestAnimationFrame(layoutDetections); });
  node("demo-button").addEventListener("click", () => { requestAnimationFrame(layoutDetections); });
  new ResizeObserver(layoutDetections).observe(node("camera-stage"));
})();
