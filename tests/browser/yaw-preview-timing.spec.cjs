const { test, expect, open } = require('./fixtures.cjs');

// Unlike a per-request fresh image, these fixtures represent a real bounded
// inference worker: one immutable camera receipt/result every 200 ms. State
// and JPEG delivery poll independently, and image delivery itself takes time.
async function setupCadence(page, model) {
  const jpeg = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    return canvas.toDataURL('image/jpeg').split(',')[1];
  });
  const mock = { calls: [], maxResultAge: 0, frames: 0, revision: 0, selected: false,
    stoppedDetail: null, imageUnavailable: false };
  const detection = { track_id: 7, confidence: .94, box: [.2, .2, .2, .6] };
  const latest = () => {
    const sequence = Math.floor((model.clock() - 100) / .2);
    return { sequence, receivedAt: 100 + sequence * .2 - .22 };
  };
  const preview = () => {
    const frame = latest(), age = model.clock() - frame.receivedAt;
    mock.maxResultAge = Math.max(mock.maxResultAge, age);
    return { enabled: true, phase: mock.stoppedDetail ? 'stopped' : mock.selected ? 'tracking' : 'idle',
      detail: mock.stoppedDetail || 'Select a person.',
      revision: mock.revision, target_id: mock.selected && !mock.stoppedDetail ? 7 : null,
      run_id: mock.selected ? model.run : null, video_id: mock.selected ? 'real-camera-1' : null,
      frame_sequence: frame.sequence, frame_received_at: frame.receivedAt, frame_age_s: age,
      frame_max_age_s: .45, error_x: mock.selected && !mock.stoppedDetail ? -.4 : null,
      yaw: mock.selected && !mock.stoppedDetail ? -.1 : 0,
      yaw_limit: .125, deadband: .035 };
  };
  model.modifyState = state => {
    const frame = latest();
    state.environment = state.configuration.environment = 'real';
    Object.assign(state.configuration, { video_source: 'device', video_endpoint: '/dev/video2' });
    Object.assign(state.video, { source: 'device', source_id: 'real-camera-1', state: 'recent', endpoint: '/dev/video2',
      label: 'USB camera', sequence: frame.sequence + 1, received_at: state.at - .01, rx_age_s: .01, width: 640, height: 360 });
    state.vision = { configured: true, state: 'recent', detail: '', model: 'YOLOX-Tiny', age_limit_s: 1,
      frame_age_s: state.at - frame.receivedAt, inference_ms: 180, processed: frame.sequence, tracks: 1 };
    state.yaw_preview = preview();
  };
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, async route => {
    if (mock.imageUnavailable) return route.fulfill({ status: 503, json: { detail: 'No recent analyzed image' } });
    const frame = latest(); mock.frames += 1;
    const headers = { 'X-Frame-Sequence': String(frame.sequence), 'X-Frame-Received-At': String(frame.receivedAt),
      'X-Run-Id': model.run, 'X-Video-Id': 'real-camera-1' };
    if (route.request().url().includes('/vision/')) headers['X-Vision-Result'] = JSON.stringify({
      width: 640, height: 360, inference_ms: 180, detections: [detection] });
    await new Promise(resolve => setTimeout(resolve, 60));
    try { await route.fulfill({ contentType: 'image/jpeg', headers, body: Buffer.from(jpeg, 'base64') }); }
    catch { /* Page teardown can cancel the in-flight JPEG. */ }
  });
  await page.route('**/api/vision/yaw-preview', async route => {
    const payload = route.request().postDataJSON(); mock.calls.push(payload);
    model.calls.push({ method: route.request().method(), path: '/api/vision/yaw-preview' });
    mock.revision += 1; mock.selected = payload.action === 'select';
    await route.fulfill({ json: { yaw_preview: preview() } });
  });
  await open(page);
  return mock;
}

async function select(page, mock) {
  await page.locator('#vision-toggle').check();
  await page.getByRole('button', { name: 'Select person #7 for yaw preview' }).click();
  await expect.poll(() => mock.calls.filter(call => call.action === 'select').length).toBe(1);
  await expect(page.locator('#yaw-preview-value')).toHaveText('-10%');
}

test('independent 5 Hz inference and JPEG delivery keep a stable person selection', async ({ page, model }) => {
  const mock = await setupCadence(page, model);
  await select(page, mock);
  await page.waitForTimeout(1600);
  // The latest server result never became stale and the tracker never lost or
  // changed its target. JPEG polling alone must not permanently stop selection.
  expect(mock.frames).toBeGreaterThan(5);
  expect(mock.maxResultAge).toBeLessThan(.45);
  expect(mock.calls.filter(call => call.action === 'clear')).toEqual([]);
  await expect(page.locator('#yaw-preview-value')).toHaveText('-10%');
});

test('a projected proposal expiry pauses its value without discarding a still-tracked person', async ({ page, model }) => {
  const mock = await setupCadence(page, model);
  await select(page, mock);
  const hold = { when: path => path === '/api/state' };
  model.hold = hold;
  await expect.poll(() => hold.requested).toBe(true);
  // The result in the last received snapshot expires locally, while real new
  // analyzed frames keep arriving through the independent image endpoint.
  await page.waitForTimeout(300);
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  await expect(page.locator('#yaw-preview-target')).toHaveText('Person #7');
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selected', 'true');
  expect(mock.calls.filter(call => call.action === 'clear')).toEqual([]);
  hold.release();
  await expect(page.locator('#yaw-preview-value')).toHaveText('-10%');
  expect(mock.calls.filter(call => call.action === 'select')).toHaveLength(1);
  expect(mock.calls.filter(call => call.action === 'clear')).toEqual([]);
});

test('a server stop preserves its reason while later JPEGs are temporarily unavailable', async ({ page, model }) => {
  const mock = await setupCadence(page, model);
  await select(page, mock);
  mock.stoppedDetail = 'The selected person is no longer present in the analyzed image.';
  mock.revision += 1;
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  await expect(page.locator('#yaw-preview-status')).toContainText(mock.stoppedDetail);
  mock.imageUnavailable = true;
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#yaw-preview-status')).toContainText(mock.stoppedDetail);
  mock.imageUnavailable = false;
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.locator('#yaw-preview-status')).toContainText(mock.stoppedDetail);
  await expect(page.locator('#yaw-preview-value')).toHaveText('0%');
  expect(mock.calls.filter(call => call.action === 'clear')).toEqual([]);
  expect(mock.calls.filter(call => call.action === 'select')).toHaveLength(1);
});
