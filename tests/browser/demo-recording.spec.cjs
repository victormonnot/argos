const { test, expect, ID, recording, open } = require('./fixtures.cjs');

function filmedRecording(state, overrides = {}) {
  return recording({ state, id: ID, started_at: 100,
    visual: { state: state === 'recording' ? 'recording' : 'complete' },
    filming: { id: ID, state: state === 'recording' ? 'recording' : 'complete',
      writer_stopped: state !== 'recording', camera: { state: state === 'recording' ? 'recording' : 'complete' } },
    ...overrides,
  });
}

async function cameraSource(page, model) {
  const jpeg = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 480;
    return canvas.toDataURL('image/jpeg').split(',')[1];
  });
  const camera = { source: 'device', state: 'recent', age: .02 };
  model.modifyState = state => {
    Object.assign(state.configuration, { environment: 'real', video_source: camera.source,
      video_endpoint: camera.source === 'device' ? '/dev/video2' : '/simulation/camera' });
    Object.assign(state.video, { source: camera.source, state: camera.state, source_id: 'raw-camera',
      received_at: state.at - camera.age, rx_age_s: camera.age, width: 640, height: 480 });
    // Raw camera capture must work without a telemetry link or Pocket reports.
    state.telemetry.state = 'unconfigured';
  };
  let sequence = 0;
  await page.route('**/api/frame.jpg', route => route.fulfill({ contentType: 'image/jpeg',
    headers: { 'X-Frame-Sequence': String(++sequence), 'X-Frame-Received-At': String(model.clock() - .02),
      'X-Run-Id': model.run, 'X-Video-Id': 'raw-camera' }, body: Buffer.from(jpeg, 'base64') }));
  return camera;
}

async function enterDemo(page) {
  await page.locator('#demo-button').click();
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-record-button')).toBeVisible();
}

test('demo records raw video in place, prevents duplicate requests and waits for writer finalization', async ({ page, model }) => {
  await cameraSource(page, model);
  const requests = [];
  let releaseStart;
  await page.route('**/api/recordings/start', async route => {
    requests.push(route.request().postDataJSON());
    await new Promise(resolve => { releaseStart = resolve; });
    model.recording = filmedRecording('recording');
    await route.fulfill({ json: model.recording });
  });
  await page.route('**/api/recordings/stop', async route => {
    requests.push(route.request().postDataJSON());
    model.recording = filmedRecording('complete', {
      visual: { state: 'finalizing' }, filming: { id: ID, state: 'finalizing', writer_stopped: false, camera: { state: 'finalizing' } },
    });
    await route.fulfill({ json: model.recording });
  });
  await open(page);
  await page.locator('#recording-include-visual').evaluate(node => { node.checked = false; });
  await enterDemo(page);
  const button = page.locator('#demo-record-button');
  await button.click();
  await expect(button).toHaveText('Starting…');
  await expect(button).toBeDisabled();
  await expect.poll(() => requests.length).toBe(1);
  await button.dispatchEvent('click');
  expect(requests).toEqual([{ include_visual: true, include_filming: true }]);
  releaseStart();
  await expect(button).toHaveAttribute('aria-label', 'Stop raw video recording');
  await expect(button).toContainText('Stop raw ·');
  await page.screenshot({ path: test.info().outputPath('demo-raw-recording.png'), fullPage: true });
  await expect(button).toBeFocused();
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
  await button.click();
  await expect(button).toHaveText('Finalizing…');
  await expect(button).toBeDisabled();
  expect(requests).toEqual([{ include_visual: true, include_filming: true }, {}]);
  // Finishing the visual archive does not imply that the raw writer is done.
  model.recording.visual.state = 'complete';
  await page.waitForTimeout(150);
  await expect(button).toBeDisabled();
  model.recording = filmedRecording('complete');
  await expect(button).toHaveText('Start recording');
  await expect(button).toBeEnabled();
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
});

test('raw recording requires a recent physical camera and cannot be triggered outside demo', async ({ page, model }) => {
  const camera = await cameraSource(page, model);
  camera.state = 'waiting';
  await open(page);
  const button = page.locator('#demo-record-button');
  await expect(button).toBeHidden();
  await button.dispatchEvent('click');
  await enterDemo(page);
  await expect(button).toBeDisabled();
  await expect(button).toHaveAttribute('title', /recent physical camera/);
  camera.state = 'recent'; camera.source = 'gazebo';
  await page.waitForTimeout(150);
  await expect(button).toBeDisabled();
  camera.source = 'device'; camera.age = 1.1;
  await page.waitForTimeout(150);
  await expect(button).toBeDisabled();
  camera.age = .02;
  await expect(button).toBeEnabled();
  await page.locator('#demo-button').click();
  await button.dispatchEvent('click');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('a rejected raw start stays in demo and exposes the error without claiming a recording', async ({ page, model }) => {
  await cameraSource(page, model);
  await page.route('**/api/recordings/start', route => route.fulfill({ status: 500, json: { detail: 'Camera writer could not open its file.' } }));
  await open(page); await enterDemo(page);
  const button = page.locator('#demo-record-button');
  await button.click();
  await expect(page.locator('#recording-action-status')).toBeVisible();
  await expect(page.locator('#recording-action-status')).toContainText('Camera writer could not open its file.');
  await expect(button).toHaveText('Start recording');
  await expect(button).toBeEnabled();
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
});

test('demo distinguishes a session-only capture from confirmed raw video and can stop it', async ({ page, model }) => {
  await cameraSource(page, model);
  model.recording = recording({ state: 'recording', id: ID, started_at: 100 });
  await open(page); await enterDemo(page);
  const button = page.locator('#demo-record-button');
  await expect(button).toHaveText('Stop capture');
  await expect(button).toHaveAttribute('title', /Raw camera recording is not confirmed/);
  await button.click();
  await expect(button).toHaveText('Start recording');
  expect(model.calls.filter(call => call.method === 'POST').map(call => call.path)).toEqual(['/api/recordings/stop']);
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
});

test('a raw writer error remains stoppable after loss of the camera', async ({ page, model }) => {
  const camera = await cameraSource(page, model);
  model.recording = filmedRecording('recording');
  await open(page); await enterDemo(page);
  const button = page.locator('#demo-record-button');
  await expect(button).toHaveAttribute('data-state', 'recording');
  camera.state = 'error';
  model.recording.filming.state = 'error';
  model.recording.filming.error = 'Disk full';
  model.recording.filming.camera.state = 'error';
  await expect(button).toHaveText('Stop · raw error');
  await expect(button).toHaveAttribute('title', /Disk full/);
  await expect(button).toBeEnabled();
  await button.click();
  await expect(button).toHaveText('Start recording');
  await expect(button).toBeDisabled();
});

test('demo never labels an unconfirmed raw start as raw recording', async ({ page, model }) => {
  await cameraSource(page, model);
  // The old fixture service starts only a journal and returns no filming report.
  await open(page); await enterDemo(page);
  await page.locator('#demo-record-button').click();
  await expect(page.locator('#demo-record-button')).toHaveText('Stop capture');
  await expect(page.locator('#recording-action-status')).toContainText('raw video recording is not confirmed');
});

test('stale service status disables the raw action and does not claim an active recording', async ({ page, model }) => {
  await cameraSource(page, model);
  model.recording = filmedRecording('recording');
  await open(page); await enterDemo(page);
  const button = page.locator('#demo-record-button');
  await expect(button).toHaveAttribute('data-state', 'recording');
  model.offline = true;
  await expect(button).toHaveText('Record unavailable');
  await expect(button).toBeDisabled();
  await button.dispatchEvent('click');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});
