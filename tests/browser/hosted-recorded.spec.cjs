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
  const mock = { calls: [], hold: null, frameState: 'recent', noFrame: false, overrideVisual: null, overrideReplay: null, respond: null, messageItems: null };
  const metadata = { ...structuredClone(model.metadata), visual: {
    state: 'complete', id: ID, revision: VISUAL_REVISION, frames: 2, samples: 3, events: 2, duration_s: 3,
  } };
  mock.metadata = metadata;
  const console = index.replace('<body data-view="observation">', '<body data-demo="true" data-view="observation">');
  await page.route('http://127.0.0.1:4173/', route => route.fulfill({ contentType: 'text/html', body: console.replace('</body>', `${welcome}</body>`) }));
  await page.route('http://127.0.0.1:4173/demo', route => route.fulfill({ contentType: 'text/html', body: console }));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    mock.calls.push({ method: request.method(), path: pathname, query: Object.fromEntries(url.searchParams) });
    expect(request.method()).toBe('GET');
    const replacement = mock.respond?.(pathname, url.searchParams);
    if (replacement) return route.fulfill(replacement).catch(() => {});
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
      items: [{ id: metadata.id, state: 'unverified', modified_at: 1750000000, size_bytes: 1024 }] } });
    if (pathname === `/api/recordings/${metadata.id}`) return route.fulfill({ json: metadata });
    expect(url.searchParams.get('revision')).toBe(metadata.revision);
    if (pathname.includes('/visual')) expect(url.searchParams.get('visual_revision')).toBe(metadata.visual.revision);
    if (frameRequest) return route.fulfill({ contentType: 'image/jpeg', body: Buffer.from(jpeg[index - 1], 'base64') }).catch(() => {});
    if (pathname.endsWith('/visual')) {
      const value = { id: metadata.id, revision: metadata.revision, visual_revision: metadata.visual.revision, at_s: at,
        state: mock.frameState === 'stale' ? 'stale' : 'recent', sample: { at_s: at },
        control: { vehicle: { armed: true, mode: index === 2 ? 2 : 0 }, phase: 'armed',
          framing: { active: index === 2, phase: index === 2 ? 'active' : 'idle', target_id: index === 2 ? 7 : null, reference_height: .3 } },
        frame: mock.noFrame ? null : { index, url: `/api/recordings/${metadata.id}/visual/frames/${index}.jpg?revision=${metadata.revision}&visual_revision=${metadata.visual.revision}`,
          sequence: index * 10, video_id: 'recorded-camera', at_s: index === 2 ? 1.5 : 0, available_at_s: index === 2 ? 1.5 : 0,
          age_s: at - (index === 2 ? 1.5 : 0), state: mock.frameState, width: 160, height: 90,
          detections: index === 2 ? [{ track_id: 7, confidence: .9, box: [.25, .2, .2, .6] }] : [] },
        events: [{ at_s: .1, kind: 'claim', detail: 'Pilot requested control', status: 'accepted' },
          { at_s: 1.5, kind: 'framing', detail: 'Engage framing requested', status: 'accepted' }].filter(event => event.at_s <= at) };
      mock.overrideVisual?.(value);
      return route.fulfill({ json: value }).catch(() => {});
    }
    if (pathname.endsWith('/replay')) {
      const value = { id: metadata.id, revision: metadata.revision, at_s: at, system: 1, component: 1,
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
      const all = mock.messageItems || [
        { index: 1, at_s: 0, type_name: 'HEARTBEAT', message_id: 0, fields: { custom_mode: 0 } },
        { index: 2, at_s: 1.5, type_name: 'ATTITUDE', message_id: 30, fields: { roll: .2, unsafe: '<img src=x onerror=window.injected=true>' } },
        { index: 3, at_s: 2.5, type_name: 'HEARTBEAT', message_id: 0, fields: { custom_mode: 2 } },
      ].map(item => ({ ...item, system: 1, component: 1, sequence: item.index, received_at: 100 + item.at_s,
        frame_bytes: 3, wire_version: 2, frame_hex: 'fd00a0' }));
      const filtered = all.filter(item => !url.searchParams.has('message_id') || String(item.message_id) === url.searchParams.get('message_id'));
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit')) || 50;
      return route.fulfill({ json: { id: metadata.id, revision: metadata.revision, offset, limit, total: filtered.length, items: filtered.slice(offset, offset + limit),
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

for (const direction of ['next', 'previous']) {
  test(`hosted ${direction} keeps the confirmed image and measurements through snapshot and JPEG loading`, async ({ page, model }) => {
    const mock = await installRecorded(page, model);
    const from = direction === 'next' ? 1.3 : 1.5, target = direction === 'next' ? 1.5 : 1.3;
    const frame = target >= 1.5 ? 2 : 1;
    await openDemo(page); await seek(page, from);
    await page.locator('#view-sessions').click();
    await expect(page.locator('#flight-replay-frame-status')).toContainText(`frame ${frame === 2 ? 10 : 20}`);
    await expect(page.locator(`#replay-${direction}`)).toBeEnabled();
    const confirmed = () => page.evaluate(() => ({
      image: document.querySelector('#flight-replay-image').getAttribute('src'),
      boxes: document.querySelector('#flight-replay-boxes').innerHTML,
      mode: document.querySelector('#flight-replay-mode').textContent,
      status: document.querySelector('#flight-replay-frame-status').textContent,
      time: document.querySelector('#replay-time').textContent,
      cursor: document.querySelector('#replay-cursor').value,
      observationImage: document.querySelector('#camera-image').getAttribute('src'),
      observationBoxes: document.querySelector('#vision-layer').innerHTML,
      observationMode: document.querySelector('#flight-mode').textContent,
      measurementsMode: document.querySelector('#history-mode').textContent,
    }));
    const before = await confirmed();
    await page.evaluate(() => {
      window.stepBlanks = [];
      window.stepObserver = new MutationObserver(() => {
        const image = document.querySelector('#flight-replay-image');
        if (image.hidden || !image.getAttribute('src') || !document.querySelector('#flight-replay-placeholder').hidden) window.stepBlanks.push(true);
      });
      window.stepObserver.observe(document.querySelector('#flight-replay-camera'), { subtree: true, attributes: true, childList: true });
    });
    const snapshot = { when: (pathname, query) => pathname.endsWith('/replay') && Number(query.get('at')) === target };
    mock.hold = snapshot;
    await page.locator(`#replay-${direction}`).click();
    await expect.poll(() => snapshot.requested).toBe(true);
    await expect(page.locator('#flight-replay-image')).toBeVisible();
    await expect(page.locator('#flight-replay-placeholder')).toBeHidden();
    expect(await confirmed()).toEqual(before);
    const jpeg = { when: pathname => pathname.endsWith(`/frames/${frame}.jpg`) }; mock.hold = jpeg;
    snapshot.release();
    await expect.poll(() => jpeg.requested).toBe(true);
    await expect(page.locator('#flight-replay-image')).toBeVisible();
    await expect(page.locator('#flight-replay-placeholder')).toBeHidden();
    expect(await confirmed()).toEqual(before);
    jpeg.release();
    await expect(page.locator('#flight-replay-frame-status')).toContainText(`frame ${frame * 10}`);
    await expect(page.locator('#replay-cursor')).toHaveValue(String(target));
    await expect(page.locator('#flight-replay-mode')).toHaveText(frame === 2 ? 'AltHold' : 'Stabilize');
    await expect(page.locator('#history-mode')).toHaveText(frame === 2 ? 'ALT_HOLD' : 'STABILIZE');
    await expect(page.locator('#flight-mode')).toHaveText(frame === 2 ? 'ALT_HOLD' : 'STABILIZE');
    await expect(page.locator('.flight-replay-box')).toHaveCount(frame === 2 ? 1 : 0);
    await expect(page.locator('.vision-box')).toHaveCount(frame === 2 ? 1 : 0);
    const pixel = await page.locator('#flight-replay-image').evaluate(image => {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
      const context = canvas.getContext('2d'); context.drawImage(image, 0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data];
    });
    expect(pixel[frame === 2 ? 2 : 0]).toBeGreaterThan(pixel[frame === 2 ? 0 : 2] * 2);
    expect(await page.locator('#camera-image').getAttribute('src')).toBe(await page.locator('#flight-replay-image').getAttribute('src'));
    expect(await page.evaluate(() => { window.stepObserver.disconnect(); return window.stepBlanks; })).toEqual([]);
    readOnly(mock);
  });
}

test('a delayed hosted next image cannot overwrite a newer slider seek', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 1.3);
  await page.locator('#view-sessions').click();
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('#replay-next')).toBeEnabled();
  const hold = { when: pathname => pathname.endsWith('/frames/2.jpg') }; mock.hold = hold;
  await page.locator('#replay-next').click();
  await expect.poll(() => hold.requested).toBe(true);
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('#replay-cursor')).toHaveValue('1.3');
  await seek(page, .5);
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
  hold.release();
  await page.waitForTimeout(150);
  await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
  await expect(page.locator('#flight-replay-mode')).toHaveText('Stabilize');
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('.flight-replay-box')).toHaveCount(0);
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expectRed(page, '#flight-replay-image');
  readOnly(mock);
});

test('a failed hosted next image clears the retained frame and retries the requested time', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 1.3);
  await page.locator('#view-sessions').click();
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('#replay-next')).toBeEnabled();
  mock.respond = pathname => pathname.endsWith('/frames/2.jpg') ? { status: 503, body: 'Image unavailable' } : null;
  await page.locator('#replay-next').click();
  await expect(page.locator('#replay-cursor-status')).toContainText('Replay unavailable');
  await expect(page.locator('#flight-replay-image')).toBeHidden();
  await expect(page.locator('#flight-replay-placeholder')).toBeVisible();
  await expect(page.locator('.flight-replay-box')).toHaveCount(0);
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#flight-mode')).toHaveText('—');
  await expect(page.locator('#replay-cursor')).toHaveValue('1.5');
  mock.respond = null;
  await page.locator('#demo-retry').click();
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await expect(page.locator('#replay-cursor')).toHaveValue('1.5');
  await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 20');
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('#replay-play')).toHaveText('Play');
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
  await expect(page.locator('#replay-cursor-status')).toContainText('Playing');
  await page.locator('.live-freeze').click();
  await expect(page.locator('#replay-play')).toHaveText('Play');
  await expect(page.locator('#replay-cursor-status')).toContainText(/paused/i);
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

test('an initial archive metadata failure has a working visible retry without opening live sources', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  mock.respond = pathname => pathname === `/api/recordings/${ID}` ? { status: 503, json: { detail: 'Recording temporarily unavailable' } } : null;
  await page.goto('/demo');
  await expect(page.locator('#demo-load-status')).toHaveText('Recording temporarily unavailable');
  await expect(page.locator('#demo-retry')).toBeVisible();
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#flight-mode')).toHaveText('—');
  mock.respond = null;
  await page.locator('#demo-retry').click();
  await expect(page.locator('#demo-retry')).toBeHidden();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('#replay-cursor')).toHaveValue('0');
  readOnly(mock);
});

for (const failure of ['visual unavailable', 'corrupt JPEG', 'null visual', 'null telemetry']) {
  test(`playback fails visibly for ${failure}, clears observations and retries the same cursor`, async ({ page, model }) => {
    const mock = await installRecorded(page, model);
    await openDemo(page); await seek(page, 2);
    await expect(page.locator('.vision-box')).toHaveCount(1);
    mock.respond = pathname => {
      if (failure === 'visual unavailable' && pathname.endsWith('/visual')) return { status: 503, json: { detail: 'Images temporarily unavailable' } };
      if (failure === 'corrupt JPEG' && pathname.endsWith('.jpg')) return { contentType: 'image/jpeg', body: Buffer.from('Invalid JPEG') };
      if (failure === 'null visual' && pathname.endsWith('/visual') || failure === 'null telemetry' && pathname.endsWith('/replay')) return { contentType: 'application/json', body: 'null' };
      return null;
    };
    await page.locator('#replay-play').click();
    await expect(page.locator('#demo-retry')).toBeVisible();
    await expect(page.locator('#demo-load-status')).toBeVisible();
    await expect(page.locator('#camera-image')).toBeHidden();
    await expect(page.locator('#flight-mode')).toHaveText('—');
    await expect(page.locator('.vision-box')).toHaveCount(0);
    await expect(page.locator('#replay-play')).toHaveText('Play');
    const failedAt = await page.locator('#replay-cursor').inputValue();
    await page.locator('#view-control').click();
    await expect(page.locator('#control-armed')).toContainText('unavailable');
    await expect(page.locator('#control-claim')).toBeDisabled();
    mock.respond = null;
    await page.locator('#demo-retry').click();
    await expect(page.locator('#demo-retry')).toBeHidden();
    await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
    await expect(page.locator('#replay-cursor')).toHaveValue(failedAt);
    await expect(page.locator('#replay-play')).toHaveText('Play');
    await page.locator('#view-observation').click();
    await expect(page.locator('#camera-image')).toBeVisible();
    await expect(page.locator('.vision-box')).toHaveCount(1);
    readOnly(mock);
  });
}

test('recorded messages never expose a partial failed archive and retry all original pages', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  mock.metadata.events = 101;
  mock.metadata.sources[0].events = 101;
  mock.messageItems = Array.from({ length: 101 }, (_, offset) => ({ index: offset + 1, at_s: offset === 100 ? 1.5 : 0,
    type_name: offset === 100 ? 'ATTITUDE' : 'HEARTBEAT', message_id: offset === 100 ? 30 : 0,
    fields: offset === 100 ? { roll: .2 } : { custom_mode: 0 }, system: 1, component: 1, sequence: offset % 256,
    received_at: 100 + (offset === 100 ? 1.5 : 0), frame_bytes: 3, wire_version: 2, frame_hex: 'fd00a0' }));
  mock.respond = (pathname, query) => pathname.endsWith('/messages') && query.get('offset') === '100'
    ? { status: 503, json: { detail: 'Second archive page unavailable' } } : null;
  await openDemo(page); await seek(page, 2);
  await page.locator('#view-messages').click();
  await expect(page.locator('#live-recorded-retry')).toBeVisible();
  await expect(page.locator('.live-type')).toHaveCount(0);
  await expect(page.locator('.live-fields')).toBeEmpty();
  await expect(page.locator('.live-hex')).toBeEmpty();
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  mock.respond = null;
  await page.locator('#live-recorded-retry').click();
  await expect(page.locator('#live-recorded-retry')).toBeHidden();
  await expect(page.locator('.live-type')).toHaveCount(2);
  await page.locator('.live-type').filter({ hasText: 'ATTITUDE' }).click();
  await expect(page.locator('.live-fields')).toContainText('roll');
  expect(mock.calls.filter(call => call.path.endsWith('/messages')).map(call => call.query.offset)).toEqual(['0', '100', '0', '100']);
  readOnly(mock);
});

test('the recording ends paused and Play starts it again without changing its outcome', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 2.8);
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await page.locator('#replay-speed').selectOption('4');
  await page.locator('#replay-play').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('3');
  await expect(page.locator('#replay-play')).toHaveText('Play');
  await expect(page.locator('#replay-next')).toBeDisabled();
  await expect(page.locator('#replay-cursor-status')).toContainText(/paused/i);
  await page.locator('#replay-speed').selectOption('1');
  await page.locator('#replay-play').click();
  await expect(page.locator('#replay-play')).toHaveText('Pause');
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await page.locator('#replay-play').click();
  const restartedAt = Number(await page.locator('#replay-cursor').inputValue());
  expect(restartedAt).toBeLessThan(1.5);
  await expectRed(page);
  readOnly(mock);
});

for (const capture of ['video only', 'telemetry only', 'neither source']) {
  test(`a ${capture} archive reports missing data explicitly without a live fallback`, async ({ page, model }) => {
    const mock = await installRecorded(page, model);
    if (capture !== 'telemetry only') { mock.metadata.sources = []; mock.metadata.events = 0; }
    if (capture !== 'video only') mock.metadata.visual = { state: 'missing' };
    await page.goto('/demo');
    await expect(page.locator('#session-status')).toContainText('Recorded flight');
    await expect(page.locator('#session-status')).not.toContainText('Loading');
    await expect(page.locator('#demo-retry')).toBeHidden();
    if (capture === 'video only') {
      await expect(page.locator('#camera-image')).toBeVisible();
      await expect(page.locator('#flight-mode')).toHaveText('—');
      await expect(page.locator('#telemetry-status')).toHaveText('No recorded telemetry component');
      await seek(page, 2);
      await expect(page.locator('.vision-box')).toHaveCount(1);
      expect(mock.calls.some(call => call.path.endsWith('/replay'))).toBe(false);
    } else {
      await expect(page.locator('#camera-image')).toBeHidden();
      await expect(page.locator('#video-status')).toHaveText('No image recorded at this cursor');
      expect(mock.calls.some(call => call.path.endsWith('/visual'))).toBe(false);
      if (capture === 'telemetry only') {
        await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
        await seek(page, 2);
        await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
      } else {
        await expect(page.locator('#flight-mode')).toHaveText('—');
        await expect(page.locator('#replay-play')).toBeDisabled();
        await expect(page.locator('#replay-cursor')).toBeDisabled();
        await page.locator('#view-messages').click();
        await expect(page.locator('.live-empty')).toContainText('No messages');
        await expect(page.locator('.live-freeze')).toBeDisabled();
      }
    }
    readOnly(mock);
  });
}

test('retry after the archive changes reloads its revision and restores the selected moment', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 2);
  await expect(page.locator('.vision-box')).toHaveCount(1);
  const nextRevision = 'd'.repeat(64);
  mock.respond = (pathname, query) => (pathname.endsWith('/replay') || pathname.endsWith('/visual')) && query.get('revision') === REVISION
    ? { status: 409, json: { detail: 'The file changed; reopen the recording' } } : null;
  mock.metadata.revision = nextRevision;
  mock.metadata.download_url = `/api/recordings/${ID}/download?revision=${nextRevision}`;
  await seek(page, 2.5);
  await expect(page.locator('#demo-retry')).toBeVisible();
  await expect(page.locator('#demo-load-status')).toContainText('file changed');
  await page.locator('#demo-retry').click();
  await expect(page.locator('#demo-retry')).toBeHidden();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('#replay-cursor')).toHaveValue('2.5');
  expect(mock.calls.filter(call => call.path === `/api/recordings/${ID}`)).toHaveLength(2);
  expect(mock.calls.filter(call => call.path.endsWith('/replay')).at(-1).query.revision).toBe(nextRevision);
  readOnly(mock);
});

test('the recorded camera and matching detection remain visible on a short laptop display', async ({ page, model }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 2);
  await expect(page.locator('.vision-box')).toHaveCount(1);
  const image = await page.locator('#camera-image').boundingBox();
  expect(image.height).toBeGreaterThanOrEqual(90);
  const layer = await page.locator('#vision-layer').boundingBox();
  const box = await page.locator('.vision-box').boundingBox();
  expect(layer.height).toBeGreaterThan(0);
  expect(box.x).toBeGreaterThanOrEqual(layer.x);
  expect(box.y).toBeGreaterThanOrEqual(layer.y);
  expect(box.x + box.width).toBeLessThanOrEqual(layer.x + layer.width + 1);
  expect(box.y + box.height).toBeLessThanOrEqual(layer.y + layer.height + 1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.waitForTimeout(100);
  expect(await page.locator('#camera-image').boundingBox()).toEqual(image);
  readOnly(mock);
});

test('framing report inspection seeks the hosted replay and preserves original download bindings', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  const context = { profile: 'full', range_response: 'normal', target_id: 7, reference_height: .3 };
  const report = { state: 'complete', id: ID, revision: REVISION, visual_revision: VISUAL_REVISION, duration_s: 3,
    coverage: { sampled_s: 3, unknown_s: 0, metric_s: 0, samples: 3, metric_samples: 0 },
    durations_s: { manual: 1.5, full: 1.5, pilot_throttle: 0, unknown_profile: 0, paused: 0, takeover: 0, inactive: 0, unknown: 0 },
    intervals: [{ start_s: 0, end_s: 1.5, state: 'manual', profile: null, range_response: null, target_id: null, reference_height: null, reason: '' },
      { start_s: 1.5, end_s: 3, state: 'full', ...context, reason: '' }],
    series: [], intervals_count: 2, intervals_truncated: false, series_count: 0, series_downsampled: false,
    events: [{ at_s: 1.5, index: 1, kind: 'framing', detail: 'Engage framing requested', status: 'accepted' }],
    events_count: 1, events_truncated: false, interruptions_count: 0,
    limits: { series: 1200, intervals: 1500, events: 500, sample_age_s: .35, video_age_s: 1 } };
  mock.respond = pathname => pathname.endsWith('/framing-report') ? { json: report } : null;
  await openDemo(page);
  await page.locator('#view-sessions').click();
  expect(mock.calls.some(call => call.path.endsWith('/framing-report'))).toBe(false);
  await page.locator('#framing-report>summary').click();
  await expect(page.locator('.framing-report-results')).toBeVisible();
  await page.locator('#framing-report-time').fill('2');
  await page.getByRole('button', { name: 'View this moment in replay' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('.flight-replay-box')).toHaveCount(1);
  await page.locator('#view-observation').click();
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#archive-download')).toHaveAttribute('href', `/api/recordings/${ID}/download?revision=${REVISION}`);
  await expect(page.locator('#archive-download')).toHaveAttribute('download', '');
  await expect(page.locator('#archive-visual-download')).toHaveAttribute('href', `/api/recordings/${ID}/visual/download?revision=${REVISION}&visual_revision=${VISUAL_REVISION}`);
  await expect(page.locator('#archive-visual-download')).toHaveAttribute('download', '');
  readOnly(mock);
});

test('changing speed while an old playback response is pending never rewinds the displayed cursor', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, .5);
  await expect(page.locator('#replay-play')).toBeEnabled();
  await page.locator('#replay-speed').selectOption('4');
  const hold = { when: (pathname, query) => pathname.endsWith('/visual') && Number(query.get('at')) > .5 };
  mock.hold = hold;
  await page.locator('#replay-play').click();
  await expect.poll(() => hold.requested).toBe(true);
  await page.evaluate(() => {
    window.cursorSamples = [];
    window.cursorSampler = setInterval(() => window.cursorSamples.push(Number(document.querySelector('#replay-cursor').value)), 10);
  });
  await page.locator('#replay-speed').selectOption('0.5');
  hold.release();
  await page.waitForTimeout(500);
  await page.locator('#replay-play').click();
  const samples = await page.evaluate(() => { clearInterval(window.cursorSampler); return window.cursorSamples; });
  expect(samples.length).toBeGreaterThan(10);
  for (let i = 1; i < samples.length; i++) expect(samples[i], 'Playback time must not move backwards without seeking').toBeGreaterThanOrEqual(samples[i - 1]);
  await expect(page.locator('#replay-play')).toHaveText('Play');
  readOnly(mock);
});

test('recorded message conflicts can reload the revised archive without resetting the visitor cursor', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page); await seek(page, 2);
  await expect(page.locator('#flight-mode')).toHaveText('ALT_HOLD');
  const nextRevision = 'd'.repeat(64);
  mock.metadata.revision = nextRevision;
  mock.metadata.download_url = `/api/recordings/${ID}/download?revision=${nextRevision}`;
  mock.respond = (pathname, query) => pathname.endsWith('/messages') && query.get('revision') === REVISION
    ? { status: 409, json: { detail: 'Archive revision changed' } } : null;
  await page.locator('#view-messages').click();
  await expect(page.locator('#live-recorded-retry')).toBeVisible();
  await expect(page.locator('.live-type')).toHaveCount(0);
  await page.locator('#live-recorded-retry').click();
  await expect(page.locator('#live-recorded-retry')).toBeHidden();
  await expect(page.locator('.live-type')).toHaveCount(2);
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  expect(mock.calls.filter(call => call.path.endsWith('/messages')).at(-1).query.revision).toBe(nextRevision);
  expect(mock.calls.filter(call => call.path === `/api/recordings/${ID}`)).toHaveLength(2);
  readOnly(mock);
});

test('hiding during a newly selected archive load preserves that selection on return', async ({ page, model }) => {
  const mock = await installRecorded(page, model);
  await openDemo(page);
  const second = 'e'.repeat(32);
  mock.metadata.id = second;
  mock.metadata.visual.id = second;
  mock.metadata.download_url = `/api/recordings/${second}/download?revision=${REVISION}`;
  await page.locator('#view-sessions').click();
  const hold = { when: pathname => pathname === `/api/recordings/${second}` };
  mock.hold = hold;
  await page.locator(`#archive-list button[data-id="${second}"]`).click();
  await expect.poll(() => hold.requested).toBe(true);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  hold.release();
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-title')).toContainText(second.slice(0, 8));
  await expect(page.locator(`#archive-list button[data-id="${second}"]`)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  expect(mock.calls.filter(call => /^\/api\/recordings\/[a-f0-9]{32}$/.test(call.path)).map(call => call.path)).toEqual([
    `/api/recordings/${ID}`, `/api/recordings/${second}`, `/api/recordings/${second}`,
  ]);
  readOnly(mock);
});
