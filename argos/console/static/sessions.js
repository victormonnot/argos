/* Read-only historical workspace. No live telemetry is fed into this view. */
(() => {
  "use strict";
  const node = (id) => document.getElementById(id);
  const text = (id, value) => { node(id).textContent = value; };
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 2 });
  const numeric = (value, unit = "") => finite(value) ? `${number.format(value)}${unit}` : "—";
  const date = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
  const identifier = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const hash = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  const kinds = ["heartbeat", "battery", "attitude", "local_position_ned"];
  let visible = false;
  let selected = null;
  let metadata = null;
  let snapshot = null;
  let source = null;
  let cursor = 0;
  let playing = false;
  let anchorAt = 0;
  let anchorTime = 0;
  let timer = null;
  let seekTimer = null;
  let epoch = 0;
  let catalogEpoch = 0;
  let request = null;
  let catalogRequest = null;
  let detailView = "measures";
  let messagesOffset = 0;
  let messagesPage = null;

  async function getJSON(url, controller) {
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Lecture refusée par le service.");
      return value;
    } finally {
      clearTimeout(timeout);
    }
  }

  function pause() {
    playing = false;
    clearTimeout(timer);
    text("replay-play", "Lire");
  }

  function clampTime(offset) {
    const end = metadata.duration_s;
    // Native range inputs serialize fewer digits than a JS float. Preserve the
    // exact recording endpoint when the handle reaches its maximum.
    if (Math.abs(end - offset) <= Number.EPSILON * 32 * Math.max(1, end)) return end;
    return Math.max(0, Math.min(end, offset));
  }

  function cancelReplay() {
    pause();
    clearTimeout(seekTimer);
    epoch += 1;
    request?.abort();
    request = null;
  }

  function setWorkspace(show) {
    if (visible === show) return;
    visible = show;
    if (show) {
      void refreshCatalog();
      // A request canceled on leaving cannot leave old values under a new cursor.
      if (metadata && detailView === "messages") void loadMessages(messagesOffset);
      else if (metadata && source && detailView === "measures") void seek(cursor);
      else if (selected && !metadata) void openRecording(selected, false);
    } else {
      cancelReplay();
      catalogEpoch += 1;
      catalogRequest?.abort();
      catalogRequest = null;
      node("archive-refresh").disabled = false;
    }
    notifyAnalysis();
  }

  function controls(busy = false) {
    const ready = Boolean(metadata && source);
    node("replay-cursor").disabled = !ready || metadata.duration_s === 0;
    node("replay-play").disabled = !ready || (!snapshot && !playing) || metadata.duration_s === 0;
    node("replay-previous").disabled = !ready || busy || snapshot?.previous_at_s == null;
    node("replay-next").disabled = !ready || busy || snapshot?.next_at_s == null;
    node("replay-measures").setAttribute("aria-busy", String(busy));
  }

  function clearMeasures(message) {
    snapshot = null;
    node("replay-measures").hidden = !metadata || !source;
    for (const element of node("replay-measures").querySelectorAll("dd,.history-value")) element.textContent = "—";
    for (const kind of kinds) {
      node(`history-${kind}`).dataset.state = "absent";
      text(`history-${kind}-age`, "En attente de la relecture");
    }
    text("history-rejection", "");
    text("replay-cursor-status", message);
    controls(true);
  }

  function markSelection() {
    for (const button of node("archive-list").querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.id === selected));
    }
  }

  async function refreshCatalog() {
    const token = ++catalogEpoch;
    catalogRequest?.abort();
    const controller = catalogRequest = new AbortController();
    node("archive-refresh").disabled = true;
    text("archive-status", "Lecture du dossier…");
    try {
      const catalog = await getJSON("/api/recordings", controller);
      if (token !== catalogEpoch || !visible) return;
      if (!Array.isArray(catalog.items) || !Number.isSafeInteger(catalog.total) || !catalog.items.every((item) => identifier(item.id) && finite(item.modified_at) && finite(item.size_bytes) && ["unverified", "recording", "error", "too_large"].includes(item.state))) throw new Error("Liste de journaux invalide.");
      text("archive-directory", typeof catalog.directory === "string" ? catalog.directory : "Emplacement non communiqué par le service.");
      const fragment = document.createDocumentFragment();
      for (const item of catalog.items) {
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.id = item.id;
        button.dataset.state = item.state;
        button.disabled = item.state !== "unverified";
        button.title = item.id;
        const state = { unverified: "Ouvrir et vérifier", recording: "Enregistrement en cours", error: "Capture en erreur", too_large: "Hors limite de relecture" }[item.state];
        for (const [name, value] of [["id", `Journal ${item.id.slice(0, 8)}`], ["date", `Fichier modifié le ${date.format(new Date(item.modified_at * 1000))}`], ["state", `${numeric(item.size_bytes / 1024, " Kio")} · ${state}`]]) {
          const span = document.createElement("span");
          span.className = `archive-item-${name}`;
          span.textContent = value;
          button.append(span);
        }
        button.addEventListener("click", () => { void openRecording(item.id, true); });
        row.append(button);
        fragment.append(row);
      }
      node("archive-list").replaceChildren(fragment);
      markSelection();
      text("archive-count", `${number.format(catalog.total)} ${catalog.total === 1 ? "journal" : "journaux"}`);
      text("archive-status", catalog.total === 0 ? "Aucun journal. Démarrez puis arrêtez une capture depuis Observation → Journal MAVLink." : catalog.total > catalog.limit ? `Les ${catalog.limit} fichiers les plus récemment modifiés sont affichés.` : "");
    } catch (error) {
      if (token === catalogEpoch && visible) text("archive-status", error.name === "AbortError" ? "Le dossier n’a pas répondu à temps. Réessayez avec Actualiser." : error.message);
    } finally {
      if (token === catalogEpoch) {
        node("archive-refresh").disabled = false;
        catalogRequest = null;
      }
    }
  }

  function validMetadata(value, id) {
    return value && value.id === id && hash(value.revision) && value.integrity === "verified"
      && finite(value.duration_s) && value.duration_s >= 0 && Number.isSafeInteger(value.events) && value.events >= 0
      && Array.isArray(value.sources) && value.sources.every((item) => [item.system, item.component].every((n) => Number.isInteger(n) && n >= 0 && n <= 255) && Number.isSafeInteger(item.events))
      && value.download_url === `/api/recordings/${id}/download?revision=${value.revision}`;
  }

  async function openRecording(id, moveFocus) {
    cancelReplay();
    const token = epoch;
    selected = id;
    metadata = snapshot = source = null;
    cursor = 0;
    detailView = "measures";
    messagesOffset = 0;
    messagesPage = null;
    renderDetailView();
    node("messages-list").replaceChildren();
    node("messages-source").replaceChildren(new Option("Toutes les sources", ""));
    node("messages-type").replaceChildren(new Option("Tous les types", ""));
    node("replay-content").hidden = true;
    node("replay-empty").hidden = true;
    node("archive-download").removeAttribute("href");
    text("replay-status", `Vérification du journal ${id.slice(0, 8)}…`);
    markSelection();
    const opener = document.activeElement;
    const controller = request = new AbortController();
    try {
      const value = await getJSON(`/api/recordings/${id}`, controller);
      if (token !== epoch || !visible) return;
      if (!validMetadata(value, id)) throw new Error("Description du journal invalide.");
      metadata = value;
      text("replay-status", "");
      text("replay-title", `Journal ${id.slice(0, 8)}`);
      node("replay-title").title = id;
      text("replay-duration", `Durée ${numeric(value.duration_s, " s")}`);
      text("replay-events", `${number.format(value.events)} trames`);
      const context = value.context;
      const originDate = context?.captured_at_utc ? new Date(context.captured_at_utc) : null;
      text("replay-provenance", context && originDate && Number.isFinite(originDate.getTime())
        ? `Capture du ${date.format(originDate)} · ${context.configuration.environment === "simulation" ? "Simulation" : context.configuration.environment === "real" ? "Réel déclaré" : "Environnement non configuré"} · contexte dans Analyse · sans vidéo enregistrée.`
        : "Télémétrie seule · date et configuration d’origine indisponibles dans ce journal · sans vidéo enregistrée.");
      const endLabels = { transport_error: "Capture interrompue", shutdown: "Capture interrompue", event_limit: "Limite de messages atteinte", size_limit: "Limite de taille atteinte" };
      const endLabel = endLabels[value.end_reason];
      text("replay-end-detail", endLabel ? `${endLabel}. ${typeof value.end_detail === "string" ? value.end_detail : ""} Les réceptions conservées restent consultables.` : "");
      node("replay-end-detail").hidden = !endLabel;
      const limits = value.limits || { heartbeat: 1, battery: 2, attitude: .2, local_position_ned: .4 };
      text("replay-age-note", `Les dernières valeurs reçues restent consultables, même anciennes. Leur âge est calculé au curseur. Seuils ${value.limits_origin === "capture" ? "enregistrés à la capture" : "d’analyse par défaut (seuils de capture inconnus)"} : mode ${numeric(limits.heartbeat, " s")} · batterie ${numeric(limits.battery, " s")} · attitude ${numeric(limits.attitude, " s")} · position ${numeric(limits.local_position_ned, " s")}.`);
      node("archive-download").href = value.download_url;
      node("replay-content").hidden = false;
      node("replay-cursor").max = String(value.duration_s);
      node("replay-cursor").value = "0";
      node("replay-source").replaceChildren();
      const sources = value.sources.filter((item) => item.system > 0 && item.component > 0);
      if (sources.length !== 1) node("replay-source").add(new Option(sources.length ? "Choisir un composant enregistré" : "Aucun composant", ""));
      for (const item of sources) node("replay-source").add(new Option(`Système ${item.system} · composant ${item.component} · ${number.format(item.events)} trames`, `${item.system}:${item.component}`));
      for (const item of value.sources) node("messages-source").add(new Option(`Système ${item.system} · composant ${item.component}`, `${item.system}:${item.component}`));
      node("replay-source").disabled = !sources.length;
      node("replay-no-source").hidden = sources.length > 0;
      node("replay-transport").hidden = sources.length === 0;
      clearMeasures(sources.length ? "Choisissez le composant dont vous voulez relire les mesures." : "");
      text("replay-time", `0 s / ${numeric(value.duration_s, " s")}`);
      if (moveFocus && document.activeElement === opener) {
        (sources.length ? node("replay-source") : node("archive-download")).focus({ preventScroll: true });
        if (matchMedia("(max-width: 760px)").matches) node("replay-content").scrollIntoView({ block: "start" });
      }
      if (sources.length === 1) {
        source = sources[0];
        void seek(0);
      }
    } catch (error) {
      if (token === epoch && visible) text("replay-status", error.name === "AbortError" ? "La vérification n’a pas répondu à temps. Ouvrez à nouveau le journal pour réessayer." : error.message);
    } finally {
      if (token === epoch && request === controller) request = null;
    }
  }

  function validSnapshot(value, id, offset, pair) {
    const fieldObject = (fields) => fields === null || (fields && typeof fields === "object" && !Array.isArray(fields) && Object.values(fields).every((item) => typeof item === "string" || finite(item)));
    return value && value.id === id && value.at_s === offset && value.system === pair.system && value.component === pair.component
      && kinds.every((name) => { const view = value[name]; return view && ["absent", "recent", "stale"].includes(view.state) && (view.rx_age_s === null || (finite(view.rx_age_s) && view.rx_age_s >= 0)) && fieldObject(view.fields); })
      && ["received", "accepted", "rejected", "ignored_source", "ignored_type"].every((key) => Number.isSafeInteger(value[key]) && value[key] >= 0)
      && [value.previous_at_s, value.next_at_s].every((n) => n === null || (finite(n) && n >= 0 && n <= metadata.duration_s))
      && value.mode && typeof value.mode.label === "string";
  }

  function renderSnapshot(value) {
    snapshot = value;
    for (const kind of kinds) {
      const view = value[kind];
      node(`history-${kind}`).dataset.state = view.state;
      text(`history-${kind}-age`, view.state === "absent" ? "Pas encore reçue au curseur" : `${view.state === "stale" ? "Ancienne au curseur" : "Récente au curseur"} · âge ${numeric(view.rx_age_s, " s")}`);
    }
    text("history-mode", value.heartbeat.fields ? value.mode.label : "—");
    const baseMode = value.heartbeat.fields?.base_mode;
    text("history-armed", finite(baseMode) ? baseMode & 128 ? "Armé" : "Désarmé" : "—");
    text("history-mode-raw", numeric(value.mode.custom_mode));
    text("history-remaining", numeric(value.battery.remaining_percent, " %"));
    text("history-voltage", numeric(value.battery.voltage_v, " V"));
    text("history-current", numeric(value.battery.current_a, " A"));
    for (const name of ["roll", "pitch", "yaw"]) text(`history-${name}`, numeric(finite(value.attitude.fields?.[name]) ? value.attitude.fields[name] * 180 / Math.PI : null, "°"));
    for (const [name, axis] of [["north", "x"], ["east", "y"], ["down", "z"]]) text(`history-${name}`, numeric(value.local_position_ned.fields?.[axis], " m"));
    for (const name of ["received", "accepted", "rejected", "ignored-source", "ignored-type"]) text(`history-${name}`, number.format(value[name.replaceAll("-", "_")]));
    const progress = { first: "première réception", advanced: "avance", repeated: "répété", decreased: "en baisse" };
    for (const [name, view] of [["attitude", value.attitude], ["position", value.local_position_ned]]) text(`history-${name}-boot`, view.fields ? `${numeric(view.fields.time_boot_ms, " ms")} · ${progress[view.boot_progress] || "inconnu"}` : "—");
    text("history-rejection", value.last_rejection ? `Dernier refus à cet instant : ${value.last_rejection}` : "Aucun payload refusé à cet instant pour ce composant.");
    node("replay-measures").hidden = false;
    text("replay-cursor-status", playing ? "Relecture en cours · réceptions historiques." : cursor >= metadata.duration_s ? "Fin du journal · relecture en pause." : "Relecture en pause · réceptions historiques.");
    controls(false);
  }

  async function seek(offset, automatic = false) {
    if (!metadata || !source || !visible || detailView !== "measures") return;
    if (!automatic) pause();
    request?.abort();
    const token = ++epoch;
    const id = selected;
    const pair = { system: source.system, component: source.component };
    const target = clampTime(offset);
    if (!automatic) {
      cursor = target;
      node("replay-cursor").value = String(cursor);
      text("replay-time", `${numeric(cursor, " s")} / ${numeric(metadata.duration_s, " s")}`);
      // The old values must never sit under the new time/source label.
      clearMeasures("Lecture des réceptions à cet instant…");
    } else {
      // During playback keep the last confirmed cursor and its values together
      // until the next response arrives; no flicker or invented interpolation.
      controls(true);
    }
    text("replay-status", "");
    const controller = request = new AbortController();
    try {
      const query = new URLSearchParams({ revision: metadata.revision, at: target, system: pair.system, component: pair.component });
      const value = await getJSON(`/api/recordings/${id}/replay?${query}`, controller);
      if (token !== epoch || !visible) return;
      if (!validSnapshot(value, id, target, pair)) throw new Error("Réponse de relecture invalide.");
      cursor = target;
      node("replay-cursor").value = String(cursor);
      text("replay-time", `${numeric(cursor, " s")} / ${numeric(metadata.duration_s, " s")}`);
      if (cursor >= metadata.duration_s) pause();
      renderSnapshot(value);
      if (playing) timer = setTimeout(() => {
        const offset = anchorAt + (performance.now() - anchorTime) / 1000 * Number(node("replay-speed").value);
        void seek(offset, true);
      }, 100);
    } catch (error) {
      if (token !== epoch || !visible) return;
      pause();
      clearMeasures("Relecture interrompue. Déplacez le curseur pour réessayer.");
      text("replay-status", error.name === "AbortError" ? "La relecture n’a pas répondu à temps." : error.message);
    } finally {
      if (token === epoch && request === controller) request = null;
    }
  }

  function renderDetailView() {
    for (const name of ["measures", "messages", "analysis"]) node(`recording-view-${name}`).setAttribute("aria-pressed", String(detailView === name));
    node("recording-measure-view").hidden = detailView !== "measures";
    node("recording-message-view").hidden = detailView !== "messages";
    node("recording-analysis-view").hidden = detailView !== "analysis";
    notifyAnalysis();
  }

  function notifyAnalysis() {
    document.dispatchEvent(new CustomEvent("argos:archive-analysis", {
      detail: { visible: visible && detailView === "analysis" && !document.hidden, metadata }
    }));
  }

  function changeDetailView(name) {
    if (detailView === name || !metadata) return;
    cancelReplay();
    detailView = name;
    renderDetailView();
    text("replay-status", "");
    if (name === "messages") void loadMessages(messagesOffset);
    else if (name === "measures" && source) void seek(cursor);
  }

  function validMessages(value, id, revision, offset, filters) {
    const uint = (item, max) => Number.isSafeInteger(item) && item >= 0 && item <= max;
    if (!value || value.id !== id || value.revision !== revision || value.offset !== offset || value.limit !== 50
      || !uint(value.total, 100000) || !Array.isArray(value.items) || value.items.length !== Math.min(50, Math.max(0, value.total - offset))
      || !Array.isArray(value.message_types) || !value.message_types.every((item) => uint(item.message_id, 16777215) && typeof item.type_name === "string" && uint(item.count, 100000))) return false;
    let previous = 0;
    return value.items.every((item) => {
      const valid = Number.isSafeInteger(item.index) && item.index > previous && item.index <= metadata.events
        && finite(item.at_s) && item.at_s >= 0 && item.at_s <= metadata.duration_s
        && finite(item.received_at) && item.received_at >= 0 && [item.system, item.component, item.sequence].every((n) => uint(n, 255))
        && uint(item.message_id, 16777215) && typeof item.type_name === "string"
        && item.fields && typeof item.fields === "object" && !Array.isArray(item.fields)
        && uint(item.frame_bytes, 280) && item.frame_bytes > 0 && [1, 2].includes(item.wire_version)
        && typeof item.frame_hex === "string" && /^[0-9a-f]+$/.test(item.frame_hex) && item.frame_hex.length === item.frame_bytes * 2
        && (filters.source === "" || `${item.system}:${item.component}` === filters.source)
        && (filters.type === "" || String(item.message_id) === filters.type);
      previous = item.index;
      return valid;
    });
  }

  function messageRow(item) {
    const row = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const time = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 6 }).format(item.at_s);
    summary.setAttribute("aria-label", `Message ${item.index}, ${item.type_name}, ${time} secondes après le début, système ${item.system}, composant ${item.component}, séquence ${item.sequence}`);
    for (const [className, content] of [["message-number", `#${item.index}`], ["message-time", `+${time} s`], ["message-name", item.type_name], ["message-origin", `${item.system} / ${item.component}`], ["message-sequence", `seq ${item.sequence}`]]) {
      const span = document.createElement("span");
      span.className = className;
      span.textContent = content;
      summary.append(span);
    }
    const body = document.createElement("div");
    body.className = "message-body";
    const heading = document.createElement("p");
    heading.className = "section-note";
    heading.textContent = `MAVLink ${item.wire_version} · message ID ${item.message_id} · ${item.frame_bytes} octets · réception locale ${item.received_at} s`;
    const fields = document.createElement("dl");
    fields.className = "message-fields";
    for (const [key, value] of Object.entries(item.fields)) {
      const line = document.createElement("div");
      const label = document.createElement("dt");
      const field = document.createElement("dd");
      label.textContent = key;
      field.textContent = JSON.stringify(value);
      line.append(label, field);
      fields.append(line);
    }
    const hexTitle = document.createElement("h3");
    hexTitle.textContent = "Trame originale · hexadécimal";
    const hex = document.createElement("pre");
    hex.tabIndex = 0;
    hex.setAttribute("aria-label", "Octets de la trame originale");
    hex.textContent = item.frame_hex.match(/.{1,32}/g).map((line) => line.match(/../g).join(" ")).join("\n");
    body.append(heading, fields, hexTitle, hex);
    details.append(summary, body);
    row.append(details);
    return row;
  }

  async function loadMessages(offset) {
    if (!metadata || !visible || detailView !== "messages") return;
    request?.abort();
    const token = ++epoch;
    const id = selected, revision = metadata.revision;
    const opener = document.activeElement;
    const restoreFocus = [node("messages-previous"), node("messages-next")].includes(opener);
    const filters = { source: node("messages-source").value, type: node("messages-type").value };
    messagesOffset = offset;
    messagesPage = null;
    node("messages-list").replaceChildren();
    node("messages-list").setAttribute("aria-busy", "true");
    node("messages-previous").disabled = true;
    node("messages-next").disabled = true;
    node("messages-retry").hidden = true;
    text("messages-count", "");
    text("messages-status", "Lecture des messages…");
    const controller = request = new AbortController();
    try {
      const query = new URLSearchParams({ revision, offset, limit: 50 });
      if (filters.source) {
        const [system, component] = filters.source.split(":");
        query.set("system", system); query.set("component", component);
      }
      if (filters.type) query.set("message_id", filters.type);
      const value = await getJSON(`/api/recordings/${id}/messages?${query}`, controller);
      if (token !== epoch || !visible || detailView !== "messages") return;
      if (!validMessages(value, id, revision, offset, filters)) throw new Error("Réponse de messages invalide.");
      messagesPage = value;
      node("messages-type").replaceChildren(new Option("Tous les types", ""));
      for (const item of value.message_types) node("messages-type").add(new Option(`${item.type_name} · ID ${item.message_id}`, String(item.message_id)));
      node("messages-type").value = filters.type;
      const fragment = document.createDocumentFragment();
      for (const item of value.items) fragment.append(messageRow(item));
      node("messages-list").replaceChildren(fragment);
      text("messages-count", value.total ? `${offset + 1}–${offset + value.items.length} / ${number.format(value.total)} messages` : "0 message");
      text("messages-status", value.total ? "" : "Aucun message ne correspond à ces filtres.");
      node("messages-previous").disabled = offset === 0;
      node("messages-next").disabled = offset + value.items.length >= value.total;
      if (restoreFocus && [document.body, opener].includes(document.activeElement)) {
        (node("messages-list").querySelector("summary") || node("messages-count")).focus({ preventScroll: true });
      }
    } catch (error) {
      if (token !== epoch || !visible || detailView !== "messages") return;
      text("messages-status", error.name === "AbortError" ? "Les messages n’ont pas répondu à temps." : error.message);
      node("messages-retry").hidden = false;
      if (restoreFocus && [document.body, opener].includes(document.activeElement)) node("messages-retry").focus({ preventScroll: true });
    } finally {
      if (token === epoch && request === controller) {
        request = null;
        node("messages-list").setAttribute("aria-busy", "false");
      }
    }
  }

  document.addEventListener("argos:workspace-changed", (event) => setWorkspace(event.detail.view === "sessions"));
  node("archive-journal").addEventListener("click", () => {
    document.dispatchEvent(new Event("argos:show-observation"));
    document.querySelector('.reception-bar [data-panel="recording"]').click();
  });
  node("archive-refresh").addEventListener("click", () => { void refreshCatalog(); });
  for (const name of ["measures", "messages", "analysis"]) node(`recording-view-${name}`).addEventListener("click", () => changeDetailView(name));
  for (const name of ["source", "type"]) node(`messages-${name}`).addEventListener("change", () => { void loadMessages(0); });
  node("messages-previous").addEventListener("click", () => { if (messagesPage) void loadMessages(Math.max(0, messagesOffset - 50)); });
  node("messages-next").addEventListener("click", () => { if (messagesPage) void loadMessages(messagesOffset + 50); });
  node("messages-retry").addEventListener("click", () => { void loadMessages(messagesOffset); });
  node("replay-source").addEventListener("change", () => {
    cancelReplay();
    const [system, component] = node("replay-source").value.split(":").map(Number);
    source = metadata.sources.find((item) => item.system === system && item.component === component) || null;
    clearMeasures("Choisissez le composant dont vous voulez relire les mesures.");
    if (source) void seek(cursor);
  });
  node("replay-cursor").addEventListener("input", () => {
    cancelReplay();
    cursor = clampTime(Number(node("replay-cursor").value));
    text("replay-time", `${numeric(cursor, " s")} / ${numeric(metadata.duration_s, " s")}`);
    clearMeasures("Lecture des réceptions à cet instant…");
    seekTimer = setTimeout(() => { void seek(cursor); }, 70);
  });
  node("replay-play").addEventListener("click", () => {
    if (playing) {
      // Stop the in-flight seek too: Pause preserves the last confirmed cursor.
      cancelReplay();
      controls(false);
      text("replay-cursor-status", "Relecture en pause · réceptions historiques.");
      return;
    }
    if (!metadata || !source || !snapshot) return;
    playing = true;
    anchorAt = cursor >= metadata.duration_s ? 0 : cursor;
    anchorTime = performance.now();
    text("replay-play", "Pause");
    void seek(anchorAt, true);
  });
  for (const name of ["previous", "next"]) node(`replay-${name}`).addEventListener("click", () => {
    const target = snapshot?.[`${name}_at_s`];
    if (finite(target)) void seek(target);
  });
  node("replay-speed").addEventListener("change", () => { anchorAt = cursor; anchorTime = performance.now(); });
  document.addEventListener("visibilitychange", () => {
    notifyAnalysis();
    if (document.hidden) {
      if (playing) {
        cancelReplay();
        controls(false);
      } else pause();
      text("replay-cursor-status", "Relecture en pause · onglet masqué.");
    }
  });
  window.addEventListener("pagehide", () => {
    visible = false;
    notifyAnalysis();
    cancelReplay();
    catalogRequest?.abort();
  });
})();
