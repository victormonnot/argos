const { test, expect, open } = require('./fixtures.cjs');

test.use({ hasTouch: true });

async function installVision(page, model) {
  const images = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const context = canvas.getContext('2d');
    const image = color => { context.fillStyle = color; context.fillRect(0, 0, 640, 360); return canvas.toDataURL('image/jpeg').split(',')[1]; };
    return { raw: image('#aa3333'), vision: image('#3355bb') };
  });
  const mock = { calls: [], video: 'video-vision-1', state: 'recent', configured: true, modelName: 'YOLOX-Tiny', ageLimit: 1, stateAge: 0,
    rawSequence: 100, visionSequence: 1, freeze: false, capturedAt: model.clock() - .03, hold: null, malformed: null,
    result: { width: 640, height: 360, inference_ms: 37.4, detections: [{ track_id: 7, box: [.25, .2, .2, .6], confidence: .93 }] } };
  model.modifyState = value => {
    Object.assign(value.video, { source: 'gazebo', source_id: mock.video, state: 'recent', label: 'Test camera', endpoint: '/test/image',
      sequence: mock.rawSequence, received_at: value.at - .01, rx_age_s: .01, width: 640, height: 360, age_limit_s: 10 });
    value.vision = { configured: mock.configured, state: mock.state, detail: '', model: mock.modelName, max_hz: 5,
      age_limit_s: mock.ageLimit, frame_age_s: mock.stateAge, inference_ms: 37.4, processed: 1, tracks: 1 };
  };
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, async route => {
    const isVision = route.request().url().includes('/vision/');
    mock.calls.push(isVision ? 'vision' : 'raw');
    const sequence = isVision ? mock.visionSequence : mock.rawSequence++;
    if (isVision && !mock.freeze) mock.visionSequence += 1;
    const receivedAt = isVision && mock.freeze ? mock.capturedAt : model.clock() - .03;
    const headers = { 'X-Frame-Sequence': String(sequence), 'X-Frame-Received-At': String(receivedAt), 'X-Run-Id': model.run,
      'X-Video-Id': mock.video, ...(isVision ? { 'X-Vision-Result': mock.malformed ?? JSON.stringify(mock.result) } : {}) };
    const body = Buffer.from(images[isVision ? 'vision' : 'raw'], 'base64');
    const hold = mock.hold;
    if (hold && hold.vision === isVision) {
      mock.hold = null; hold.requested = true; model.pending.add(hold);
      await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
    }
    try { await route.fulfill({ contentType: 'image/jpeg', headers, body: mock.decodeError && isVision ? Buffer.from('broken JPEG') : body }); } catch { /* An intentionally delayed request can expire. */ }
  });
  return mock;
}

async function enabled(page, model) {
  const mock = await installVision(page, model);
  await open(page);
  await page.getByRole('checkbox', { name: 'Person detection' }).check();
  await expect(page.locator('.vision-box')).toHaveCount(1);
  return mock;
}

test('detection is optional, off by default, and uses only the paired analyzed frame', async ({ page, model }) => {
  const mock = await installVision(page, model);
  await open(page);
  await expect(page.locator('#camera-image')).toBeVisible();
  expect(mock.calls.every(call => call === 'raw')).toBe(true);
  await expect(page.locator('#vision-toggle')).not.toBeChecked();
  await page.locator('#vision-toggle').check();
  await expect(page.locator('.vision-box-label')).toHaveText('Person #7 · 93%');
  await expect(page.locator('#vision-status')).toContainText('Visual tracking only · 1 person');
  await expect(page.locator('#vision-status')).toContainText('YOLOX-Tiny');
  mock.modelName = 'YOLOX-S';
  await expect(page.locator('#vision-status')).toContainText('YOLOX-S');
  // Raw frame sequence was already >=100; the older analyzed sequence must be
  // accepted after toggling, and its blue JPEG must replace the red raw image.
  const pixel = await page.locator('#camera-image').evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0, 1, 1);
    return [...context.getImageData(0, 0, 1, 1).data];
  });
  expect(pixel[2]).toBeGreaterThan(pixel[0] * 2);
  expect(model.calls.some(call => call.method === 'POST')).toBe(false);
  await page.locator('#vision-toggle').uncheck();
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#camera-image')).toBeVisible();
});

test('an old response cannot restore detection after turning it off', async ({ page, model }) => {
  const mock = await installVision(page, model);
  const hold = { vision: true }; mock.hold = hold;
  await open(page);
  await page.locator('#vision-toggle').check();
  await expect.poll(() => hold.requested).toBe(true);
  await page.locator('#vision-toggle').uncheck();
  hold.release();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#vision-status')).toHaveText('Person detection off');
});

test('a pending raw image cannot overwrite the analyzed image selected later', async ({ page, model }) => {
  const mock = await installVision(page, model);
  const hold = { vision: false }; mock.hold = hold;
  await open(page);
  await expect.poll(() => hold.requested).toBe(true);
  await page.locator('#vision-toggle').check();
  hold.release();
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.locator('#vision-status')).toContainText('Visual tracking only');
});

for (const context of ['run', 'video']) {
  test(`an in-flight result from the previous ${context} cannot restore old boxes`, async ({ page, model }) => {
    const mock = await installVision(page, model);
    const hold = { vision: true }; mock.hold = hold;
    await open(page);
    await page.locator('#vision-toggle').check();
    await expect.poll(() => hold.requested).toBe(true);
    await page.evaluate(() => {
      window.observedTrackIds = [];
      new MutationObserver(() => window.observedTrackIds.push(...Array.from(document.querySelectorAll('.vision-box'), node => node.dataset.trackId)))
        .observe(document.getElementById('vision-layer'), { childList: true });
    });
    if (context === 'run') model.run = 'run-next'; else mock.video = 'video-next';
    mock.result.detections[0].track_id = 12;
    await page.waitForTimeout(220);
    hold.release();
    await expect(page.locator('.vision-box-label')).toHaveText('Person #12 · 93%');
    expect(await page.evaluate(() => window.observedTrackIds)).not.toContain('7');
  });
}

test('watchdog removes a frozen analyzed image and boxes even while state keeps advancing', async ({ page, model }) => {
  const mock = await enabled(page, model);
  mock.freeze = true; mock.capturedAt = model.clock();
  await page.waitForTimeout(160);
  await expect(page.locator('#camera-image')).toBeHidden({ timeout: 2200 });
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  await expect(page.locator('#camera-empty-title')).toContainText('recent analyzed image');
});

for (const failure of ['stale', 'error', 'service']) {
  test(`${failure} clears the analyzed image and raw video recovers independently`, async ({ page, model }) => {
    const mock = await enabled(page, model);
    if (failure === 'service') model.offline = true; else mock.state = failure;
    await expect(page.locator('#camera-image')).toBeHidden({ timeout: 2500 });
    await expect(page.locator('.vision-box')).toHaveCount(0);
    await expect(page.locator('#vision-toggle')).toBeEnabled();
    await page.locator('#vision-toggle').uncheck();
    model.offline = false;
    await expect(page.locator('#camera-image')).toBeVisible();
    await expect(page.locator('#service-status')).toHaveText('Service connected');
  });
}

const invalidResults = [
  ['invalid JSON', '{'],
  ['too large', 'x'.repeat(32769)],
  ['wrong dimensions', result => { result.width = 320; }],
  ['negative box', result => { result.detections[0].box[0] = -.1; }],
  ['outside image', result => { result.detections[0].box[2] = .9; }],
  ['invalid confidence', result => { result.detections[0].confidence = 1.2; }],
  ['invalid identity', result => { result.detections[0].track_id = '<img onerror=alert(1)>'; }],
  ['duplicate identity', result => { result.detections.push(structuredClone(result.detections[0])); }],
  ['too many detections', result => { result.detections = Array.from({ length: 65 }, (_, index) => ({ ...result.detections[0], track_id: index + 1 })); }],
];
for (const [name, corrupt] of invalidResults) {
  test(`rejects ${name} without interrupting the core state`, async ({ page, model }) => {
    const mock = await installVision(page, model);
    if (typeof corrupt === 'string') mock.malformed = corrupt; else corrupt(mock.result);
    await open(page);
    await page.locator('#vision-toggle').check();
    await expect(page.locator('#vision-status')).toContainText('metadata is unavailable');
    await expect(page.locator('#camera-image')).toBeHidden();
    await expect(page.locator('.vision-box')).toHaveCount(0);
    await expect(page.locator('#service-status')).toHaveText('Service connected');
  });
}

test('a decode failure removes previously shown detections', async ({ page, model }) => {
  const mock = await enabled(page, model);
  mock.decodeError = true;
  await expect(page.locator('#vision-status')).toContainText('metadata is unavailable');
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('.vision-box')).toHaveCount(0);
});

test('absent or malformed optional vision never blocks telemetry or the raw image', async ({ page, model }) => {
  const mock = await installVision(page, model);
  const original = model.modifyState;
  model.modifyState = state => { original(state); state.vision.age_limit_s = 'bad'; };
  await open(page);
  await expect(page.locator('#vision-toggle')).toBeDisabled();
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  expect(mock.calls.every(call => call === 'raw')).toBe(true);
});

test('a distant person keeps a complete dark label above the box and inside image edges', async ({ page, model }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await enabled(page, model);
  for (const [x, y] of [[.5, .5], [.98, .01], [.01, .95], [.98, .95]]) {
    mock.result.detections[0].box = [x, y, .015, .04];
    await expect.poll(() => page.locator('.vision-box').evaluate(box => Number.parseFloat(box.style.left))).toBe(x * 100);
    await expect.poll(() => page.evaluate(() => {
      const layer = document.getElementById('vision-layer').getBoundingClientRect();
      const box = document.querySelector('.vision-box').getBoundingClientRect();
      const labelNode = document.querySelector('.vision-box-label'), label = labelNode.getBoundingClientRect();
      return label.width > box.width * 3 && label.x >= layer.x && label.right <= layer.right
        && label.y >= layer.y && label.bottom <= layer.bottom && labelNode.scrollWidth <= labelNode.clientWidth;
    })).toBe(true);
    if (y > .1) expect(await page.evaluate(() => document.querySelector('.vision-box-label').getBoundingClientRect().bottom
      <= document.querySelector('.vision-box').getBoundingClientRect().y)).toBe(true);
  }
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`boxes retain contain-fit alignment and touch controls at ${viewport.width}x${viewport.height}`, async ({ page, model }) => {
    await page.setViewportSize(viewport);
    await enabled(page, model);
    const assertGeometry = async () => {
      // Read both rectangles atomically: a new paired frame replaces the boxes
      // at up to5Hz, so a detached old element is not a geometry failure.
      await expect.poll(() => page.evaluate(() => {
        const node = document.querySelector('.vision-box');
        if (!node || document.getElementById('vision-layer').hidden) return 999;
        const stage = document.getElementById('camera-stage').getBoundingClientRect(), box = node.getBoundingClientRect();
        const scale = Math.min(stage.width / 640, stage.height / 360);
        return Math.max(Math.abs(box.x - stage.x - (stage.width - 640 * scale) / 2 - 160 * scale),
          Math.abs(box.y - stage.y - (stage.height - 360 * scale) / 2 - 72 * scale),
          Math.abs(box.width - 128 * scale), Math.abs(box.height - 216 * scale));
      })).toBeLessThan(1);
    };
    await assertGeometry();
    await page.screenshot({ path: test.info().outputPath('observation.png'), fullPage: true });
    expect((await page.locator('.vision-toggle').boundingBox()).height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('#view-control').click();
    await expect(page.locator('#control-panel')).toBeVisible();
    await assertGeometry();
    await page.setViewportSize({ width: viewport.height, height: viewport.width });
    await page.waitForTimeout(100);
    await assertGeometry();
    await expect(page.locator('#vision-layer')).toHaveCSS('pointer-events', 'none');
  });
}
