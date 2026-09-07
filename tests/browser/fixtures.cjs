const { test: base, expect } = require('@playwright/test');
const ID = 'a'.repeat(32), REVISION = 'b'.repeat(64);
const absent = () => ({ state: 'absent', rx_age_s: null, received_at: null, age_limit_s: 2, fields: null, boot_progress: null });
function recording(overrides = {}) {
  return { state: 'idle', id: null, events: 0, started_at: null, ended_at: null, error: '', download_url: null,
    end_reason: null, end_detail: '', max_events: 100000, max_bytes: 33554432, size_bytes: 0, ...overrides };
}
function state(at, model) {
  const heartbeat = { state: 'recent', rx_age_s: .01, received_at: at - .01, age_limit_s: 2.5,
    fields: { base_mode: 128, custom_mode: 0, type: 2, autopilot: 3, system_status: 4 }, boot_progress: null };
  const value = { schema_version: 1, run_id: model.run, at, environment: 'simulation',
    configuration: { environment: 'simulation', video_source: 'none', video_endpoint: null, mavlink_transport: 'tcp',
      mavlink_bind: null, mavlink_peer: null, mavlink_tcp: '127.0.0.1:5760', mavlink_device: null,
      baudrate: 115200, sequence_scope: 'channel', system: 1, component: 1 },
    recording: structuredClone(model.recording),
    video: { source: 'none', source_id: 'video-1', state: 'unconfigured', label: 'Aucune caméra', detail: '', endpoint: null,
      sequence: 0, received_at: null, rx_age_s: null, age_limit_s: 1, width: null, height: null, rejected: 0, last_rejection: '' },
    reconnecting: null, reception: { active: [], last_recovery: null }, events: [],
    telemetry: { state: 'receiving', detail: 'Réceptions du composant sélectionné', endpoint: 'TCP 127.0.0.1:5760',
      connection_id: model.connection, system: 1, component: 1, rx_messages: 60, rx_bytes: 1800, bad_bytes: 0,
      accepted: 30, ignored_source: 0, ignored_type: 30, rejected: 0, last_rejection: '',
      mode: { state: 'recent', rx_age_s: .01, age_limit_s: 2.5, label: 'STABILIZE', known: true, custom_mode: 0 },
      heartbeat, attitude: absent(), local_position_ned: absent(),
      battery: { ...absent(), voltage_v: null, current_a: null, remaining_percent: null },
      autopilot_status: { declaration: { state: 'recent', system_status: 4, name: 'MAV_STATE_ACTIVE', label: 'Actif', known: true,
        received_at: at - .01, rx_age_s: .01, age_limit_s: 2.5 },
        texts: { system: 1, component: 1, connection_id: model.connection, entries: structuredClone(model.texts),
          max_entries: 60, evicted_entries: 0, pending: 0, max_pending: 8, max_chunks: 32, chunk_timeout_s: 2,
          rejected: 0, last_rejection: '', duplicates: 0 } } } };
  model.modifyState(value);
  return value;
}
function live(at, model) {
  const type = (system, component, id) => ({ system, component, message_id: id, type_name: id === 30 ? 'ATTITUDE' : id === 0 ? 'HEARTBEAT' : `MESSAGE_${id}`,
    count: 20, sequence: 23, last_received_at: at - .1, rx_age_s: .1, hz: 6.67,
    fields: { roll: model.field, exact: '18446744073709551615', text: '<img src=x onerror=window.injected=true>',
      ...Object.fromEntries(Array.from({length: 25}, (_, i) => [`field_${i}`, i])) },
    frame_hex: 'fd00a0', frame_bytes: 3, wire_version: 2 });
  return { run_id: model.run, connection_id: model.connection, at, state: 'receiving', rx_messages: 60, rx_bytes: 1800,
    bad_bytes: 0, unsupported_frames: 0, read_errors: 0, window_s: 3, rate_resolution_s: .1,
    max_types: 256, evicted_types: 0, types: [type(1, 1, 30), type(1, 42, 30), type(2, 1, 0),
      ...Array.from({length: 15}, (_, i) => type(1, 1, 100 + i))] };
}
function replay(at) {
  const heartbeat = { state: 'recent', rx_age_s: at % 1, fields: { base_mode: 128 }, boot_progress: null };
  return { id: ID, at_s: at, system: 1, component: 1, heartbeat, battery: absent(), attitude: absent(), local_position_ned: absent(),
    received: 1, accepted: 1, rejected: 0, ignored_source: 0, ignored_type: 0, last_rejection: '',
    previous_at_s: at > 0 ? Math.max(0, Math.floor(at) - 1) : null, next_at_s: at < 3 ? Math.min(3, Math.floor(at) + 1) : null,
    mode: { label: 'STABILIZE', custom_mode: 0 } };
}
function analysis(query) {
  const bins = Number(query.get('bins')), duration = 3;
  const filter = Object.fromEntries(['system', 'component', 'message_id'].map(key => [key, query.has(key) ? Number(query.get(key)) : null]));
  return { id: ID, revision: REVISION, duration_s: duration, filter, events: 3,
    bins: Array.from({length: bins}, (_, i) => ({ start_s: duration * i / bins, end_s: duration * (i + 1) / bins,
      events: i < 3 ? 1 : 0, rate_hz: i < 3 ? bins / duration : 0, max_age_s: 1 })),
    gaps: [], gap_count: 0, gaps_truncated: false, gap_threshold_s: Number(query.get('gap_threshold_s')),
    summary: { mean_rate_hz: 1, max_gap_s: 1 }, sources: [{ system: 1, component: 1, events: 3 }],
    message_types: [{ message_id: 0, type_name: 'HEARTBEAT', events: 3 }] };
}
function statusText(overrides = {}) {
  return { id: 1, system: 1, component: 1, connection_id: 'connection-1', status_id: 0, severity: 4,
    severity_name: 'MAV_SEVERITY_WARNING', severity_label: 'Avertissement', text: 'Précontrôle déclaré', complete: true,
    reason: null, utf8_valid: true, first_received_at: 90, received_at: 90, rx_age_s: 10, chunks: 1, ...overrides };
}
const test = base.extend({
  model: async ({ page }, use) => {
    const start = performance.now(), errors = [], unexpected = [];
    const model = { pending: new Set(), run: 'run-1', connection: 'connection-1', recording: recording(), texts: [], field: .5,
      frozen: false, offline: false, modifyState: () => {}, calls: [], hold: null,
      metadata: { id: ID, revision: REVISION, integrity: 'verified', duration_s: 3, events: 3,
        sources: [{ system: 1, component: 1, events: 3 }], context: null, context_status: 'unknown',
        end_reason: null, end_detail: '', download_url: `/api/recordings/${ID}/download?revision=${REVISION}` },
      clock: () => 100 + (performance.now() - start) / 1000 };
    page.on('pageerror', error => errors.push(error.message));
    // No test can reach a user's live server or any external endpoint.
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://127.0.0.1:4173') { unexpected.push(request.url()); return route.abort(); }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const method = request.method(), path = url.pathname;
      model.calls.push({ method, path, query: url.searchParams });
      let value;
      if (method === 'POST' && ['/api/recordings/start', '/api/recordings/stop'].includes(path)) {
        model.recording = recording(path.endsWith('/start')
          ? { state: 'recording', id: ID, started_at: model.clock(), events: 3 }
          : { state: 'complete', id: ID, started_at: 100, ended_at: model.clock(), events: 3, end_reason: 'stopped', download_url: `/api/recordings/${ID}/download` });
        value = model.recording;
      } else if (method !== 'GET') { unexpected.push(`${method} ${path}`); return route.fulfill({status: 405, json: {detail: 'Unexpected fixture mutation'}}); }
      else if (path === '/api/state') {
        if (model.offline) return route.abort();
        if (!model.frozen) model.lastAt = model.clock();
        value = state(model.lastAt, model);
      } else if (path === '/api/mavlink/messages') value = live(model.clock(), model);
      else if (path === '/api/recordings') value = { directory: '/synthetic/recordings', total: 1, limit: 100,
        items: [{id: ID, state: 'unverified', modified_at: 1750000000, size_bytes: 1024}] };
      else if (path === `/api/recordings/${ID}`) value = structuredClone(model.metadata);
      else if (path === `/api/recordings/${ID}/replay`) value = replay(Number(url.searchParams.get('at')));
      else if (path === `/api/recordings/${ID}/analysis`) value = analysis(url.searchParams);
      else if (path === `/api/recordings/${ID}/messages`) value = { id: ID, revision: REVISION, offset: 0, limit: 50, total: 0, items: [], message_types: [] };
      else { unexpected.push(`${method} ${path}`); return route.fulfill({status: 404, json: {detail: 'Unknown fixture request'}}); }
      const hold = model.hold;
      if (hold && hold.when(path, url.searchParams)) {
        model.hold = null;
        hold.requested = true;
        model.pending.add(hold);
        await new Promise(resolve => { hold.release = resolve; });
        model.pending.delete(hold);
      }
      try { await route.fulfill({json: value}); } catch { /* Deliberately delayed replies can be aborted. */ }
    });
    await use(model);
    for (const hold of model.pending) hold.release?.();
    expect(errors, 'Uncaught browser errors').toEqual([]);
    expect(unexpected, 'Unexpected or external network requests').toEqual([]);
  },
});
async function open(page) {
  await page.goto('/');
  await expect(page.locator('#service-status')).toHaveText('Service connecté');
}
async function openArchive(page) {
  await page.locator('#view-sessions').click();
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#replay-play')).toBeEnabled();
}
module.exports = { test, expect, ID, REVISION, recording, statusText, open, openArchive };
