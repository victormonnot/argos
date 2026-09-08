"use strict";

(() => {
  const node = (id) => document.getElementById(id);
  const directions = Array.from(document.querySelectorAll("[data-control-axis]"));
  const actionButtons = Array.from(document.querySelectorAll("[data-control-action]"));
  const throttleButtons = Array.from(document.querySelectorAll("[data-throttle-step]"));
  const zero = () => ({ forward: 0, right: 0, up: 0, yaw: 0 });
  const keys = { ArrowUp: ["forward", 1], ArrowDown: ["forward", -1], ArrowLeft: ["right", -1], ArrowRight: ["right", 1],
    r: ["up", 1], f: ["up", -1], q: ["yaw", -1], e: ["yaw", 1] };
  const pointers = new Map(), pressedKeys = new Map();
  // The capability stays in this closure: no storage, URL or DOM attribute.
  let token = null, seq = 0, epoch = 0, control = null, fresh = false;
  let runId = null, environment = null, focused = true;
  let claiming = false, actionPending = false, inputPending = false, inputChanged = false;
  let draftMode = 2, throttle = 0;
  let feedback = "", feedbackTone = "neutral";
  const active = () => document.body.dataset.view === "control" && !document.hidden && focused;
  const owned = () => Boolean(token && control?.owned && fresh && control?.available);
  const selectedMode = () => control?.selected_mode === 0 ? 0 : 2;
  const modeName = (mode) => mode === 0 ? "Stabilize" : "AltHold";
  const prepared = () => control?.prepared ?? (control?.command?.action === "prepare" && control.command.observed);
  const flyingMode = () => control?.vehicle?.armed === true ? selectedMode() : draftMode;
  const movingAllowed = () => active() && owned() && !actionPending && control.vehicle?.armed === true && control.vehicle?.mode === selectedMode()
    && !["landing", "released", "expired", "error"].includes(control.phase);
  const text = (id, value) => { if (node(id).textContent !== value) node(id).textContent = value; };
  const message = (value, tone = "neutral") => { feedback = value; feedbackTone = tone; };
  const commandPending = () => control?.command && ["sent", "accepted"].includes(control.command.state) && !control.command.observed;

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
    throttle = Math.round(Math.max(0, Math.min(100, value))) / 100;
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

  function adopt(next) {
    if (!next || typeof next !== "object" || !Number.isFinite(next.at)) throw new Error("État du pilotage invalide.");
    if (control && next.at < control.at) return false;
    control = next;
    if (next.vehicle?.armed !== true || selectedMode() !== 0 || ["landing", "released", "expired", "error"].includes(next.phase)) resetThrottle();
    return true;
  }

  async function post(path, payload, { keepalive = false, timeout = 1800 } = {}) {
    const abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), timeout);
    try {
      const response = await fetch(`/api/control/${path}`, { method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: abort.signal, keepalive });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "La commande a été refusée.");
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Le service de pilotage ne répond pas à temps.");
      throw error;
    } finally { window.clearTimeout(timer); }
  }

  function release(reason, { send = true } = {}) {
    const previous = token;
    token = null;
    epoch += 1;
    const releaseEpoch = epoch, releaseRun = runId;
    claiming = false;
    actionPending = false;
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
    if (!active() || !owned() || actionPending) return;
    if (inputPending) { inputChanged = true; return; }
    // A pending capture can disappear before gotpointercapture; some browsers
    // then emit no lostpointercapture. Do not retain that unowned pointer.
    for (const [id, entry] of pointers) if (!entry.button.hasPointerCapture(id)) pointers.delete(id);
    inputPending = true;
    inputChanged = false;
    const currentToken = token, currentEpoch = epoch;
    try {
      const body = await post("input", { token: currentToken, seq: ++seq, axes: axes(), throttle: movingAllowed() && selectedMode() === 0 ? throttle : 0 }, { timeout: 500 });
      if (token !== currentToken || epoch !== currentEpoch) return;
      adopt(body.control);
      if (!control.owned || !control.available) release("Commandes expirées ou indisponibles. Reprenez-les explicitement.", { send: false });
    } catch (error) {
      if (token === currentToken && epoch === currentEpoch) release(`${error.message} Commandes libérées.`);
    } finally {
      inputPending = false;
      render();
      // Coalesce changes during one request into the latest axes, never a queue of maneuvers.
      if (inputChanged && active() && owned()) void sendInput();
    }
  }

  function render() {
    const hasControl = owned(), vehicle = fresh ? control?.vehicle : null;
    const supported = fresh && control?.available && environment === "simulation";
    text("control-environment", environment === "real" ? "Pilotage réel indisponible" : "Simulation uniquement");
    text("control-authority", hasControl ? "Vous avez les commandes" : !fresh ? "État non actualisé" : control?.owned ? "Autre pilote connecté" : supported ? "Commandes libres" : "Pilotage indisponible");
    node("control-claim").disabled = !active() || !supported || Boolean(control?.owned) || Boolean(token) || claiming || vehicle?.armed !== false;
    node("control-claim").hidden = hasControl;
    text("control-claim", claiming ? "Connexion…" : "Prendre les commandes");
    text("control-mode", `Mode ${vehicle?.mode === 2 ? "AltHold" : vehicle?.mode === 9 ? "Land" : vehicle?.mode === 0 ? "Stabilize" : vehicle?.mode == null ? "—" : vehicle.mode}`);
    text("control-armed", vehicle?.armed === true ? "Armé" : vehicle?.armed === false ? "Désarmé" : "Armement —");
    const mode = flyingMode(), isStabilize = mode === 0;
    node("control-panel").dataset.mode = String(mode);
    node("control-mode-select").value = String(vehicle?.armed ? selectedMode() : draftMode);
    node("control-mode-select").disabled = !active() || !hasControl || vehicle?.armed !== false || vehicle?.landed !== true || actionPending || commandPending();
    node("control-height-pad").hidden = isStabilize;
    node("control-throttle-group").hidden = !isStabilize;
    text("control-height-title", isStabilize ? "Gaz et rotation" : "Hauteur et rotation");
    text("control-throttle-value", `${Math.round(throttle * 100)} %`);
    node("control-throttle").value = String(Math.round(throttle * 100));
    node("control-throttle").disabled = !isStabilize || !movingAllowed();
    for (const button of throttleButtons) button.disabled = !isStabilize || !movingAllowed() || (Number(button.dataset.throttleStep) < 0 ? throttle <= 0 : throttle >= 1);
    text("control-mode-note", isStabilize ? "Stabilize : retour à plat, gaz manuels. Le mode se prépare au sol." : "AltHold : retour à plat, altitude assistée. Le mode se prépare au sol.");
    text("control-neutral-note", isStabilize ? "Relâcher une direction remet l’inclinaison au neutre. Les gaz restent à la valeur choisie : ajustez-les pour tenir l’altitude. Le drone peut dériver, sans GPS." : "Relâcher remet les commandes au neutre : le drone peut continuer à dériver, sans GPS. Pour décoller, maintenez Monter après l’armement.");
    const motion = axes();
    for (const button of directions) {
      button.disabled = !movingAllowed() || (isStabilize && button.dataset.controlAxis === "up");
      button.setAttribute("aria-pressed", String(!button.disabled && motion[button.dataset.controlAxis] * Number(button.dataset.controlValue) > 0));
    }
    for (const button of actionButtons) {
      const action = button.dataset.controlAction;
      const pending = actionPending || commandPending();
      button.disabled = !active() || !hasControl || (action !== "release" && action !== "land" && pending)
        || (action === "prepare" && (vehicle?.armed !== false || vehicle?.landed !== true))
        || (action === "arm" && (vehicle?.armed !== false || !prepared() || vehicle?.mode !== selectedMode() || draftMode !== selectedMode() || !control?.profile?.ready))
        || (action === "land" && (vehicle?.armed !== true || control?.phase === "landing"))
        || (action === "disarm" && (vehicle?.armed !== true || vehicle?.landed !== true));
    }
    const command = control?.command;
    const commandText = command ? `${({ prepare: `Préparation ${modeName(command.mode ?? selectedMode())}`, arm: "Armement", land: "Atterrissage", disarm: "Désarmement", release: "Libération" })[command.action] || "Commande"} · ${command.observed && command.state === "observed" ? "confirmé par le drone" : ({ sent: "envoyé, confirmation en attente", accepted: "accepté, confirmation en attente", denied: "refusé par le drone", timeout: "confirmation non reçue", send_failed: "échec de l’envoi" })[command.state] || "confirmation en attente"}.` : "";
    const unavailable = !fresh ? "Connexion au service interrompue. Reprenez les commandes après rétablissement." : !control ? "Le pilotage n’est pas activé sur ce service." : !control.available ? control.reason || "Simulation indisponible." : "";
    const profileIssue = hasControl && !control?.profile?.ready ? control?.profile?.mismatched?.length ? "Configuration de simulation incompatible : vérifiez le profil sans GPS du lancement." : "Vérification de la configuration sans GPS en cours…" : "";
    const draftIssue = hasControl && vehicle?.armed === false && (!prepared() || draftMode !== selectedMode()) ? `Préparez ${modeName(draftMode)}, puis armez pour décoller manuellement.` : "";
    const hint = hasControl ? vehicle?.armed ? isStabilize ? "Réglez les gaz ; maintenez les directions pour piloter." : "Maintenez les boutons pour piloter." : control?.profile?.ready ? isStabilize ? "Armez à 0 % de gaz, puis augmentez progressivement pour décoller." : "Armez, puis maintenez Monter pour décoller." : "La configuration sans GPS attend sa confirmation." : vehicle?.armed !== false ? "La prise de commandes nécessite un drone désarmé." : "Prenez les commandes pour préparer le vol.";
    text("control-feedback", unavailable || feedback || (["denied", "timeout", "send_failed"].includes(command?.state) ? commandText : profileIssue) || draftIssue || commandText || control?.last_error || hint);
    node("control-feedback").dataset.tone = unavailable || profileIssue ? "warning" : feedback ? feedbackTone : ["denied", "timeout", "send_failed"].includes(command?.state) ? "error" : "neutral";
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
      if (typeof body.token !== "string" || !body.token.length) throw new Error("Réponse de prise de commandes invalide.");
      mintedToken = body.token;
      if (epoch !== currentEpoch || !active()) {
        void post("action", { token: body.token, action: "release" }, { keepalive: true }).catch(() => {});
        return;
      }
      adopt(body.control);
      token = body.token;
      draftMode = selectedMode();
      void sendInput();
    } catch (error) {
      if (mintedToken && token !== mintedToken) void post("action", { token: mintedToken, action: "release" }, { keepalive: true }).catch(() => {});
      if (epoch === currentEpoch) message(error.message, "error");
    }
    finally { if (epoch === currentEpoch) claiming = false; render(); }
  });

  for (const button of actionButtons) button.addEventListener("click", async () => {
    if (button.disabled) return;
    const action = button.dataset.controlAction;
    if (action === "release") { release("Commandes libérées. Atterrissage demandé si le drone est armé."); return; }
    actionPending = true;
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
    resetThrottle();
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
  window.addEventListener("blur", () => { focused = false; release("Fenêtre quittée : commandes libérées. Reprenez-les explicitement."); });
  window.addEventListener("focus", () => { focused = true; render(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) release("Onglet masqué : commandes libérées. Reprenez-les explicitement."); render(); });
  window.addEventListener("pagehide", () => release("Page quittée : commandes libérées."));
  document.addEventListener("argos:workspace-changed", (event) => {
    if (event.detail?.view !== "control") release("Pilotage quitté : commandes libérées.");
    render();
  });
  document.addEventListener("argos:control-state", (event) => {
    const detail = event.detail || {};
    if (runId !== null && runId !== detail.run_id) {
      release("La source a changé. Reprenez les commandes explicitement.");
      control = null;
      draftMode = 2;
    }
    runId = detail.run_id;
    fresh = detail.fresh === true;
    environment = detail.environment;
    try { if (detail.control) adopt(detail.control); else control = null; } catch { fresh = false; }
    if ((token && (!fresh || !control?.available || !control?.owned || environment !== "simulation"))
      || (claiming && (!fresh || !control?.available || environment !== "simulation"))) release("Le pilotage a été interrompu. Reprenez les commandes explicitement.");
    if (!movingAllowed() && (pointers.size || pressedKeys.size)) { clearInputs(); void sendInput(); }
    render();
  });
  window.setInterval(() => { if (active() && owned()) void sendInput(); }, 100);
  render();
})();
