/* Public archive entry point. The normal local console does not run this file. */
(() => {
  "use strict";
  if (document.body.dataset.demo !== "true") return;
  const node = id => document.getElementById(id);
  document.body.dataset.view = "sessions";
  document.title = "ARGOS · Interactive demo";
  node("view-title").textContent = "Interactive demo";
  node("environment").textContent = "Demo mode";
  node("service-dot").hidden = true;
  node("service-status").textContent = "Recorded simulation";
  node("demo-actions").hidden = false;
  node("hosted-demo-notice").hidden = false;
  node("workspace").hidden = true;
  node("sessions-workspace").hidden = false;
  node("archive-title").textContent = "Demo recordings";
  node("session-status").textContent = "Playback only · no vehicle connected";
  node("replay-empty").hidden = true;
  const skip = document.querySelector(".skip-link");
  skip.href = "#sessions-workspace";
  skip.textContent = "Skip to demo";

  const welcome = node("demo-welcome");
  if (welcome) {
    // The server renders an open overlay to cover the first paint. Upgrade it
    // to a modal dialog so the loaded console is inert and focus stays inside.
    welcome.close();
    welcome.showModal();
    node("demo-enter").addEventListener("click", () => welcome.close());
    welcome.addEventListener("close", () => {
      if (!welcome.open) node("sessions-workspace").focus({ preventScroll: true });
    });
  }

  let loading = false;
  async function start() {
    if (loading) return;
    loading = true;
    node("demo-retry").hidden = true;
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
      document.dispatchEvent(new CustomEvent("argos:demo-ready", { detail: {
        recording_id: config.recording_id, initial_at_s: welcome ? 0.5 : 0,
      } }));
    } catch (error) {
      node("replay-status").textContent = error.name === "AbortError"
        ? "The demo took too long to respond. Please try again."
        : "The demo could not be loaded. Please try again.";
      node("demo-retry").hidden = false;
    } finally {
      clearTimeout(timeout);
      loading = false;
    }
  }
  node("demo-retry").addEventListener("click", () => { void start(); });
  // Archive requests stop on pagehide; returning from the browser cache needs
  // the same fresh initialization as the normal console.
  window.addEventListener("pageshow", event => { if (event.persisted) window.location.reload(); });
  // All archive/report listeners must exist before publishing the configuration.
  document.addEventListener("DOMContentLoaded", () => { void start(); }, { once: true });
})();
