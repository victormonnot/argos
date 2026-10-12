const fs = require('node:fs');
const path = require('node:path');
const { test, expect, ID, REVISION } = require('./fixtures.cjs');
const VISUAL_REVISION = 'c'.repeat(64);
const index = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/index.html'), 'utf8');
const welcome = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/demo-welcome.html'), 'utf8');
const absent = () => ({ state: 'absent', rx_age_s: null, received_at: null, age_limit_s: 2, fields: null, boot_progress: null });

// Only fixture responses are synthetic. Production reads the bundled, unmodified recording.
async function installRecorded(page, model) {
  const jpeg = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 160; canvas.height = 90;
    const context = canvas.getContext('2d');
    return ['#b52222', '#2233bb'].map(color => {
      context.fillStyle = color; context.fillRect(0, 0, 160, 90);
      return canvas.toDataURL('image/jpeg').split(',')[1];
    });
  });
  const mock = { calls: [], hold: null, frameState: 'recent', noFrame: false, overrideVisual: null, overrideReplay: null };
  const metadata = { ...structuredClone(model.metadata), visual: {
    state: 'complete', id: ID, revision: VISUAL_REVISION, frames: 2, samples: 3, events: 2, duration_s: 3,
  } };
  const console = index.replace('<body data-view="observation">', '<body data-demo="true" data-view="observation">');
  await page.route('http://127.0.0.1:4173/', route => route.fulfill({ contentType: 'text/html', body: console.replace('</body>', `${welcome}</body>`) }));
  await page.route('http://127.0.0.1:4173/demo', route => route.fulfill({ contentType: 'text/html', body: console }));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    mock.calls.push({ method: request.method(), path: pathname, query: Object.fromEntries(url.searchParams) });
    expect(request.method()).toBe('GET');
    const at = Number(url.searchParams.get('at'));
    const frameRequest = pathname.includes('/frames/');
    const index = frameRequest ? Number(pathname.match(/\/(\d+)\.jpg$/)[1]) : at >= 1.5 ? 2 : 1;
    const hold = mock.hold;
    if (hold && hold.when(pathname, url.searchParams)) {
      mock.hold = null; hold.requested = true; model.pending.add(hold);
      await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
    }
    if (pathname === '/api/demo-config') return route.fulfill({ json: { enabled: true, recording_id: ID } });
    if (pathname === '/api/recordings') return route.fulfill({ json: { directory: '/synthetic/recordings', total: 1, limit: 100,
      items: [{ id: ID, state: 'unverified', modified_at: 1750000000, size_bytes: 1024 }] } });
    if (pathname === `/api/recordings/${ID}`) return route.fulfill({ json: metadata });
    expect(url.searchParams.get('revision')).toBe(REVISION);
    if (pathname.includes('/visual')) expect(url.searchParams.get('visual_revision')).toBe(VISUAL_REVISION);
    if (frameRequest) return route.fulfill({ contentType: 'image/jpeg', body: Buffer.from(jpeg[index - 1], 'base64') }).catch(() => {});
    if (pathname.endsWith('/visual')) {
      const value = { id: ID, revision: REVISION, visual_revision: VISUAL_REVISION, at_s: at,
        state: mock.frameState === 'stale' ? 'stale' : 'recent', sample: { at_s: at },
        control: { vehicle: { armed: true, mode: index === 2 ? 2 : 0 }, phase: 'armed',
          framing: { active: index === 2, phase: index === 2 ? 'active' : 'idle', target_id: index === 2 ? 7 : null, reference_height: .3 } },
        frame: mock.noFrame ? null : { index, url: `/api/recordings/${ID}/visual/frames/${index}.jpg?revision=${REVISION}&visual_revision=${VISUAL_REVISION}`,
          sequence: index * 10, video_id: 'recorded-camera', at_s: index === 2 ? 1.5 : 0, available_at_s: index === 2 ? 1.5 : 0,
          age_s: at - (index === 2 ? 1.5 : 0), state: mock.frameState, width: 160, height: 90,
          detections: index === 2 ? [{ track_id: 7, confidence: .9, box: [.25, .2, .2, .6] }] : [] },
        events: [{ at_s: .1, kind: 'claim', detail: 'Pilot requested control', status: 'accepted' },
          { at_s: 1.5, kind: 'framing', detail: 'Engage framing requested', status: 'accepted' }].filter(event => event.at_s <= at) };
      mock.overrideVisual?.(value);
      return route.fulfill({ json: value }).catch(() => {});
    }
    if (pathname.endsWith('/replay')) {
      const value = { id: ID, revision: REVISION, at_s: at, system: 1, component: 1,
        heartbeat: { state: 'recent', rx_age_s: at % 1, received_at: at - at % 1, age_limit_s: 2.5,
          fields: { base_mode: 128, custom_mode: index === 2 ? 2 : 0, system_status: 4 }, boot_progress: null },
        battery: { ...absent(), state: 'recent', rx_age_s: .1, fields: { voltage_battery: 12400, current_battery: 100, battery_remaining: 80 }, voltage_v: 12.4, current_a: 1, remaining_percent: 80 },
        attitude: index === 2 ? { ...absent(), state: 'recent', rx_age_s: at - 1.5, fields: { roll: .2, pitch: .1, yaw: .3, time_boot_ms: 1500 } } : absent(),
        local_position_ned: absent(), received: index === 2 ? 2 : 1, accepted: index === 2 ? 2 : 1, rejected: 0, ignored_source: 0, ignored_type: 0, last_rejection: '',
        previous_at_s: at > 0 ? Math.max(0, at - .2) : null, next_at_s: at < 3 ? Math.min(3, at + .2) : null,
        mode: { label: index === 2 ? 'ALT_HOLD' : 'STABILIZE', custom_mode: index === 2 ? 2 : 0 } };
      mock.overrideReplay?.(value);
      return route.fulfill({ json: value }).catch(() => {});
    }
    if (pathname.endsWith('/messages')) {
      const all = [
        { index: 1, at_s: 0, type_name: 'HEARTBEAT', message_id: 0, fields: { custom_mode: 0 } },
        { index: 2, at_s: 1.5, type_name: 'ATTITUDE', message_id: 30, fields: { roll: .2, unsafe: '<img src=x onerror=window.injected=true>' } },
        { index: 3, at_s: 2.5, type_name: 'HEARTBEAT', message_id: 0, fields: { custom_mode: 2 } },
      ].map(item => ({ ...item, system: 1, component: 1, sequence: item.index, received_at: 100 + item.at_s,
        frame_bytes: 3, wire_version: 2, frame_hex: 'fd00a0' }));
      const filtered = all.filter(item => !url.searchParams.has('message_id') || String(item.message_id) === url.searchParams.get('message_id'));
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit')) || 50;
      return route.fulfill({ json: { id: ID, revision: REVISION, offset, limit, total: filtered.length, items: filtered.slice(offset, offset + limit),
        message_types: [{ message_id: 0, type_name: 'HEARTBEAT', count: 2 }, { message_id: 30, type_name: 'ATTITUDE', count: 1 }] } });
    }
    // Framing-report and analysis have independent test suites; preserve their inert defaults here.
    return route.fallback();
  });
  return mock;
}

async function openDemo(page) {
  await page.goto('/demo');
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#camera-image')).toBeVisible();
}
async function seek(page, at) {
  await page.locator('#replay-cursor').fill(String(at));
}
async function expectRed(page, selector = '#camera-image') {
  const pixel = await page.locator(selector).evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data];
  });
  expect(pixel[0]).toBeGreaterThan(pixel[2] * 2);
}
function readOnly(mock) {
  expect(mock.calls.every(call => call.method === 'GET' && (call.path === '/api/demo-config' || call.path.startsWith('/api/recordings')))).toBe(true);
}

test('recorded camera, telemetry and Sessions use one cursor without enabling flight operations', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page);
  await expect(page.locator('#demo-playback')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expectRed(page);
  await seek(page, 2);
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await page.locator('#view-control').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#control-panel')).toBeVisible();
  for (const control of await page.locator('#control-panel button, #control-panel input, #control-panel select').all()) await expect(control).toBeDisabled();
  await page.locator('#control-claim').dispatchEvent('click');
  await page.keyboard.press('w');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('.flight-replay-box')).toHaveCount(1);
  await page.locator('#recording-view-measures').click();
  await expect(page.locator('#history-mode')).toHaveText('ALT_HOLD');
  await seek(page, .5);
  await expect(page.locator('#history-mode')).toHaveText('STABILIZE');
  await page.locator('#view-observation').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expectRed(page);
  await page.locator('#replay-speed').selectOption('2');
  await page.locator('#replay-play').click();
  await expect.poll(async () => Number(await page.locator('#replay-cursor').inputValue())).toBeGreaterThan(.5);
  await page.locator('#replay-play').click();
  const paused = await page.locator('#replay-cursor').inputValue();
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-cursor')).toHaveValue(paused);
  await expect(page.locator('#replay-speed')).toHaveValue('2');
  readOnly(mock);
});

for (const frame of [false, true]) {
  test(`a delayed recorded ${frame ? 'JPEG' : 'snapshot'} cannot overwrite a backward seek`, async ({ page, model }) => {
    const mock = await installRecorded(page, model);
    await openDemo(page);
    const hold = { when: (pathname, query) => frame ? pathname.endsWith('/frames/2.jpg') : pathname.endsWith('/visual') && query.get('at') === '2' };
    mock.hold = hold;
    await seek(page, 2);
    await expect.poll(() => hold.requested).toBe(true);
    await expect(page.locator('#camera-image')).toBeHidden();
    await expect(page.locator('.vision-box')).toHaveCount(0);
    await seek(page, .5);
    await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
    await expect(page.locator('#camera-image')).toBeVisible();
    hold.release();
    await page.waitForTimeout(150);
    await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
    await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
    await expect(page.locator('.vision-box')).toHaveCount(0);
    await expectRed(page);
    readOnly(mock);
  });
}

test('paused recorded ages stay at the cursor and stale or absent images clear detections', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 2);
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await page.locator('#mode-button').click();
  const age = await page.locator('#detail-heartbeat-age').textContent();
  await page.waitForTimeout(800);
  await expect(page.locator('#detail-heartbeat-age')).toHaveText(age);
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  mock.frameState = 'stale';
  const frames = mock.calls.filter(call => call.path.includes('/frames/')).length;
  await seek(page, 2.5);
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  expect(mock.calls.filter(call => call.path.includes('/frames/'))).toHaveLength(frames);
  mock.noFrame = true;
  await seek(page, 0);
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('#camera-image')).toBeHidden();
  readOnly(mock);
});

for (const width of [1366, 320, 390]) {
  test(`MAVLink inspection follows the recorded cursor with reachable fields at ${width}px`, async ({ page, model }) => {
  await page.setViewportSize({ width, height: 844 });
  const mock = await installRecorded(page, model);
  await openDemo(page);
  await page.locator('#view-messages').click();
  await expect(page.locator('.live-type')).toHaveCount(1);
  await expect(page.locator('.live-type')).toContainText('HEARTBEAT');
  await seek(page, 2);
  await expect(page.locator('.live-type')).toHaveCount(2);
  await page.locator('#live-type-filter').fill('ATTITUDE');
  await expect(page.locator('.live-type:visible')).toHaveCount(1);
  await page.locator('.live-type:visible').click();
  await expect(page.locator('.live-selected-title')).toContainText('ATTITUDE');
  await expect(page.locator('.live-fields')).toContainText('<img src=x onerror=window.injected=true>');
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  await page.locator('.live-wire summary').click();
  await expect(page.locator('.live-hex')).toContainText('fd 00 a0');
  if (width < 760) await page.locator('.live-mobile-navigation button').first().click();
  await page.locator('#live-source-filter').selectOption('1:1');
  await page.locator('#live-type-filter').fill('');
  await seek(page, .5);
  await expect(page.locator('.live-type')).toHaveCount(1);
  await expect(page.locator('.live-type')).toContainText('HEARTBEAT');
  await expect(page.locator('.live-selected-title')).not.toContainText('ATTITUDE');
  await page.locator('#replay-play').click();
  await expect(page.locator('#replay-play')).toHaveText('Pause');
  await page.locator('.live-freeze').click();
  await expect(page.locator('#replay-play')).toHaveText('Play');
  const paused = await page.locator('#replay-cursor').inputValue();
  await page.waitForTimeout(250);
  await expect(page.locator('#replay-cursor')).toHaveValue(paused);
  readOnly(mock);
});
}

test('separate visitor tabs keep independent recorded cursors and playback speeds', async ({ page, model, context }) => {
  const first = await installRecorded(page, model);
  const secondPage = await context.newPage();
  const errors = [];
  secondPage.on('pageerror', error => errors.push(error.message));
  const second = await installRecorded(secondPage, model);
  try {
    await openDemo(page); await openDemo(secondPage);
    await seek(page, 2);
    await page.locator('#replay-speed').selectOption('2');
    await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
    await expect(secondPage.locator('#replay-cursor')).toHaveValue('0');
    await expect(secondPage.locator('#replay-speed')).toHaveValue('1');
    await expectRed(secondPage);
    await seek(secondPage, .5);
    await expect(page.locator('#replay-cursor')).toHaveValue('2');
    await expect(page.locator('#replay-speed')).toHaveValue('2');
    readOnly(first); readOnly(second);
    expect(errors).toEqual([]);
  } finally { await secondPage.close(); }
});

test('a new recorded image waits for telemetry from the same cursor before publication', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page);
  const hold = { when: (pathname, query) => pathname.endsWith('/replay') && query.get('at') === '2' };
  mock.hold = hold;
  await seek(page, 2);
  await expect.poll(() => hold.requested).toBe(true);
  await expect.poll(() => mock.calls.some(call => call.path.endsWith('/visual') && call.query.at === '2')).toBe(true);
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#flight-mode')).toHaveText('—');
  await expect(page.locator('.vision-box')).toHaveCount(0);
  hold.release();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  readOnly(mock);
});

test('invalid archived image bindings clear all observations and a fresh seek can recover', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page);
  const frames = mock.calls.filter(call => call.path.includes('/frames/')).length;
  mock.overrideVisual = value => { value.frame.url = 'https://example.com/unrelated.jpg'; };
  await seek(page, 2);
  await expect(page.locator('#replay-cursor-status')).toContainText('Replay unavailable');
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#flight-mode')).toHaveText('—');
  expect(mock.calls.filter(call => call.path.includes('/frames/'))).toHaveLength(frames);
  mock.overrideVisual = value => { value.visual_revision = 'd'.repeat(64); };
  await seek(page, .5);
  await expect(page.locator('#replay-cursor-status')).toContainText('Replay unavailable');
  await expect(page.locator('#camera-image')).toBeHidden();
  expect(mock.calls.filter(call => call.path.includes('/frames/'))).toHaveLength(frames);
  mock.overrideVisual = null;
  await seek(page, 0);
  await expect(page.locator('#camera-image')).toBeVisible();
  await expectRed(page);
  readOnly(mock);
});

test('clearing a recorded telemetry component removes its values while camera replay remains usable', async ({ page, model }) => {
  model.metadata.sources.push({ system: 1, component: 42, events: 0 });
  const mock = await installRecorded(page, model);
  await openDemo(page);
  await page.locator('#view-sessions').click();
  await page.locator('#recording-view-measures').click();
  await page.locator('#replay-source').selectOption('1:1');
  await expect(page.locator('#history-mode')).toHaveText('STABILIZE');
  await seek(page, 2);
  await expect(page.locator('#history-mode')).toHaveText('ALT_HOLD');
  await page.locator('#replay-source').selectOption('');
  await page.locator('#view-observation').click();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('—');
  await expect(page.locator('#telemetry-status')).toHaveText('No recorded telemetry component');
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#replay-play')).toBeEnabled();
  readOnly(mock);
});

test('hiding the tab interrupts a pending seek and returning resumes that cursor without autoplay', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page);
  const hold = { when: (pathname, query) => pathname.endsWith('/visual') && query.get('at') === '2' };
  mock.hold = hold;
  await seek(page, 2);
  await expect.poll(() => hold.requested).toBe(true);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  hold.release();
  await page.waitForTimeout(150);
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#replay-play')).toHaveText('Play');
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#replay-play')).toHaveText('Play');
  await expect(page.locator('.vision-box')).toHaveCount(1);
  readOnly(mock);
});
