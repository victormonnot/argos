/* Hosted console setup. Live capabilities stay disabled; one archive cursor feeds every view. */
(() => {
  "use strict";
  if (document.body.dataset.demo !== "true") return;
  const node = id => document.getElementById(id);
  document.title = node("demo-welcome") ? "Try ARGOS | Interactive Demo" : "ARGOS · Observation";
  node("environment").textContent = "Demo mode";
  node("service-dot").hidden = true;
  node("service-status").textContent = "No vehicle connected";
  node("demo-actions").hidden = false;
  node("hosted-demo-notice").hidden = false;
  node("archive-title").textContent = "Demo recordings";
  node("session-status").textContent = "Demo mode · no vehicle connected";
  node("replay-empty").hidden = true;
  node("demo-open-recording").hidden = false;
  node("view-messages").textContent = "Recorded MAVLink";
  node("demo-playback").append(node("replay-transport"));
  node("demo-playback").hidden = false;
  node("replay-transport").hidden = true;

  // Keep the actual controls discoverable, with an explanation that also works
  // on touch screens and for keyboard users who cannot focus disabled buttons.
  const unavailable = [
    "#control-panel button", "#control-panel input", "#control-panel select",
    "#sources-form input", "#sources-form select", "#sources-form button",
    "[data-reconnect]", "#vision-toggle", "#yaw-preview-clear",
    "#recording-start", "#recording-stop", "#recording-include-visual",
    "#demo-record-button", "#demo-history-inspect",
  ].join(",");
  for (const control of document.querySelectorAll(unavailable)) {
    control.disabled = true;
    const description = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    description.add("demo-unavailable-note");
    control.setAttribute("aria-describedby", [...description].join(" "));
  }
  node("framing-controls").hidden = false;
  node("recording-stop").hidden = false;
  const labels = {
    "view-context": "Recorded Gazebo flight",
    "global-recording-state": "Capture · unavailable",
    "video-status": "Unavailable in demo mode",
    "recording-state": "Unavailable in demo mode",
    "reception-state": "No live sources",
    "reception-summary": "Live reception is unavailable in demo mode. No camera or telemetry link is connected.",
    "telemetry-status": "Unavailable in demo mode",
    "source-telemetry-state": "Unavailable",
    "source-telemetry-endpoint": "No live telemetry link",
    "source-video-state": "Unavailable",
    "source-video-detail": "Live camera input is unavailable in demo mode.",
    "camera-empty-title": "Opening the recorded flight…",
    "camera-empty-detail": "Recorded images and measurements will appear at the selected time.",
    "camera-source": "No live camera",
    "vision-status": "Live detection is unavailable in demo mode",
    "control-environment": "Demo mode",
    "control-authority": "Unavailable in demo mode",
    "control-feedback": "Flight commands require a connected vehicle or simulator.",
    "control-hold-note": "Flight commands are unavailable in demo mode. Use playback controls to inspect recorded actions.",
    "control-mode-note": "Mode and arming are recorded observations at the shared cursor.",
    "control-neutral-note": "Playback cannot change the aircraft or the recorded outcome.",
    "framing-profile-note": "Assistance choices are unavailable in demo mode. Recorded framing state appears above.",
    "framing-response-note": "Distance response cannot be changed during replay.",
    "framing-status": "Unavailable in demo mode. Inspect recorded framing in Sessions.",
    "sources-form-hint": "Unavailable in demo mode. Source configuration requires a local ARGOS installation.",
    "recording-hint": "Unavailable in demo mode. The included recording can be replayed and downloaded in Sessions.",
    "detail-recording-state": "Unavailable",
    "archive-live-recording": "New recordings unavailable in demo mode",
    "demo-shot-title": "Radio data unavailable.",
    "demo-shot-subtitle": "This demo has no live pilot inputs or assistance reports.",
    "demo-history-status": "Radio reports unavailable",
  };
  for (const [id, value] of Object.entries(labels)) node(id).textContent = value;
  document.querySelector(".control-loss-note").textContent = "Install ARGOS to use flight controls with your own simulator or vehicle.";
  for (const button of document.querySelectorAll("[data-demo-sessions]")) {
    button.addEventListener("click", () => document.dispatchEvent(new CustomEvent("argos:show-workspace", { detail: { view: "sessions" } })));
  }
  document.addEventListener("argos:workspace-changed", event => {
    node("demo-unavailable-note").hidden = event.detail.view === "sessions";
    node("view-context").textContent = {
      observation: "Recorded Gazebo flight", control: "Recorded observations · commands unavailable",
      messages: "Recorded MAVLink", sessions: "Recorded Gazebo flight",
    }[event.detail.view];
  });

  const welcome = node("demo-welcome");
  if (welcome) {
    // The server renders an open overlay to cover the first paint. Upgrade it
    // to a modal dialog so the loaded console is inert and focus stays inside.
    welcome.close();
    welcome.showModal();
    node("demo-enter").addEventListener("click", () => welcome.close());
    welcome.addEventListener("close", () => {
      if (!welcome.open) {
        document.title = `ARGOS · ${node("view-title").textContent.trim()}`;
        node("workspace").focus({ preventScroll: true });
      }
    });
  }

  let loading = false;
  let configReady = false;
  document.addEventListener("argos:recorded-state", event => {
    const state = event.detail;
    node("demo-load-status").hidden = state.phase !== "error";
    node("demo-load-status").textContent = state.error || "";
    node("demo-retry").hidden = state.phase !== "error";
  });
  async function start() {
    if (loading) return;
    loading = true;
    node("demo-retry").hidden = true;
    node("demo-load-status").hidden = true;
    node("replay-status").textContent = "Opening the demo recording…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/demo-config", { signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error("The demo is unavailable. Please try again.");
      const config = await response.json();
      if (config.enabled !== true || !/^[0-9a-f]{32}$/.test(config.recording_id)) {
        throw new Error("The demo recording is unavailable. Please try again.");
      }
      configReady = true;
      document.dispatchEvent(new CustomEvent("argos:demo-ready", { detail: {
        recording_id: config.recording_id, initial_at_s: welcome ? 0.5 : 0,
      } }));
    } catch (error) {
      const message = error.name === "AbortError"
        ? "The demo took too long to respond. Please try again."
        : "The demo could not be loaded. Please try again.";
      node("replay-status").textContent = message;
      node("demo-load-status").textContent = message;
      node("demo-load-status").hidden = false;
      node("demo-retry").hidden = false;
    } finally {
      clearTimeout(timeout);
      loading = false;
    }
  }
  node("demo-retry").addEventListener("click", () => {
    if (configReady) document.dispatchEvent(new CustomEvent("argos:recorded-command", { detail: { action: "retry" } }));
    else void start();
  });
  // Archive requests stop on pagehide; returning from the browser cache needs
  // the same fresh initialization as the normal console.
  window.addEventListener("pageshow", event => { if (event.persisted) window.location.reload(); });
  // All archive/report listeners must exist before publishing the configuration.
  document.addEventListener("DOMContentLoaded", () => { void start(); }, { once: true });
})();
