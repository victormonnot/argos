"use strict";

(() => {
  const node = (id) => document.getElementById(id);
  const directions = Array.from(document.querySelectorAll("[data-control-axis]"));
  const actionButtons = Array.from(document.querySelectorAll("[data-control-action]"));
  const throttleButtons = Array.from(document.querySelectorAll("[data-throttle-step]"));
  const framingButtons = Array.from(document.querySelectorAll("[data-framing-operation]"));
  const zero = () => ({ forward: 0, right: 0, up: 0, yaw: 0 });
  const keys = { ArrowUp: ["forward", 1], ArrowDown: ["forward", -1], ArrowLeft: ["right", -1], ArrowRight: ["right", 1],
    r: ["up", 1], f: ["up", -1], q: ["yaw", -1], e: ["yaw", 1] };
  const pointers = new Map(), pressedKeys = new Map();
  // The capability stays in this closure: no storage, URL or DOM attribute.
  let token = null, seq = 0, epoch = 0, control = null, fresh = false;
  let runId = null, environment = null, focused = true;
  let claiming = false, actionPending = false, inputPending = false, inputChanged = false;
  let draftMode = 2, throttle = 0;
  let modeSwitchPending = false, modeSyncPending = false, seededModeGeneration = -1;
  let framingIntent = 0, framingBusy = false, framingOperation = null, framingSuppressed = false;
  let framingFeedback = "", acknowledgedInputSeq = -1;
  let vision = { enabled: false, recent: false };
  let feedback = "", feedbackTone = "neutral";
  let leaseStartedAt = null, leaseInterruption = null, clientInterruption = null, interruptionPending = false;
  const active = () => document.body.dataset.view === "control" && !document.hidden && focused;
  const sameOwnedLease = (value = control) => leaseStartedAt === null || value?.lease_started_at === leaseStartedAt;
  const owned = () => Boolean(token && control?.owned && sameOwnedLease() && fresh && control?.available);
  const selectedMode = () => control?.selected_mode === 0 ? 0 : 2;
  const generation = (value = control) => value?.mode_generation ?? 0;
  const modeChanging = () => modeSwitchPending || modeSyncPending || (owned() && (control?.phase === "switching" || Boolean(control?.mode_transition)));
  const modeName = (mode) => mode === 0 ? "Stabilize" : "AltHold";
  const prepared = () => control?.prepared ?? (control?.command?.action === "prepare" && control.command.observed);
  const flyingMode = () => control?.vehicle?.armed === true ? selectedMode() : draftMode;
  const movingAllowed = () => active() && owned() && !actionPending && !modeChanging() && control.vehicle?.armed === true && control.vehicle?.mode === selectedMode()
    && !["landing", "released", "expired", "error"].includes(control.phase);
  const text = (id, value) => { if (node(id).textContent !== value) node(id).textContent = value; };
  const message = (value, tone = "neutral") => { feedback = value; feedbackTone = tone; };
  const commandPending = () => control?.command && ["sent", "accepted"].includes(control.command.state) && !control.command.observed;

  function framingView() {
    const view = control?.framing;
    const finiteOrNull = value => value === null || (typeof value === "number" && Number.isFinite(value));
    if (!view || typeof view !== "object" || typeof view.enabled !== "boolean" || typeof view.active !== "boolean"
      || (view.paused !== undefined && typeof view.paused !== "boolean")
      || typeof view.available !== "boolean" || !Number.isSafeInteger(view.revision) || view.revision < 0
      || (view.profile !== undefined && !["full", "pilot_throttle"].includes(view.profile))
      || (view.range_response !== undefined && !["gentle", "normal", "responsive"].includes(view.range_response))
      || (view.profiles !== undefined && (!view.profiles || typeof view.profiles !== "object"
        || !["full", "pilot_throttle"].every(profile => typeof view.profiles[profile]?.available === "boolean" && typeof view.profiles[profile]?.reason === "string")))
      || !["disabled", "idle", "selected", "active", "takeover"].includes(view.phase)
      || !(view.target_id === null || (Number.isSafeInteger(view.target_id) && view.target_id > 0))
      || ![view.error_x, view.error_y, view.height, view.reference_height, view.frame_age_s, view.takeover_remaining_s].every(finiteOrNull)) return null;
    return view;
  }

  function manualIntent() {
    const framing = framingView();
    if (!framing?.enabled) return;
    framingSuppressed = true;
    if (framing.active || framing.phase === "takeover" || framingBusy) {
      void requestFraming("stop", {}, { urgent: true });
    } else {
      framingIntent += 1;
    }
  }

  function axes() {
    const value = zero();
    if (!movingAllowed()) return value;
    for (const entry of [...pointers.values(), ...pressedKeys.values()]) value[entry.axis] += entry.value;
    for (const key of Object.keys(value)) value[key] = Math.max(-1, Math.min(1, value[key]));
    if (selectedMode() === 0) value.up = 0;
    return value;
  }

  function resetThrottle() {
    if (throttle === 0) return;
    throttle = 0;
    inputChanged = true;
  }

  function setThrottle(value) {
    if (!movingAllowed() || selectedMode() !== 0) return;
    // Raw Stabilize throttle always belongs to the pilot. Adjusting it keeps
    // selection, pending engagement and paused assistance intact; it is not
    // an acknowledgement of a manual-takeover prompt.
    throttle = Math.round(Math.max(0, Math.min(100, value)) * 10) / 1000;
    inputChanged = true;
    render();
    void sendInput();
  }

  function clearInputs() {
    const held = [...pointers.entries()];
    pointers.clear();
    pressedKeys.clear();
    for (const [id, entry] of held) {
      try { if (entry.button.hasPointerCapture(id)) entry.button.releasePointerCapture(id); } catch { /* A cancelled pointer has no capture. */ }
    }
    inputChanged = true;
    render();
  }

  function adopt(next, { newLease = false } = {}) {
    if (!next || typeof next !== "object" || !Number.isFinite(next.at)) throw new Error("Invalid flight-control state.");
    if (!Number.isSafeInteger(generation(next)) || generation(next) < 0) throw new Error("Invalid flight-mode generation.");
    if (control && next.at < control.at) return false;
    const sameLease = control && next.lease_started_at === control.lease_started_at;
    if (sameLease && !newLease && generation(next) < generation()) return false;
    const modeChanged = sameLease && generation(next) > generation();
    const transfer = next.mode_transfer;
    const completedTransfer = transfer && Number.isSafeInteger(transfer.generation) && transfer.generation === generation(next)
      && [0, 2].includes(transfer.from_mode) && [0, 2].includes(transfer.to_mode) && transfer.from_mode !== transfer.to_mode
      && Number.isFinite(transfer.completed_at) && transfer.completed_at <= next.at;
    if (completedTransfer && transfer.to_mode === 0 && (!Number.isFinite(transfer.throttle) || transfer.throttle < 0 || transfer.throttle > 1)) {
      throw new Error("Invalid transferred Stabilize throttle.");
    }
    control = next;
    if (clientInterruption && next.lease_started_at !== clientInterruption.lease_started_at) {
      if (feedback === clientInterruption.reason) message("");
      clientInterruption = null;
    }
    if (modeChanged) {
      // Mode intent supersedes every pending framing reply, even if it arrives
      // after the transition has completed and normal controls are enabled.
      framingIntent += 1;
      framingBusy = false;
      framingOperation = null;
      framingSuppressed = true;
      acknowledgedInputSeq = -1;
    }
    if (token && next.owned && sameOwnedLease(next) && next.vehicle?.armed === true && next.vehicle?.mode === selectedMode()
      && next.phase !== "switching" && !next.mode_transition && completedTransfer
      && transfer.to_mode === selectedMode() && transfer.generation > seededModeGeneration) {
      // Seed only this confirmed generation. Later snapshots may still contain
      // the record after the pilot has manually adjusted the throttle.
      seededModeGeneration = transfer.generation;
      draftMode = selectedMode();
      throttle = selectedMode() === 0 ? transfer.throttle : 0;
      inputChanged = true;
    }
    rememberInterruption(next);
    if (next.vehicle?.armed !== true || selectedMode() !== 0 || ["landing", "released", "expired", "error"].includes(next.phase)) resetThrottle();
    return true;
  }

  function rememberInterruption(next) {
    const interruption = next?.interruption;
    // A rejected input can arrive before the state explaining its revocation.
    // Keep the exact lease identity after releasing its token, so later state
    // can explain the loss without attributing another pilot's incident.
    if (!interruptionPending || leaseStartedAt === null || leaseInterruption) return;
    // A late GET may still show our revoked lease as owned. Only a different
    // lease identity closes the association with the interruption we await.
    if (next.lease_started_at !== leaseStartedAt) { interruptionPending = false; return; }
    if (next.owned !== false || !interruption || typeof interruption !== "object"
      || interruption.lease_started_at !== leaseStartedAt
      || !Number.isFinite(interruption.at) || interruption.at < leaseStartedAt || interruption.at > next.at
      || typeof interruption.reason !== "string" || !interruption.reason.trim() || interruption.reason.length > 4096) return;
    leaseInterruption = { at: interruption.at, reason: interruption.reason };
    interruptionPending = false;
  }

  function interruptionText() {
    const clientReason = clientInterruption?.lease_started_at === leaseStartedAt ? clientInterruption.reason : "";
    if (!leaseInterruption) return clientReason;
    // Our best-effort release can produce this generic server reason. Keep the
    // client timeout that caused it, but let a specific server loss explain itself.
    const reason = leaseInterruption.reason === "Control released" && clientReason ? clientReason : leaseInterruption.reason;
    const command = control?.command;
    // Keep the cause and the outcome of its LAND independently visible. A
    // later pilot's LAND, or an old successful Arm, does not describe this loss.
    if (control?.interruption?.at !== leaseInterruption.at || control.interruption.lease_started_at !== leaseStartedAt || command?.action !== "land"
      || (command.observed && command.state === "observed")) return reason;
    const outcome = ({ sent: "sent, awaiting confirmation", accepted: "accepted, awaiting confirmation",
      denied: "rejected by the drone", timeout: "confirmation not received", send_failed: "send failed" })[command.state] || "awaiting confirmation";
    return `${reason} · Landing · ${outcome}.`;
  }

  async function post(path, payload, { keepalive = false, timeout = 1800 } = {}) {
    const abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), timeout);
    try {
      const response = await fetch(`/api/control/${path}`, { method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: abort.signal, keepalive });
      const body = await response.json();
      if (!response.ok) {
        const error = new Error(typeof body.detail === "string" ? body.detail : "The command was rejected.");
        error.status = response.status;
        error.code = body.code;
        error.control = body.control;
        throw error;
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") {
        const timeoutError = new Error("The flight-control service did not respond in time.");
        timeoutError.code = "control_response_timeout";
        throw timeoutError;
      }
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  function release(reason, { send = true, clientTimeout = false } = {}) {
    const previous = token;
    if (previous && clientTimeout && leaseStartedAt !== null && sameOwnedLease()) {
      clientInterruption = { lease_started_at: leaseStartedAt, reason };
    }
    token = null;
    epoch += 1;
    const releaseEpoch = epoch, releaseRun = runId;
    claiming = false;
    actionPending = false;
    modeSwitchPending = false;
    modeSyncPending = false;
    seededModeGeneration = -1;
    framingIntent += 1;
    framingBusy = false;
    framingOperation = null;
    framingSuppressed = true;
    framingFeedback = "";
    resetThrottle();
    clearInputs();
    if (reason) message(reason, "warning");
    render();
    if (previous && send) {
      // Keepalive is best effort. The independent service lease handles a lost tab/link.
      void post("action", { token: previous, action: "release" }, { keepalive: true }).then((body) => {
        if (!token && epoch === releaseEpoch && runId === releaseRun) adopt(body.control);
        render();
      }).catch(() => { /* The visible state remains released; never reacquire silently. */ });
    }
  }

  async function sendInput() {
    if (!active() || !owned() || actionPending || modeSyncPending) return;
    if (inputPending) { inputChanged = true; return; }
    // A pending capture can disappear before gotpointercapture; some browsers
    // then emit no lostpointercapture. Do not retain that unowned pointer.
    for (const [id, entry] of pointers) if (!entry.button.hasPointerCapture(id)) pointers.delete(id);
    inputPending = true;
    inputChanged = false;
    const currentToken = token, currentEpoch = epoch, sentGeneration = generation();
    const sentSeq = ++seq;
    try {
      const body = await post("input", { token: currentToken, seq: sentSeq, mode_generation: sentGeneration,
        axes: axes(), throttle: control.vehicle?.armed === true && selectedMode() === 0 && !["landing", "released", "expired", "error"].includes(control.phase) ? throttle : 0 }, { timeout: 500 });
      if (token !== currentToken || epoch !== currentEpoch) return;
      adopt(body.control);
      if (sentGeneration === generation()) acknowledgedInputSeq = Math.max(acknowledgedInputSeq, sentSeq);
      if (!control.owned || !control.available) release("Control expired or unavailable. Take control again explicitly.", { send: false });
    } catch (error) {
      if (token === currentToken && epoch === currentEpoch) {
        if (error.status === 409 && (error.code === "stale_mode_generation" || (sentGeneration < generation() && owned()))) {
          await syncMode(currentToken, currentEpoch, error.code === "stale_mode_generation" ? error.control : null);
        } else release(`${error.message} Control released.`, { clientTimeout: error.code === "control_response_timeout" });
      }
    } finally {
      inputPending = false;
      render();
      // Coalesce changes during one request into the latest axes, never a queue of maneuvers.
      if (inputChanged && active() && owned()) void sendInput();
    }
  }

  async function syncMode(currentToken, currentEpoch, suppliedControl = null) {
    const currentRun = runId, abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), 400);
    modeSyncPending = true;
    clearInputs();
    try {
      let next = suppliedControl;
      if (!next) {
        const response = await fetch("/api/state", { credentials: "same-origin", cache: "no-store", signal: abort.signal });
        if (!response.ok) throw new Error("Mode synchronization failed.");
        const body = await response.json();
        if (body.run_id !== currentRun || body.environment !== "simulation") throw new Error("The flight source changed.");
        next = body.control;
      }
      if (token !== currentToken || epoch !== currentEpoch) return;
      adopt(next);
      if (!control?.owned || !control?.available || !sameOwnedLease()) release("Control expired or unavailable. Take control again explicitly.", { send: false });
      else inputChanged = true;
    } catch (error) {
      if (token === currentToken && epoch === currentEpoch) release("Flight-mode synchronization failed. Control released.");
    } finally {
      window.clearTimeout(timer);
      if (token === currentToken && epoch === currentEpoch) modeSyncPending = false;
      render();
    }
  }

  async function requestFraming(operation, values = {}, { urgent = false } = {}) {
    const modeFenced = ["select", "engage", "closer", "farther", "response"].includes(operation);
    if (!active() || !owned() || (modeFenced && modeChanging()) || !framingView()?.enabled || (framingBusy && !urgent)) return;
    const currentToken = token, currentEpoch = epoch, intent = ++framingIntent, capturedGeneration = generation();
    framingBusy = true;
    framingOperation = operation;
    if (operation === "stop" || operation === "clear") framingSuppressed = true;
    framingFeedback = ({ select: "Selecting person…", engage: "Engaging framing…", stop: "Returning to manual…", clear: "Clearing selection…", closer: "Adjusting closer…", farther: "Adjusting farther…", response: "Updating distance response…" })[operation];
    render();
    const current = () => intent === framingIntent && token === currentToken && epoch === currentEpoch && active() && owned()
      && (!modeFenced || (!modeChanging() && generation() === capturedGeneration));
    try {
      let extra = values;
      if (operation === "engage") {
        // Existing input requests stay serialized and keep the lease alive.
        // Fence engagement with a confirmed neutral pilot input; periodic
        // neutral keepalives may continue while the framing POST is pending.
        while (inputPending && current()) await new Promise(resolve => window.setTimeout(resolve, 10));
        if (!current() || Object.values(axes()).some(Boolean)) return;
        await sendInput();
        if (!current() || Object.values(axes()).some(Boolean) || !vision.enabled || !vision.recent) return;
        extra = { profile: values.profile ?? "full", revision: framingView().revision, input_seq: acknowledgedInputSeq };
      }
      if (!current()) return;
      const body = await post("framing", { token: currentToken, operation, intent, ...extra,
        ...(modeFenced ? { mode_generation: capturedGeneration } : {}) }, { timeout: operation === "stop" ? 500 : 1800 });
      if (!current()) return;
      adopt(body.control);
      framingFeedback = "";
      if (operation === "engage" || operation === "select") framingSuppressed = false;
    } catch (error) {
      if (current()) {
        framingFeedback = error.message;
        // A stop without confirmation cannot be assumed to have reached the
        // service. Relinquish the lease so the independent landing fallback acts.
        if (operation === "stop") release("Manual takeover was not confirmed. Control released; landing requested.");
      }
    } finally {
      if (intent === framingIntent && currentEpoch === epoch) { framingBusy = false; framingOperation = null; }
      render();
    }
  }

  function renderFraming() {
    const framing = framingView(), hasControl = active() && owned() && !modeChanging();
    node("framing-controls").hidden = !framing?.enabled;
    const effectiveActive = hasControl && framing?.active && !framingSuppressed;
    const paused = effectiveActive && framing.paused === true;
    const pilotThrottle = framing?.profile === "pilot_throttle";
    const takeover = framing?.phase === "takeover";
    const modeProfile = selectedMode() === 0 ? "pilot_throttle" : "full";
    const modeReadiness = framing?.profiles?.[modeProfile];
    const inactiveReason = (framing?.profile ?? "full") !== modeProfile && modeReadiness
      ? modeReadiness.reason || (modeReadiness.available ? "Selected. Engage framing with manual throttle." : "Select a person, then engage framing after manual takeoff.")
      : framing?.reason;
    document.querySelector(".control-heading .eyebrow").textContent = takeover ? "MANUAL TAKEOVER" : paused ? "FRAMING PAUSED" : effectiveActive && pilotThrottle ? "FRAMING · MANUAL THROTTLE" : effectiveActive ? "ASSISTED FRAMING" : "MANUAL FLIGHT";
    if (document.body.dataset.view === "control") text("view-context", paused ? "Framing paused · GPS-free" : effectiveActive && pilotThrottle ? "Framing + manual throttle · GPS-free" : effectiveActive ? "Assisted framing · GPS-free" : "Manual flight · GPS-free");
    document.dispatchEvent(new CustomEvent("argos:framing-ui", { detail: {
      allowed: Boolean(hasControl && framing?.enabled && vision.enabled && vision.recent && !framingBusy && !framing.active && !takeover),
      target_id: hasControl ? framing?.target_id ?? null : null, active: Boolean(effectiveActive), paused: Boolean(paused),
    } }));
    if (!framing?.enabled) return;
    text("framing-target", framing.target_id === null ? "No target" : `Person #${framing.target_id}`);
    node("framing-response").value = framing.range_response ?? "normal";
    node("framing-response").disabled = !hasControl || framingBusy || paused || takeover
      || control.phase === "landing" || framing.range_response === undefined;
    const motion = Object.values(axes()).some(Boolean);
    const engage = hasControl && vision.enabled && vision.recent && framing.target_id !== null
      && control.vehicle?.armed === true && control.vehicle?.landed === false
      && !framing.active && !takeover && !framingBusy && !motion && !actionPending;
    for (const button of framingButtons) {
      const operation = button.dataset.framingOperation;
      const profile = button.dataset.framingProfile;
      const profileStatus = framing.profiles?.[profile];
      const profileAvailable = profileStatus?.available ?? (profile === "full" && framing.available);
      const matchingMode = selectedMode() === (profile === "pilot_throttle" ? 0 : 2);
      button.disabled = operation === "engage" ? !engage || !profileAvailable || !matchingMode : operation === "stop" ? !hasControl || !(framing.active || takeover || framingBusy)
        : operation === "clear" ? !hasControl || framing.target_id === null || framing.active || takeover || framingBusy
        : !hasControl || !effectiveActive || paused || framingBusy || !vision.recent;
      if (operation === "engage") {
        button.setAttribute("aria-pressed", String(Boolean(effectiveActive && (framing.profile ?? "full") === profile)));
        button.title = profileStatus?.reason || (matchingMode ? "Select a person and engage after manual takeoff." : `Switch to ${profile === "pilot_throttle" ? "Stabilize" : "AltHold"}, then select and engage.`);
      }
    }
    document.querySelector('[data-framing-operation="stop"]').setAttribute("aria-pressed", String(!effectiveActive && !takeover));
    let status = interruptionText() || (active() && owned() && modeChanging() ? "Framing off during flight-mode transfer. After confirmation, select a person and engage the matching profile."
      : !hasControl ? "Take control to select a person in the image." : !vision.enabled ? "Enable person detection, then select a person in the image."
      : takeover ? `${framing.reason || "Tracking lost"} · Manual takeover required${Number.isFinite(framing.takeover_remaining_s) ? ` within ${Math.max(0, framing.takeover_remaining_s).toFixed(1)} s` : ""}.`
      : paused ? `Framing paused · ${framing.reason || "Detection interrupted; corrections paused"}${pilotThrottle ? " · Keep managing throttle." : ""}`
      : framingFeedback || (effectiveActive ? pilotThrottle ? "Framing active · You manage throttle. Tilt, yaw or Manual ends assistance." : "Framing active · Any manual direction returns control to you." : inactiveReason || "Select a person, then engage framing after manual takeoff."));
    if (framingFeedback && !leaseInterruption && !takeover && !paused && hasControl) status = framingFeedback;
    text("framing-status", status);
    text("framing-profile-note", effectiveActive && pilotThrottle
      ? "ARGOS controls yaw and relative size, with level roll. You control throttle; vertical centering is manual."
      : effectiveActive ? "Full framing controls image centering and relative size in AltHold."
      : selectedMode() === 0
        ? "Stabilize: engage framing with manual throttle. For full framing, switch to AltHold, then select and engage again."
        : "AltHold: engage full framing. For manual throttle, switch to Stabilize, then select and engage again.");
    node("framing-status").dataset.phase = takeover ? "lost" : effectiveActive ? "active" : framing.phase;
    const recent = hasControl && vision.recent && !paused && !takeover && Number.isFinite(framing.frame_age_s) && framing.frame_age_s <= 1;
    const error = value => recent && Number.isFinite(value) && Math.abs(value) <= 1 ? `${value >= 0 ? "+" : ""}${value.toFixed(2)}` : "—";
    const height = value => Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1)}%` : "—";
    text("framing-error", `${error(framing.error_x)} / ${error(framing.error_y)}`);
    text("framing-height", `${recent ? height(framing.height) : "—"} / ${height(framing.reference_height)}`);
  }

  function render() {
    const hasControl = owned(), vehicle = fresh ? control?.vehicle : null;
    const supported = fresh && control?.available && environment === "simulation";
    text("control-environment", environment === "real" ? "Physical flight control unavailable" : "Simulation only");
    text("control-authority", hasControl ? "You have control" : !fresh ? "Status not refreshed" : control?.owned ? "Another pilot connected" : supported ? "Control available" : "Flight controls unavailable");
    node("control-claim").disabled = !active() || !supported || Boolean(control?.owned) || Boolean(token) || claiming || vehicle?.armed !== false;
    node("control-claim").hidden = hasControl;
    text("control-claim", claiming ? "Connecting…" : "Take control");
    text("control-mode", `Mode ${vehicle?.mode === 2 ? "AltHold" : vehicle?.mode === 9 ? "Land" : vehicle?.mode === 0 ? "Stabilize" : vehicle?.mode == null ? "—" : vehicle.mode}`);
    text("control-armed", vehicle?.armed === true ? "Armed" : vehicle?.armed === false ? "Disarmed" : "Arming —");
    const mode = flyingMode(), isStabilize = mode === 0;
    node("control-panel").dataset.mode = String(mode);
    node("control-mode-select").value = String(hasControl ? draftMode : selectedMode());
    const airborne = vehicle?.armed === true && vehicle?.landed === false && vehicle?.mode === selectedMode();
    const modeChoiceAllowed = active() && hasControl && !actionPending && !commandPending() && !modeChanging()
      && ((vehicle?.armed === false && vehicle?.landed === true) || airborne);
    node("control-mode-select").disabled = !modeChoiceAllowed;
    node("control-mode-switch").hidden = vehicle?.armed !== true;
    const switchReady = control?.mode_switch?.available === true && control.mode_switch.target_mode === draftMode;
    node("control-mode-switch").disabled = !modeChoiceAllowed || !airborne || draftMode === selectedMode() || !switchReady;
    text("control-mode-switch", modeChanging() ? "Switching…" : "Switch mode");
    node("control-height-pad").hidden = isStabilize;
    node("control-throttle-group").hidden = !isStabilize;
    text("control-height-title", isStabilize ? "Throttle and yaw" : "Height and yaw");
    text("control-throttle-value", `${Number((throttle * 100).toFixed(1))} %`);
    node("control-throttle").value = String(throttle * 100);
    node("control-throttle").disabled = !isStabilize || !movingAllowed();
    for (const button of throttleButtons) button.disabled = !isStabilize || !movingAllowed() || (Number(button.dataset.throttleStep) < 0 ? throttle <= 0 : throttle >= 1);
    text("control-mode-note", vehicle?.armed ? draftMode === 0 && selectedMode() !== 0
      ? "Switch to Stabilize: throttle starts from the autopilot’s recent output. Adjust it manually after switching."
      : "Choose a flight mode, then use Switch mode. The selector alone does not change flight."
      : isStabilize ? "Stabilize: self-leveling, manual throttle. Prepare the mode on the ground." : "AltHold: self-leveling, assisted altitude. Prepare the mode on the ground.");
    text("control-neutral-note", isStabilize ? "Releasing a direction neutralizes tilt. Throttle stays at the chosen value: adjust it to maintain height. The drone may drift without GPS." : "Release to neutralize inputs: the drone may keep drifting without GPS. To take off, hold Climb after arming.");
    const motion = axes();
    for (const button of directions) {
      button.disabled = !movingAllowed() || (isStabilize && button.dataset.controlAxis === "up");
      button.setAttribute("aria-pressed", String(!button.disabled && motion[button.dataset.controlAxis] * Number(button.dataset.controlValue) > 0));
    }
    for (const button of actionButtons) {
      const action = button.dataset.controlAction;
      const pending = actionPending || commandPending() || modeChanging();
      button.disabled = !active() || !hasControl || (action !== "release" && action !== "land" && pending)
        || (action === "prepare" && (vehicle?.armed !== false || vehicle?.landed !== true))
        || (action === "arm" && (vehicle?.armed !== false || !prepared() || vehicle?.mode !== selectedMode() || draftMode !== selectedMode() || !control?.profile?.ready))
        || (action === "land" && (vehicle?.armed !== true || control?.phase === "landing"))
        || (action === "disarm" && (vehicle?.armed !== true || vehicle?.landed !== true));
    }
    const command = control?.command;
    const commandText = command ? `${({ prepare: `Preparation ${modeName(command.mode ?? selectedMode())}`, switch_mode: `Switch to ${modeName(command.mode ?? selectedMode())}`, arm: "Arming", land: "Landing", disarm: "Disarming", release: "Release" })[command.action] || "Command"} · ${command.observed && command.state === "observed" ? "confirmed by the drone" : ({ sent: "sent, awaiting confirmation", accepted: "accepted, awaiting confirmation", denied: "rejected by the drone", timeout: "confirmation not received", send_failed: "send failed" })[command.state] || "awaiting confirmation"}.` : "";
    const unavailable = !fresh ? "Service connection interrupted. Take control again after recovery." : !control ? "Flight controls are not enabled on this service." : !control.available ? control.reason || "Simulation unavailable." : "";
    const profileIssue = hasControl && !control?.profile?.ready ? control?.profile?.mismatched?.length ? "Incompatible simulation settings: check the GPS-free startup profile." : "Checking GPS-free configuration…" : "";
    const draftIssue = hasControl && vehicle?.armed === false && (!prepared() || draftMode !== selectedMode()) ? `Prepare ${modeName(draftMode)}, then arm for manual takeoff.` : "";
    const hint = hasControl ? vehicle?.armed ? isStabilize ? "Adjust throttle; hold directions to fly." : "Hold the buttons to fly." : control?.profile?.ready ? isStabilize ? "Arm at 0% throttle, then increase gradually to take off." : "Arm, then hold Climb to take off." : "Waiting for GPS-free configuration confirmation." : vehicle?.armed !== false ? "Taking control requires a disarmed drone." : "Take control to prepare for flight.";
    const switching = modeChanging() ? `Switching to ${modeName(control?.mode_transition?.to_mode ?? draftMode)} · Directions paused; waiting for confirmation from the drone.` : "";
    const switchIssue = hasControl && airborne && draftMode !== selectedMode() && !switchReady ? control?.mode_switch?.reason || "Flight-mode transfer is not currently available." : "";
    text("control-feedback", interruptionText() || unavailable || switching || feedback || switchIssue || (["denied", "timeout", "send_failed"].includes(command?.state) ? commandText : profileIssue) || draftIssue || commandText || control?.last_error || hint);
    node("control-feedback").dataset.tone = leaseInterruption || unavailable || profileIssue ? "warning" : feedback ? feedbackTone : ["denied", "timeout", "send_failed"].includes(command?.state) ? "error" : "neutral";
    renderFraming();
  }

  node("control-claim").addEventListener("click", async () => {
    if (node("control-claim").disabled) return;
    claiming = true;
    message("");
    resetThrottle();
    clearInputs();
    const currentEpoch = ++epoch;
    let mintedToken = null;
    render();
    try {
      const body = await post("claim", {});
      if (typeof body.token !== "string" || !body.token.length) throw new Error("Invalid take-control response.");
      mintedToken = body.token;
      if (epoch !== currentEpoch || !active()) {
        void post("action", { token: body.token, action: "release" }, { keepalive: true }).catch(() => {});
        return;
      }
      adopt(body.control, { newLease: true });
      leaseStartedAt = Number.isFinite(body.control.lease_started_at) ? body.control.lease_started_at : null;
      leaseInterruption = null;
      clientInterruption = null;
      interruptionPending = leaseStartedAt !== null;
      token = body.token;
      rememberInterruption(control);
      framingIntent = 0;
      framingSuppressed = false;
      acknowledgedInputSeq = -1;
      seededModeGeneration = -1;
      draftMode = selectedMode();
      void sendInput();
    } catch (error) {
      if (mintedToken && token !== mintedToken) void post("action", { token: mintedToken, action: "release" }, { keepalive: true }).catch(() => {});
      if (epoch === currentEpoch) message(error.message, "error");
    }
    finally { if (epoch === currentEpoch) claiming = false; render(); }
  });

  node("control-mode-switch").addEventListener("click", async () => {
    if (node("control-mode-switch").disabled) return;
    const currentToken = token, currentEpoch = epoch, startGeneration = generation(), targetMode = draftMode;
    const current = () => token === currentToken && epoch === currentEpoch && active() && owned();
    modeSwitchPending = true;
    const switchIntent = ++framingIntent;
    framingBusy = false;
    framingOperation = null;
    framingSuppressed = true;
    framingFeedback = "";
    clearInputs();
    message("");
    render();
    try {
      while (inputPending && current()) await new Promise(resolve => window.setTimeout(resolve, 10));
      if (!current() || generation() !== startGeneration) return;
      // Clear the prior maneuver and confirm a neutral input before requesting
      // the transfer. Ordinary keepalives continue while its action is pending.
      await sendInput();
      if (!current() || generation() !== startGeneration || acknowledgedInputSeq < 0) return;
      const body = await post("action", { token: currentToken, action: "switch_mode", mode: targetMode,
        mode_generation: startGeneration, input_seq: acknowledgedInputSeq });
      if (current()) adopt(body.control);
    } catch (error) {
      if (current()) {
        message(error.message, "warning");
        // Never retry a mode-changing action after an uncertain reply. Resync
        // its state and let the backend's fixed deadline determine the outcome.
        await syncMode(currentToken, currentEpoch, error.code === "stale_mode_generation" ? error.control : null);
        // A refused transfer can leave the existing assistance running. A
        // confirmed unchanged generation describes that actual state; a newer
        // Manual or other action still wins and must not be visually undone.
        if (current() && generation() === startGeneration && framingIntent === switchIntent
          && framingView()?.active && !control.mode_transition && control.phase !== "switching") framingSuppressed = false;
      }
    } finally {
      if (token === currentToken && epoch === currentEpoch) {
        modeSwitchPending = false;
        inputChanged = true;
        void sendInput();
      }
      render();
    }
  });

  for (const button of actionButtons) button.addEventListener("click", async () => {
    if (button.disabled) return;
    const action = button.dataset.controlAction;
    if (action === "release") { release("Control released. Landing requested if the drone is armed."); return; }
    actionPending = true;
    framingIntent += 1;
    framingBusy = false;
    framingSuppressed = true;
    clearInputs();
    if (action === "prepare" || action === "arm") resetThrottle();
    message("");
    const currentToken = token, currentEpoch = epoch;
    render();
    try {
      const body = await post("action", { token: currentToken, action, ...(action === "prepare" ? { mode: draftMode } : {}) });
      if (currentEpoch === epoch && token === currentToken) adopt(body.control);
    } catch (error) { if (currentEpoch === epoch) message(error.message, "error"); }
    finally { if (currentEpoch === epoch) { actionPending = false; void sendInput(); } render(); }
  });

  node("control-mode-select").addEventListener("change", () => {
    if (node("control-mode-select").disabled) { render(); return; }
    draftMode = Number(node("control-mode-select").value) === 0 ? 0 : 2;
    if (control?.vehicle?.armed !== true) resetThrottle();
    clearInputs();
    message("");
    render();
  });
  node("control-throttle").addEventListener("input", (event) => setThrottle(Number(event.target.value)));
  for (const button of throttleButtons) button.addEventListener("click", () => {
    if (!button.disabled) setThrottle(throttle * 100 + Number(button.dataset.throttleStep));
  });

  function pointerEnd(event) {
    const entry = pointers.get(event.pointerId);
    if (!entry) return;
    pointers.delete(event.pointerId);
    try { if (entry.button.hasPointerCapture(event.pointerId)) entry.button.releasePointerCapture(event.pointerId); } catch { /* Already cancelled. */ }
    render();
    void sendInput();
  }
  for (const button of directions) {
    button.addEventListener("pointerdown", (event) => {
      if (!movingAllowed() || button.disabled || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault();
      try { button.setPointerCapture(event.pointerId); } catch { return; }
      manualIntent();
      pointers.set(event.pointerId, { axis: button.dataset.controlAxis, value: Number(button.dataset.controlValue), button });
      render();
      void sendInput();
    });
    button.addEventListener("lostpointercapture", pointerEnd);
    button.addEventListener("contextmenu", (event) => event.preventDefault());
    button.addEventListener("dragstart", (event) => event.preventDefault());
    // A synthesized click must never latch a flight input.
    button.addEventListener("click", (event) => event.preventDefault());
  }
  window.addEventListener("pointerup", pointerEnd);
  window.addEventListener("pointercancel", pointerEnd);
  document.addEventListener("keydown", (event) => {
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (!movingAllowed() || event.ctrlKey || event.metaKey || event.altKey || event.isComposing || event.target.closest("input,textarea,select,[contenteditable]")) return;
    if (!keys[key]) return;
    event.preventDefault();
    if (event.repeat || pressedKeys.has(key)) return;
    const [axis, value] = keys[key];
    if (axis === "up" && selectedMode() === 0) { setThrottle(throttle * 100 + value * 2); return; }
    manualIntent();
    pressedKeys.set(key, { axis, value });
    render();
    void sendInput();
  });
  document.addEventListener("keyup", (event) => {
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (!pressedKeys.delete(key)) return;
    event.preventDefault();
    render();
    void sendInput();
  });
  // Editing or opening another context cannot retain a previously held key.
  document.addEventListener("focusin", (event) => {
    if (event.target.id !== "control-throttle" && event.target.closest("input,textarea,select,[contenteditable]") && (pressedKeys.size || pointers.size)) { clearInputs(); void sendInput(); }
  });
  window.addEventListener("blur", () => { focused = false; release("Window left: control released. Take control again explicitly."); });
  window.addEventListener("focus", () => { focused = true; render(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) release("Tab hidden: control released. Take control again explicitly."); render(); });
  window.addEventListener("pagehide", () => release("Page left: control released."));
  document.addEventListener("argos:workspace-changed", (event) => {
    if (event.detail?.view !== "control") release("Flight controls left: control released.");
    render();
  });
  for (const button of framingButtons) button.addEventListener("click", () => {
    if (!button.disabled) void requestFraming(button.dataset.framingOperation,
      button.dataset.framingProfile ? { profile: button.dataset.framingProfile } : {}, { urgent: button.dataset.framingOperation === "stop" });
  });
  node("framing-response").addEventListener("change", event => {
    if (!event.target.disabled) void requestFraming("response", { range_response: event.target.value });
  });
  document.addEventListener("argos:select-person", event => {
    const framing = framingView();
    if (!active() || !owned() || modeChanging() || !framing?.enabled || framing.active || framing.phase === "takeover" || framingBusy || !vision.enabled || !vision.recent) return;
    const value = event.detail;
    if (!value || value.run_id !== runId || value.video_id !== vision.video_id || !Number.isSafeInteger(value.frame_sequence)
      || value.frame_sequence < 0 || !Number.isSafeInteger(value.track_id) || value.track_id < 1) return;
    void requestFraming("select", { run_id: value.run_id, video_id: value.video_id, frame_sequence: value.frame_sequence, track_id: value.track_id });
  });
  document.addEventListener("argos:vision-state", event => {
    const wasEnabled = vision.enabled;
    vision = event.detail || { enabled: false, recent: false };
    if (wasEnabled && !vision.enabled && owned() && framingView()?.enabled) void requestFraming("stop", {}, { urgent: true });
    render();
  });
  document.addEventListener("argos:control-state", (event) => {
    const detail = event.detail || {};
    if (runId !== null && runId !== detail.run_id) {
      release("The source changed. Take control again explicitly.");
      control = null;
      leaseStartedAt = null;
      leaseInterruption = null;
      clientInterruption = null;
      interruptionPending = false;
      draftMode = 2;
    }
    runId = detail.run_id;
    fresh = detail.fresh === true;
    environment = detail.environment;
    try { if (detail.control) adopt(detail.control); else control = null; } catch { fresh = false; }
    if ((token && (!fresh || !control?.available || !control?.owned || !sameOwnedLease() || environment !== "simulation"))
      || (claiming && (!fresh || !control?.available || environment !== "simulation"))) release("Flight control was interrupted. Take control again explicitly.");
    if (!movingAllowed() && (pointers.size || pressedKeys.size)) { clearInputs(); void sendInput(); }
    render();
  });
  window.setInterval(() => { if (active() && owned()) void sendInput(); }, 100);
  render();
})();
