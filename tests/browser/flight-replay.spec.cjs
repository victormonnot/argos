const { test, expect, ID, REVISION, recording, open } = require('./fixtures.cjs');
const VISUAL_REVISION = 'c'.repeat(64);

async function images(page) {
  return page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 160; canvas.height = 90;
    const context = canvas.getContext('2d');
    return ['#b52222', '#2233bb'].map(color => {
      context.fillStyle = color; context.fillRect(0, 0, 160, 90);
      return canvas.toDataURL('image/jpeg').split(',')[1];
    });
  });
}
async function installReplay(page, model, { cameraOnly = false } = {}) {
  const jpeg = await images(page);
  const mock = { calls: [], frameState: 'recent', noFrame: false, hold: null, override: null };
  model.metadata.visual = { state: 'complete', id: ID, revision: VISUAL_REVISION, frames: 2, samples: 3, events: 2, duration_s: 3 };
  if (cameraOnly) { model.metadata.sources = []; model.metadata.events = 0; }
  await page.route(/\/api\/recordings\/[a-f0-9]{32}\/visual(?:[/?]|$)/, async route => {
    const url = new URL(route.request().url()), at = Number(url.searchParams.get('at'));
    const frameRequest = url.pathname.includes('/frames/');
    mock.calls.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method: route.request().method() });
    expect(url.searchParams.get('revision')).toBe(REVISION);
    expect(url.searchParams.get('visual_revision')).toBe(VISUAL_REVISION);
    const index = frameRequest ? Number(url.pathname.match(/\/(\d+)\.jpg$/)[1]) : at >= 1.5 ? 2 : 1;
    const hold = mock.hold;
    if (hold && hold.frame === frameRequest && (frameRequest ? index === hold.index : at === hold.at)) {
      mock.hold = null; hold.requested = true; model.pending.add(hold);
      await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
    }
    if (frameRequest) return route.fulfill({ contentType: 'image/jpeg', body: Buffer.from(jpeg[index - 1], 'base64') }).catch(() => {});
    const value = { id: ID, revision: REVISION, visual_revision: VISUAL_REVISION, at_s: at, state: mock.frameState === 'stale' ? 'stale' : 'recent',
      sample: { at_s: at }, control: { vehicle: { armed: true, mode: index === 2 ? 2 : 0 }, phase: 'armed',
        framing: { active: index === 2, phase: index === 2 ? 'active' : 'idle', target_id: index === 2 ? 7 : null, reference_height: .3 } },
      frame: mock.noFrame ? null : { index, url: `/api/recordings/${ID}/visual/frames/${index}.jpg?revision=${REVISION}&visual_revision=${VISUAL_REVISION}`,
        sequence: index * 10, video_id: 'recorded-camera', at_s: index === 2 ? 1.5 : 0, available_at_s: index === 2 ? 1.5 : 0, age_s: at - (index === 2 ? 1.5 : 0),
        state: mock.frameState, width: 160, height: 90, detections: index === 2 ? [{ track_id: 7, confidence: .9, box: [.25, .2, .2, .6] }] : [] },
      events: [{ at_s: .1, kind: 'claim', detail: 'Pilot requested control', status: 'accepted' },
        { at_s: 1.5, kind: 'framing', detail: 'Engage framing requested', status: 'accepted' }].filter(event => event.at_s <= at) };
    if (mock.override) mock.override(value);
    await route.fulfill({ json: value }).catch(() => {});
  });
  return mock;
}
async function openFlight(page) {
  await open(page);
  await page.locator('#view-sessions').click();
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#recording-view-flight')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#replay-play')).toBeEnabled();
}
async function seek(page, at) {
  await page.locator('#replay-cursor').fill(String(at));
  await page.locator('#replay-cursor').dispatchEvent('input');
}
async function redImage(page) {
  const pixel = await page.locator('#flight-replay-image').evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data];
  });
  expect(pixel[0]).toBeGreaterThan(pixel[2] * 2);
}

test('camera-only flight replay shares the cursor, pairs detections and emits no live control', async ({ page, model }) => {
  const mock = await installReplay(page, model, { cameraOnly: true });
  await openFlight(page);
  await expect(page.locator('#flight-replay-image')).toBeVisible();
  await redImage(page);
  await seek(page, 2);
  await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 20');
  await expect(page.locator('.flight-replay-box')).toHaveCount(1);
  await expect(page.locator('.flight-replay-box')).toContainText('Person #7 · 90%');
  await expect(page.locator('#flight-replay-framing')).toHaveText('Active');
  await expect(page.locator('#flight-replay-events li')).toHaveCount(2);
  await expect(page.locator('#flight-replay-events')).toContainText('Request accepted');
  await expect(page.locator('#archive-visual-download')).toHaveAttribute('href', `/api/recordings/${ID}/visual/download?revision=${REVISION}&visual_revision=${VISUAL_REVISION}`);
  await page.locator('#recording-view-measures').click();
  await expect(page.locator('#replay-no-source')).toBeVisible();
  await page.locator('#recording-view-flight').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 20');
  await page.locator('#flight-replay-events button').first().click();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.1');
  await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 10');
  await expect(page.locator('.flight-replay-box')).toHaveCount(0);
  await page.locator('#replay-play').click();
  await expect.poll(async () => Number(await page.locator('#replay-cursor').inputValue())).toBeGreaterThan(.1);
  await expect(page.locator('#replay-play')).toHaveText('Pause');
  await page.locator('#replay-play').click();
  expect(mock.calls.every(call => call.method === 'GET')).toBe(true);
  expect(model.calls.some(call => call.method === 'POST')).toBe(false);
});

for (const frame of [false, true]) {
  test(`backward seeking rejects an old ${frame ? 'image' : 'snapshot'} response`, async ({ page, model }) => {
    const mock = await installReplay(page, model);
    await openFlight(page);
    const hold = { frame, at: 2, index: 2 }; mock.hold = hold;
    await seek(page, 2);
    await expect.poll(() => hold.requested).toBe(true);
    await expect(page.locator('#flight-replay-image')).toBeHidden();
    await seek(page, .5);
    await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 10');
    hold.release();
    await page.waitForTimeout(200);
    await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
    await expect(page.locator('#flight-replay-mode')).toHaveText('Stabilize');
    await expect(page.locator('.flight-replay-box')).toHaveCount(0);
    await redImage(page);
  });
}

test('missing and stale recorded images clear old images and detections', async ({ page, model }) => {
  const mock = await installReplay(page, model);
  await openFlight(page); await seek(page, 2);
  await expect(page.locator('.flight-replay-box')).toHaveCount(1);
  mock.frameState = 'stale';
  const frames = mock.calls.filter(call => call.path.includes('/frames/')).length;
  await seek(page, 2.5);
  await expect(page.locator('#flight-replay-frame-status')).toContainText('Stale image omitted');
  await expect(page.locator('#flight-replay-image')).toBeHidden();
  await expect(page.locator('.flight-replay-box')).toHaveCount(0);
  expect(mock.calls.filter(call => call.path.includes('/frames/'))).toHaveLength(frames);
  mock.noFrame = true;
  await seek(page, 0);
  await expect(page.locator('#flight-replay-placeholder')).toHaveText('No camera image recorded at this time.');
});

test('recording gaps and ended visual captures label retained control observations as historical', async ({ page, model }) => {
  const mock = await installReplay(page, model);
  await openFlight(page);
  mock.frameState = 'stale';
  mock.override = value => { value.state = 'gap'; value.sample.at_s = 0; };
  await seek(page, 2);
  await expect(page.locator('#flight-replay-control-status')).toContainText('Recording gap · stale observation at the cursor · age 2 s');
  await expect(page.locator('#flight-replay-image')).toBeHidden();
  mock.override = value => { value.state = 'ended'; value.sample.at_s = 1; };
  await seek(page, 2.5);
  await expect(page.locator('#flight-replay-control-status')).toContainText('capture ended · last recorded observation');
  await expect(page.locator('#flight-replay-image')).toBeHidden();
});

test('choosing a telemetry component after Flight replay restores the shared transport', async ({ page, model }) => {
  await installReplay(page, model);
  model.metadata.sources.push({ system: 1, component: 42, events: 1 });
  await openFlight(page); await seek(page, 2);
  await page.locator('#recording-view-measures').click();
  await expect(page.locator('#replay-transport')).toBeHidden();
  await page.locator('#replay-source').selectOption('1:1');
  await expect(page.locator('#replay-transport')).toBeVisible();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-cursor')).toHaveValue('2');
  await expect(page.locator('#history-mode')).toHaveText('STABILIZE');
});

test('reopening a recording cancels its previous pending image and cursor', async ({ page, model }) => {
  const mock = await installReplay(page, model);
  await openFlight(page);
  const hold = { frame: true, index: 2 }; mock.hold = hold;
  await seek(page, 2);
  await expect.poll(() => hold.requested).toBe(true);
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#flight-replay-frame-status')).toContainText('frame 10');
  hold.release();
  await page.waitForTimeout(200);
  await expect(page.locator('#replay-cursor')).toHaveValue('0');
  await redImage(page);
  await expect(page.locator('.flight-replay-box')).toHaveCount(0);
});

test('a mismatched media revision or external frame URL cannot render or issue an image request', async ({ page, model }) => {
  const mock = await installReplay(page, model);
  await openFlight(page);
  mock.override = value => { value.frame.url = 'https://example.com/other.jpg'; };
  await seek(page, 2);
  await expect(page.locator('#replay-status')).toHaveText('Invalid flight replay response.');
  await expect(page.locator('#flight-replay-image')).toBeHidden();
  mock.override = value => { value.visual_revision = 'd'.repeat(64); };
  await seek(page, .5);
  await expect(page.locator('#replay-status')).toHaveText('Invalid flight replay response.');
  mock.override = value => { value.events.push({ at_s: 2, kind: 'action', detail: 'Future event', status: 'accepted' }); };
  await seek(page, 1);
  await expect(page.locator('#replay-status')).toHaveText('Invalid flight replay response.');
  expect(mock.calls.filter(call => call.path.includes('/frames/'))).toHaveLength(1);
});

for (const state of ['missing', 'finalizing', 'invalid']) {
  test(`old or unavailable visual capture is explicit: ${state}`, async ({ page, model }) => {
    model.metadata.visual = { state, detail: state };
    await open(page); await page.locator('#view-sessions').click();
    await page.locator(`#archive-list button[data-id="${ID}"]`).click();
    await expect(page.locator('#replay-play')).toBeEnabled();
    await page.locator('#recording-view-flight').click();
    await expect(page.locator('#flight-replay-placeholder')).toContainText(state === 'missing' ? 'No recorded video' : state === 'finalizing' ? 'still finalizing' : 'could not be verified');
    await expect(page.locator('#replay-transport')).toBeHidden();
    await expect(page.locator('#archive-visual-download')).toBeHidden();
    expect(model.calls.some(call => call.method === 'POST')).toBe(false);
  });
}

test('session recording includes visuals by default and permits a camera without telemetry', async ({ page, model }) => {
  const jpeg = (await images(page))[0];
  const requests = [];
  model.modifyState = value => {
    value.telemetry.state = 'unconfigured';
    Object.assign(value.video, { source: 'gazebo', state: 'recent', label: 'Camera', endpoint: '/camera', source_id: 'camera-only',
      sequence: 1, received_at: value.at - .01, rx_age_s: .01, width: 160, height: 90 });
  };
  await page.route('**/api/frame.jpg', route => route.fulfill({ contentType: 'image/jpeg', body: Buffer.from(jpeg, 'base64'),
    headers: { 'X-Frame-Sequence': '1', 'X-Frame-Received-At': String(model.clock() - .01), 'X-Run-Id': model.run, 'X-Video-Id': 'camera-only' } }));
  await page.route('**/api/recordings/start', async route => {
    requests.push(route.request().postDataJSON());
    model.recording = recording({ state: 'recording', id: ID, started_at: model.clock(), events: 0,
      visual: { state: 'recording', frames: 1, samples: 1, events: 0, dropped: 0, size_bytes: 5000, max_bytes: 10000, detail: '' } });
    await route.fulfill({ json: model.recording });
  });
  await open(page); await page.locator('#global-recording').click();
  await expect(page.locator('#recording-include-visual')).toBeChecked();
  await expect(page.locator('#recording-start')).toBeEnabled();
  await expect(page.locator('#recording-hint')).toContainText('telemetry unavailable');
  await page.locator('#recording-include-visual').uncheck();
  await expect(page.locator('#recording-start')).toBeDisabled();
  await page.locator('#recording-include-visual').check();
  await page.locator('#recording-start').click();
  expect(requests).toEqual([{ include_visual: true }]);
  await expect(page.locator('#recording-include-visual')).toBeDisabled();
  await expect(page.locator('#recording-visual-status')).toContainText('1 image');
  await expect(page.locator('#recording-events')).toHaveText('0');
});

test('telemetry-only capture can be chosen explicitly', async ({ page, model }) => {
  const requests = [];
  await page.route('**/api/recordings/start', async route => {
    requests.push(route.request().postDataJSON());
    await route.fallback();
  });
  await open(page); await page.locator('#global-recording').click();
  await page.locator('#recording-include-visual').uncheck();
  await page.locator('#recording-start').click();
  expect(requests).toEqual([{ include_visual: false }]);
  await expect(page.locator('#global-recording-state')).toHaveText('Capture in progress');
});

for (const viewport of [{ width: 1366, height: 768 }, { width: 768, height: 1024 }, { width: 360, height: 640 }]) {
  test(`flight replay remains readable at ${viewport.width}x${viewport.height}`, async ({ page, model }, testInfo) => {
    await page.setViewportSize(viewport);
    await installReplay(page, model, { cameraOnly: true });
    await openFlight(page); await seek(page, 2);
    await expect(page.locator('.flight-replay-box')).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    const image = await page.locator('#flight-replay-image').boundingBox();
    const box = await page.locator('.flight-replay-box').boundingBox();
    expect(image.width / image.height).toBeCloseTo(160 / 90, 1);
    expect(box.x - image.x).toBeCloseTo(image.width * .25, 0);
    expect(box.width).toBeCloseTo(image.width * .2, 0);
    await page.screenshot({ path: testInfo.outputPath('flight-replay.png'), fullPage: true });
  });
}
