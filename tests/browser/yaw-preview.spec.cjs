const { test, expect, open } = require('./fixtures.cjs');

async function setup(page, model) {
  const jpeg = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const context = canvas.getContext('2d'); context.fillStyle = '#364840'; context.fillRect(0, 0, 640, 360);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  });
  const mock = { video: 'real-camera-1', sequence: 0, receivedAt: model.clock(), freeze: false,
    calls: [], hold: null, previewFrozen: null, override: null, detections: [{ track_id: 7, confidence: .94, box: [.2, .2, .2, .6] }],
    preview: { enabled: true, phase: 'idle', detail: 'Select a person.', revision: 0, target_id: null,
      run_id: null, video_id: null, frame_sequence: null, frame_received_at: null, frame_age_s: null,
      frame_max_age_s: .45, error_x: null, yaw: 0, yaw_limit: .125, deadband: .035 } };
  function preview() {
    const result = structuredClone(mock.override || mock.preview);
    if (result.phase === 'tracking') {
      result.frame_received_at = mock.previewFrozen ?? model.clock() - .02;
      result.frame_age_s = model.clock() - result.frame_received_at;
      result.frame_sequence = mock.sequence;
    }
    return result;
  }
  model.modifyState = state => {
    state.environment = state.configuration.environment = 'real';
    Object.assign(state.configuration, { video_source: 'device', video_endpoint: '/dev/video2' });
    Object.assign(state.video, { source: 'device', source_id: mock.video, state: 'recent', endpoint: '/dev/video2',
      label: 'USB camera', sequence: mock.sequence, received_at: state.at - .01, rx_age_s: .01, width: 640, height: 360 });
    state.vision = { configured: true, state: 'recent', detail: '', model: 'YOLOX-Tiny', age_limit_s: 1,
      frame_age_s: .02, inference_ms: 25, processed: 2, tracks: mock.detections.length };
    state.yaw_preview = preview();
  };
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, async route => {
    if (!mock.freeze) { mock.sequence += 1; mock.receivedAt = model.clock() - .02; }
    const headers = { 'X-Frame-Sequence': String(mock.sequence), 'X-Frame-Received-At': String(mock.receivedAt),
      'X-Run-Id': model.run, 'X-Video-Id': mock.video };
    if (route.request().url().includes('/vision/')) headers['X-Vision-Result'] = JSON.stringify({ width: 640,
      height: 360, inference_ms: 25, detections: mock.detections });
    await route.fulfill({ contentType: 'image/jpeg', headers, body: Buffer.from(jpeg, 'base64') });
  });
  await page.route('**/api/vision/yaw-preview', async route => {
    const payload = route.request().postDataJSON(); mock.calls.push(payload);
    model.calls.push({ method: route.request().method(), path: '/api/vision/yaw-preview' });
    mock.preview.revision += 1;
    if (payload.action === 'select') Object.assign(mock.preview, { phase: 'tracking', target_id: payload.track_id,
      run_id: payload.run_id, video_id: payload.video_id, error_x: -.4, yaw: -.1 });
    else Object.assign(mock.preview, { phase: 'idle', target_id: null, error_x: null, yaw: 0 });
    const response = { yaw_preview: preview() }, hold = mock.hold;
    if (hold && hold.action === payload.action) {
      mock.hold = null; hold.requested = true; model.pending.add(hold);
      await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
    }
    try { await route.fulfill({ json: response }); } catch { /* Pending replies may be abandoned. */ }
  });
  await open(page);
  return mock;
}

async function select(page) {
  await page.locator('#vision-toggle').check();
  await page.getByRole('button', { name: 'Select person #7 for yaw preview' }).click();
  await expect(page.locator('#yaw-preview-value')).toHaveText('-10%');
}

test('real camera preview is opt-in through an exact paired-frame person selection, with no control requests', async ({ page, model }) => {
  const mock = await setup(page, model);
  await expect(page.locator('#yaw-preview')).toBeVisible();
  await expect(page.locator('#vision-toggle')).not.toBeChecked();
  await expect(page.locator('#yaw-preview-status')).toContainText('Enable person detection');
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  expect(mock.calls).toEqual([]);
  await select(page);
  expect(mock.calls[0]).toEqual({ action: 'select', revision: 0, run_id: 'run-1', video_id: 'real-camera-1',
    frame_sequence: expect.any(Number), track_id: 7 });
  expect(mock.calls[0].frame_sequence).toBeGreaterThan(0);
  await expect(page.locator('#yaw-preview-error')).toHaveText('40% left');
  await expect(page.locator('#yaw-preview-target')).toHaveText('Person #7');
  await expect(page.locator('#yaw-preview-status')).toContainText('Physical turn direction is not verified');
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selected', 'true');
  await page.getByRole('button', { name: 'Clear yaw preview' }).click();
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  expect(mock.calls[1]).toEqual({ action: 'clear', run_id: 'run-1', video_id: 'real-camera-1' });
  expect(model.calls.filter(call => call.method === 'POST').map(call => call.path)).toEqual([
    '/api/vision/yaw-preview', '/api/vision/yaw-preview']);
});

test('clear wins over a delayed selection response and subsequently repeated tracking snapshots', async ({ page, model }) => {
  const mock = await setup(page, model), hold = { action: 'select' }; mock.hold = hold;
  await page.locator('#vision-toggle').check();
  await page.getByRole('button', { name: 'Select person #7 for yaw preview' }).click();
  await expect.poll(() => hold.requested).toBe(true);
  const oldState = structuredClone(mock.preview);
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  await page.getByRole('button', { name: 'Clear yaw preview' }).click();
  await expect.poll(() => mock.calls.length).toBe(2);
  hold.release();
  mock.override = oldState;
  await page.waitForTimeout(300);
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  await expect(page.locator('#yaw-preview-target')).toHaveText('No active target');
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selected', 'false');
});

test('positive limits and the centered deadband remain explicit image-space measurements', async ({ page, model }) => {
  const mock = await setup(page, model); await select(page);
  Object.assign(mock.preview, { error_x: .7, yaw: .125 });
  await expect(page.locator('#yaw-preview-error')).toHaveText('70% right');
  await expect(page.locator('#yaw-preview-value')).toHaveText('+12.5%');
  Object.assign(mock.preview, { error_x: .01, yaw: 0 });
  await expect(page.locator('#yaw-preview-error')).toHaveText('Centered');
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  await expect(page.locator('#yaw-preview-target')).toHaveText('Person #7');
});

for (const cause of ['frozen image', 'frozen result', 'service unavailable', 'target missing']) {
  test(`${cause} clears the displayed correction and cannot resume without another selection`, async ({ page, model }) => {
    const mock = await setup(page, model); await select(page);
    if (cause === 'frozen image') mock.freeze = true;
    if (cause === 'frozen result') mock.previewFrozen = model.clock() - 1;
    if (cause === 'service unavailable') model.offline = true;
    if (cause === 'target missing') mock.detections = [];
    await expect(page.locator('#yaw-preview-value')).toHaveText('0%', { timeout: 1800 });
    await expect.poll(() => mock.calls.some(call => call.action === 'clear')).toBe(true);
    mock.freeze = false; mock.previewFrozen = null; model.offline = false;
    mock.detections = [{ track_id: 7, confidence: .94, box: [.2, .2, .2, .6] }];
    await expect(page.locator('.vision-box')).toHaveCount(1);
    await page.waitForTimeout(200);
    await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
    expect(mock.calls.filter(call => call.action === 'select')).toHaveLength(1);
  });
}

for (const action of ['detection off', 'leave observation', 'source change']) {
  test(`${action} invalidates a previously selected preview`, async ({ page, model }) => {
    const mock = await setup(page, model); await select(page);
    if (action === 'detection off') await page.locator('#vision-toggle').uncheck();
    if (action === 'leave observation') await page.locator('#view-control').click();
    if (action === 'source change') mock.video = 'real-camera-2';
    await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
    if (action === 'leave observation') {
      await expect(page.locator('#yaw-preview')).toBeHidden();
      await page.locator('#view-observation').click();
    }
    if (action === 'detection off') await page.locator('#vision-toggle').check();
    await page.waitForTimeout(200);
    await expect(page.locator('#yaw-preview-target')).toHaveText('No active target');
  });
}

test('malformed optional preview is hidden while the independent camera remains usable', async ({ page, model }) => {
  const mock = await setup(page, model);
  mock.preview.yaw = 99;
  await expect(page.locator('#yaw-preview')).toBeHidden();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#service-status')).toHaveText('Service connected');
});

for (const size of [{ width: 1366, height: 650 }, { width: 390, height: 844 }]) {
  test(`preview and touch selection fit at ${size.width}x${size.height}`, async ({ page, model }) => {
    await page.setViewportSize(size);
    await setup(page, model); await select(page);
    await expect(page.locator('#yaw-preview')).toBeVisible();
    const cameraHeight = (await page.locator('#camera-stage').boundingBox()).height;
    expect(cameraHeight).toBeGreaterThanOrEqual(230);
    if (size.width > 760) expect((await page.locator('#yaw-preview').boundingBox()).y
      + (await page.locator('#yaw-preview').boundingBox()).height).toBeLessThanOrEqual(size.height);
    expect((await page.locator('#yaw-preview-clear').boundingBox()).height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('yaw-preview.png'), fullPage: true });
    await page.locator('#focus-button').click();
    await expect(page.locator('#inspector')).toBeHidden();
    expect((await page.locator('#camera-stage').boundingBox()).height).toBeGreaterThanOrEqual(cameraHeight);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  });
}
