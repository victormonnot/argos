const { test, expect, open } = require('./fixtures.cjs');

const AXES = ['roll', 'pitch', 'throttle', 'yaw'];

// Synthetic images and reports stay inside this browser fixture. The production
// console still uses its configured camera and never supplies demonstration data.
async function setupDemoCamera(page, model, { width = 640, height = 480 } = {}) {
  const jpeg = await page.evaluate(({ width, height }) => {
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    const context = canvas.getContext('2d');
    context.fillStyle = '#364840'; context.fillRect(0, 0, width, height);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  }, { width, height });
  const camera = {
    video: 'demo-camera-1', sequence: 0, phase: 'tracking', target: 7,
    detections: [{ track_id: 7, confidence: .94, box: [.2, .2, .2, .6] }],
  };
  model.modifyState = state => {
    state.environment = state.configuration.environment = 'real';
    Object.assign(state.configuration, { video_source: 'device', video_endpoint: '/dev/video2' });
    Object.assign(state.video, {
      source: 'device', source_id: camera.video, state: 'recent', endpoint: '/dev/video2',
      label: 'USB camera', sequence: camera.sequence, received_at: state.at - .02,
      rx_age_s: .02, width, height,
    });
    state.vision = {
      configured: true, state: 'recent', detail: '', model: 'YOLOX-Tiny', age_limit_s: 1,
      frame_age_s: .02, inference_ms: 25, processed: camera.sequence, tracks: camera.detections.length,
    };
    state.yaw_preview = {
      enabled: true, continuous: true, phase: camera.phase, detail: '', revision: 1,
      target_id: camera.target, run_id: model.run, video_id: camera.video, frame_sequence: camera.sequence,
      frame_received_at: state.at - .02, frame_age_s: .02, frame_max_age_s: .45,
      error_x: camera.phase === 'tracking' ? -.4 : null,
      yaw: camera.phase === 'tracking' ? -.1 : 0, yaw_limit: .2, deadband: .035,
      recovery_max_gap_s: 3, recovery_deadline_at: camera.phase === 'paused' ? state.at + 2 : null,
    };
    // An older service can report global activity without providing AP1. That
    // global report must never manufacture an axis state, mode or zero sticks.
    state.yaw_assist = {
      enabled: true, connected: true, status_age_s: .01,
      radio_state: 'A', reason: 'radio reports assistance active', radio_a_to_t_total: 0,
    };
  };
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, async route => {
    camera.sequence += 1;
    const headers = {
      'X-Frame-Sequence': String(camera.sequence), 'X-Frame-Received-At': String(model.clock() - .02),
      'X-Run-Id': model.run, 'X-Video-Id': camera.video,
    };
    if (route.request().url().includes('/vision/')) headers['X-Vision-Result'] = JSON.stringify({
      width, height, inference_ms: 25, detections: camera.detections,
    });
    await route.fulfill({ contentType: 'image/jpeg', headers, body: Buffer.from(jpeg, 'base64') });
  });
  await open(page);
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.locator('#yaw-preview-target')).toHaveText('Person #7');
  return camera;
}

async function enterDemo(page) {
  await page.locator('#demo-button').click();
  await expect(page.locator('body')).toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-button')).toHaveText('Exit demo');
  await expect(page.locator('#demo-button')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#demo-summary')).toBeVisible();
  await expect(page.locator('.demo-shot')).toBeVisible();
}

test('demo shares the live image and leaves missing Pocket reports unavailable', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const stage = await page.locator('#camera-stage').elementHandle();
  await enterDemo(page);
  expect(await stage.evaluate(node => node === document.getElementById('camera-stage'))).toBe(true);
  // The existing player replaces its decoded image for each new frame.
  await expect(page.locator('#camera-stage img')).toHaveCount(1);
  await expect(page.locator('#camera-image')).toBeVisible();
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await expect(page.locator('#demo-target-state')).toHaveText('Tracked');
  await expect(page.locator('#demo-target-card')).toHaveAttribute('data-state', 'tracked');
  await expect(page.locator('.vision-box-label .demo-box-state')).toHaveText('TRACKED');
  await expect(page.locator('.vision-box-label')).toContainText('Person #7 · 94%');
  await expect(page.locator('#demo-mode')).toHaveText('Unavailable');
  for (const axis of AXES) {
    await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Unavailable');
    await expect(page.locator(`#demo-${axis}-stick`)).toHaveText('—');
    await expect(page.locator(`#demo-${axis}-stick`).locator('xpath=..')).toHaveAttribute('aria-label', /unavailable/i);
  }
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('demo entry, target interaction and exit are read-only', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  await expect(page.locator('.vision-hit')).toBeVisible();
  await enterDemo(page);
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selectable', 'false');
  await expect(page.locator('.vision-hit')).toBeHidden();
  await expect(page.locator('#yaw-preview')).toBeHidden();
  await expect(page.locator('#vision-toggle')).toBeHidden();
  await expect(page.locator('#demo-aim')).toBeVisible();
  // A hidden hit target or synthetic click must not bypass the read-only state.
  await page.locator('.vision-box').dispatchEvent('click');
  await page.locator('.vision-hit').dispatchEvent('click');
  await page.waitForTimeout(250);
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await page.locator('#demo-button').click();
  await expect(page.locator('body')).not.toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-button')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#demo-summary')).toBeHidden();
  await expect(page.locator('.demo-shot')).toBeHidden();
  await expect(page.locator('#yaw-preview')).toBeVisible();
  await expect(page.locator('#vision-toggle')).toBeVisible();
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selectable', 'true');
  await expect(page.locator('.vision-hit')).toBeVisible();
  await expect(page.locator('#demo-aim')).toBeHidden();
  await expect(page.locator('#demo-aim')).toHaveCSS('display', 'none');
  await expect(page.locator('.vision-box-label .demo-box-state')).toBeEmpty();
  await expect(page.locator('.vision-box-label')).toHaveText('Person #7 · 94%');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('leaving demo for another workspace or the recording inspector restores the standard display', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  for (const view of ['control', 'messages', 'sessions']) {
    await enterDemo(page);
    await page.locator(`#view-${view}`).click();
    await expect(page.locator('body')).toHaveAttribute('data-view', view);
    await expect(page.locator('body')).not.toHaveClass(/\bdemo-mode\b/);
    await expect(page.locator('#demo-summary')).toBeHidden();
    await expect(page.locator('.demo-shot')).toBeHidden();
    await page.locator('#view-observation').click();
    await expect(page.locator('#demo-button')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('#yaw-preview')).toBeVisible();
  }
  await enterDemo(page);
  await page.locator('#global-recording').click();
  await expect(page.locator('body')).not.toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-summary')).toBeHidden();
  await expect(page.locator('.demo-shot')).toBeHidden();
  await expect(page.locator('#inspector')).toBeVisible();
  await expect(page.locator('#recording-start')).toBeVisible();
  await expect(page.locator('#yaw-preview')).toBeVisible();
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('a temporarily lost target keeps its selected identity with assistance paused', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  await enterDemo(page);
  camera.phase = 'paused'; camera.detections = [];
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await expect(page.locator('#demo-target-state')).toHaveText('Searching');
  await expect(page.locator('#demo-target-card')).toHaveAttribute('data-state', 'searching');
  await expect(page.locator('#demo-aim')).toBeHidden();
  for (const axis of AXES) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Unavailable');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

async function expectDemoFillsDesktop(page, size) {
  // Keep the camera itself centered between balanced instrument panels; the
  // surrounding dashboard and bottom timeline still fill the entire screen.
  await expect.poll(async () => {
    const panel = await page.locator('.camera-panel').boundingBox();
    return Math.abs(panel.x) + Math.abs(panel.width - size.width);
  }).toBeLessThan(3);
  await expect.poll(async () => {
    const history = await page.locator('#demo-history').boundingBox();
    const provenance = await page.locator('.demo-provenance').boundingBox();
    return Math.max(history.y + history.height, provenance.y + provenance.height);
  }).toBeLessThanOrEqual(size.height + 1);
  const video = await page.locator('#camera-stage').boundingBox();
  const controls = await page.locator('#demo-summary').boundingBox();
  const shot = await page.locator('.demo-shot').boundingBox();
  const footer = await page.locator('.camera-footer').boundingBox();
  const history = await page.locator('#demo-history').boundingBox();
  expect(Math.abs(video.x + video.width / 2 - size.width / 2)).toBeLessThan(2);
  expect(Math.abs(controls.width - shot.width)).toBeLessThan(2);
  expect(Math.min(controls.width, shot.width)).toBeGreaterThanOrEqual(279);
  expect(controls.x + controls.width).toBeLessThanOrEqual(video.x + 1);
  expect(video.x + video.width).toBeLessThanOrEqual(shot.x + 1);
  expect(Math.abs(footer.x - video.x)).toBeLessThan(2);
  expect(Math.abs(footer.width - video.width)).toBeLessThan(2);
  expect(footer.y).toBeGreaterThanOrEqual(video.y + video.height - 1);
  expect(history.y).toBeGreaterThanOrEqual(Math.max(controls.y + controls.height, shot.y + shot.height, footer.y + footer.height) - 1);
  expect(Math.abs(history.x - controls.x)).toBeLessThan(2);
  expect(Math.abs(history.x + history.width - shot.x - shot.width)).toBeLessThan(2);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(size.height + 1);
}

for (const size of [{ width: 1366, height: 650 }, { width: 1920, height: 1080 }, { width: 2560, height: 1440 }, { width: 960, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
  test(`demo preserves native video ratio and full-width bottom history at ${size.width}x${size.height}`, async ({ page, model }) => {
    await page.setViewportSize(size);
    await setupDemoCamera(page, model);
    await enterDemo(page);
    if (size.width > 960) await expectDemoFillsDesktop(page, size);
    const video = await page.locator('#camera-stage').boundingBox();
    const controls = await page.locator('#demo-summary').boundingBox();
    const shot = await page.locator('.demo-shot').boundingBox();
    const history = await page.locator('#demo-history').boundingBox();
    expect(video.width / video.height).toBeCloseTo(4 / 3, 2);
    expect(history.y).toBeGreaterThanOrEqual(Math.max(video.y + video.height, controls.y + controls.height, shot.y + shot.height) - 1);
    if (size.width <= 960) {
      expect(controls.y).toBeGreaterThanOrEqual(video.y + video.height - 1);
      expect(shot.y).toBeGreaterThanOrEqual(video.y + video.height - 1);
      if (size.width > 700) {
        expect(Math.abs(controls.y - shot.y)).toBeLessThan(2);
        expect(controls.x + controls.width).toBeLessThanOrEqual(shot.x + 1);
      } else {
        expect(shot.y).toBeGreaterThanOrEqual(controls.y + controls.height - 1);
        expect(Math.abs(controls.width - shot.width)).toBeLessThan(2);
      }
      expect(Math.abs(history.x - video.x)).toBeLessThan(2);
      expect(Math.abs(history.width - video.width)).toBeLessThan(2);
    }
    await expect(page.locator('#camera-image')).toHaveCSS('object-fit', 'contain');
    await expect(page.locator('#demo-message-bar')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('#demo-summary').scrollIntoViewIfNeeded();
    await expect(page.locator('#demo-summary')).toBeInViewport();
    for (const axis of AXES) {
      await page.locator(`.demo-axis[data-axis="${axis}"]`).scrollIntoViewIfNeeded();
      await expect(page.locator(`.demo-axis[data-axis="${axis}"]`)).toBeInViewport();
    }
    await page.locator('#demo-shot-details summary').scrollIntoViewIfNeeded();
    await expect(page.locator('#demo-shot-details summary')).toBeInViewport();
    await page.screenshot({ path: test.info().outputPath('demo-view.png'), fullPage: true });
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

for (const size of [{ width: 1366, height: 650 }, { width: 1920, height: 1080 }, { width: 2560, height: 1440 }]) {
  test(`demo derives the native widescreen ratio and fills the desktop at ${size.width}x${size.height}`, async ({ page, model }) => {
    await page.setViewportSize(size);
    await setupDemoCamera(page, model, { width: 640, height: 360 });
    await enterDemo(page);
    await expectDemoFillsDesktop(page, size);
    await expect.poll(async () => {
      const box = await page.locator('#camera-stage').boundingBox();
      return box.width / box.height;
    }).toBeCloseTo(16 / 9, 2);
    const panel = await page.locator('.camera-panel').boundingBox();
    const history = await page.locator('#demo-history').boundingBox();
    expect(Math.abs(history.width - panel.width)).toBeLessThan(3);
    await expect(page.locator('#camera-image')).toHaveCSS('object-fit', 'contain');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

test('fullscreen demo fills the screen and keeps the complete native camera image', async ({ page, model }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await setupDemoCamera(page, model);
  await enterDemo(page);
  test.skip(!await page.evaluate(() => document.fullscreenEnabled), 'Browser does not support the Fullscreen API');
  await page.locator('#fullscreen-button').click();
  await expect(page.locator('#fullscreen-button')).toHaveAttribute('aria-pressed', 'true');
  expect(await page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);
  const size = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
  await expectDemoFillsDesktop(page, size);
  const video = await page.locator('#camera-stage').boundingBox();
  expect(video.width / video.height).toBeCloseTo(4 / 3, 2);
  await expect(page.locator('#camera-image')).toHaveCSS('object-fit', 'contain');
  await page.locator('#demo-shot-details summary').click();
  await expect(page.locator('#demo-shot-details')).toHaveAttribute('open', '');
  await expectDemoFillsDesktop(page, size);
  await page.evaluate(() => document.exitFullscreen());
  await expect(page.locator('#fullscreen-button')).toHaveAttribute('aria-pressed', 'false');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

function attachPocketReports(model) {
  const cameraState = model.modifyState;
  const reports = { receivedAt: null, age: .02, modify: () => {} };
  model.modifyState = state => {
    cameraState(state);
    const sample = {
      // Pocket reports use host monotonic time; video/state use console-run time.
      schema_version: 1, received_at: reports.receivedAt ?? 1_000_000 + state.at - .02,
      session: '0123abcd', generation: 1, ticket: 22, ack: 18, state: 'A', cause: 'A', mode: 'D',
      yaw_phase: 'A', pitch_phase: 'A', sticks: { roll: 0, pitch: -512, throttle: 1024, yaw: 256 },
      lua_outputs: { yaw: { valid: true, value: 82 }, pitch: { valid: true, value: -20 } },
    };
    Object.assign(state.yaw_assist, {
      session: sample.session, generation: sample.generation, ticket: sample.ticket, ack: sample.ack,
      radio_state: 'A', radio_cause: 'A', radio_mode: 'D', radio_yaw_phase: 'A', radio_pitch_phase: 'A',
      pilot_sample: sample, pilot_sample_count: 20, pilot_sample_age_s: reports.age, pilot_sample_fresh: true,
      distance_preview: { experimental: true, valid: true, reference_height: .5, pitch: -20, reason: 'tracking' },
      assistance: {
        source: 'pocket_lua_report', fresh: true, selected_mode: 'D',
        meaning: 'Last reported Lua output; not native mixer or flight-controller feedback',
        yaw: { state: 'assisted', valid: true, value: 82 },
        pitch: { state: 'assisted', valid: true, value: -20 },
      },
    });
    reports.modify(state.yaw_assist, state);
  };
  return reports;
}

async function expectPocketReady(page) {
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');
  await expect(page.locator('#demo-yaw-state')).toHaveText('Assisted');
  await expect(page.locator('#demo-pitch-state')).toHaveText('Assisted');
}

test('target status and centering guide follow the displayed detection and expire with its evidence', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  const cameraState = model.modifyState;
  let analysisExpired = false;
  model.modifyState = state => {
    cameraState(state);
    // The newer preview intentionally points right while the displayed JPEG
    // initially puts the subject left. Only the paired box may place the guide.
    state.yaw_preview.error_x = .8;
    if (analysisExpired) {
      state.yaw_preview.frame_received_at = state.at - 2;
      state.yaw_preview.frame_age_s = 2;
    }
  };
  await enterDemo(page);
  await expectPocketReady(page);
  const aim = page.locator('#demo-aim');
  await expect(aim).toBeVisible();
  await expect(aim).toHaveAttribute('data-state', 'locked');
  await expect(aim).toHaveAttribute('aria-label', /40% left.*not measured aircraft motion/);
  await expect(page.locator('#demo-target-state')).toHaveText('Locked');
  await expect(page.locator('.vision-box-label .demo-box-state')).toHaveText('LOCKED');
  await expect(page.locator('.vision-box-label')).toContainText('Person #7 · 94%');

  const guideEndpointError = () => page.evaluate(() => {
    const stage = document.getElementById('camera-stage').getBoundingClientRect();
    const box = document.querySelector('.vision-box[data-selected=true]').getBoundingClientRect();
    const centerX = stage.x + stage.width / 2, centerY = stage.y + stage.height / 2;
    return Math.max(...['demo-aim-link', 'demo-aim-shadow'].flatMap(id => {
      const line = document.getElementById(id);
      const point = n => new DOMPoint(Number(line.getAttribute(`x${n}`)), Number(line.getAttribute(`y${n}`))).matrixTransform(line.getScreenCTM());
      const start = point(1), end = point(2);
      return [Math.hypot(start.x - centerX, start.y - centerY),
        Math.hypot(end.x - box.x - box.width / 2, end.y - centerY)];
    }));
  });
  await expect.poll(guideEndpointError).toBeLessThan(1);
  camera.detections[0].box = [.6, .15, .2, .3];
  camera.detections[0].confidence = .86;
  await expect(aim).toHaveAttribute('aria-label', /40% right/);
  await expect(page.locator('.vision-box-label')).toContainText('Person #7 · 86%');
  await expect.poll(guideEndpointError).toBeLessThan(1);

  // Vertical movement at the same horizontal position must not tilt the yaw
  // guide. A horizontally centered subject has no span even above the center.
  camera.detections[0].box = [.6, .65, .2, .2];
  camera.detections[0].confidence = .82;
  await expect(page.locator('.vision-box-label')).toContainText('Person #7 · 82%');
  await expect(aim).toHaveAttribute('aria-label', /40% right/);
  await expect.poll(guideEndpointError).toBeLessThan(1);
  camera.detections[0].box = [.4, .1, .2, .2];
  camera.detections[0].confidence = .81;
  await expect(page.locator('.vision-box-label')).toContainText('Person #7 · 81%');
  await expect(aim).toHaveAttribute('aria-label', /Horizontal image offset: target centered/);
  await expect.poll(guideEndpointError).toBeLessThan(1);

  // Losing only the Pocket report must remove the claim of a control lock,
  // while retaining the independently fresh visual tracking and its guide.
  reports.age = 1;
  await expect(page.locator('#demo-target-state')).toHaveText('Tracked');
  await expect(aim).toHaveAttribute('data-state', 'tracked');
  await expect(aim).toBeVisible();
  await expect(page.locator('.vision-box-label .demo-box-state')).toHaveText('TRACKED');
  reports.age = .02;
  await expect(page.locator('#demo-target-state')).toHaveText('Locked');

  analysisExpired = true;
  await expect(aim).toBeHidden();
  await expect(page.locator('#demo-target-state')).toHaveText('Unavailable');
  analysisExpired = false;
  await expect(aim).toBeVisible();
  // Fresh state responses cannot keep the guide alive once JPEGs stop arriving.
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, route => route.abort());
  await expect(aim).toBeHidden();
  await expect(page.locator('#demo-target-state')).toHaveText('Unavailable');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

async function expectUnavailable(page, ids) {
  for (const id of ids) {
    const field = page.locator(`#demo-${id}`);
    await expect(field).toHaveText(id.endsWith('-stick') ? '—' : 'Unavailable');
    if (id.endsWith('-stick')) await expect(field.locator('xpath=..')).toHaveAttribute('aria-label', /unavailable/i);
  }
}

const ALL_POCKET_FIELDS = ['mode', ...AXES.flatMap(axis => [`${axis}-state`, `${axis}-stick`])];

test('Pocket modes remain separate from per-axis assistance and calibrated pilot sticks', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  for (const [axis, value] of [['roll', '0%'], ['pitch', '-50%'], ['throttle', '+100%'], ['yaw', '+25%']]) {
    await expect(page.locator(`#demo-${axis}-stick`)).toHaveText(value);
  }
  for (const axis of ['roll', 'throttle']) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Manual');

  // Mode D does not imply both axes are assisted: the yaw stick can temporarily
  // give yaw back to the pilot while pitch still reports an assisted output.
  reports.modify = runtime => {
    runtime.radio_yaw_phase = runtime.pilot_sample.yaw_phase = 'M';
    runtime.pilot_sample.lua_outputs.yaw = { valid: false, value: 0 };
    runtime.assistance.yaw = { state: 'manual', valid: false, value: null };
  };
  await expect(page.locator('#demo-yaw-state')).toHaveText('Manual');
  await expect(page.locator('#demo-pitch-state')).toHaveText('Assisted');
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');

  reports.modify = runtime => {
    runtime.radio_mode = runtime.pilot_sample.mode = 'Y';
    runtime.radio_pitch_phase = runtime.pilot_sample.pitch_phase = 'N';
    runtime.pilot_sample.lua_outputs.pitch = { valid: false, value: 0 };
    runtime.assistance.selected_mode = 'Y';
    runtime.assistance.pitch = { state: 'manual', valid: false, value: null };
  };
  await expect(page.locator('#demo-mode')).toHaveText('Yaw assist');
  await expect(page.locator('#demo-yaw-state')).toHaveText('Assisted');
  await expect(page.locator('#demo-pitch-state')).toHaveText('Manual');

  reports.modify = runtime => {
    runtime.radio_mode = runtime.pilot_sample.mode = 'N';
    runtime.radio_state = runtime.pilot_sample.state = 'M';
    runtime.radio_cause = runtime.pilot_sample.cause = 'M';
    runtime.assistance.selected_mode = 'M';
    for (const axis of ['yaw', 'pitch']) {
      runtime[`radio_${axis}_phase`] = runtime.pilot_sample[`${axis}_phase`] = 'N';
      runtime.pilot_sample.lua_outputs[axis] = { valid: false, value: 0 };
      runtime.assistance[axis] = { state: 'manual', valid: false, value: null };
    }
  };
  await expect(page.locator('#demo-mode')).toHaveText('Manual');
  for (const axis of AXES) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Manual');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('small cross-axis stick inputs do not change fresh assisted ownership or its history', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  const sticks = { yaw: 20, pitch: -20 };
  reports.modify = runtime => Object.assign(runtime.pilot_sample.sticks, sticks);
  await enterDemo(page);
  const manualSegments = page.locator('.demo-history-track:not([data-lane="target"]) .demo-segment[data-state="manual"]:not([hidden])');

  // Simulate small unwanted movement on the other direction of each gimbal.
  // Ownership comes from fresh axis reports, not the raw stick's sign or size.
  for (const [yaw, pitch, yawText, pitchText] of [
    [20, -20, '+2%', '-2%'], [-51, 51, '-5%', '+5%'], [102, -102, '+10%', '-10%'],
  ]) {
    Object.assign(sticks, { yaw, pitch });
    await expect(page.locator('#demo-yaw-stick')).toHaveText(yawText);
    await expect(page.locator('#demo-pitch-stick')).toHaveText(pitchText);
    await expectPocketReady(page);
    for (const axis of ['yaw', 'pitch']) {
      await expect(rail(page, axis, 'argos')).toHaveAttribute('data-active', 'true');
      await expect(rail(page, axis, 'pilot')).toHaveAttribute('data-active', 'false');
      await expect(page.locator(`.demo-history-track[data-lane="${axis}"] .demo-segment[data-state="assisted"]:not([hidden])`)).not.toHaveCount(0);
    }
    await expect(page.locator('#demo-history-detail')).toContainText('Yaw ARGOS · Pitch ARGOS');
    await expect(manualSegments).toHaveCount(0);
  }
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('reported manual ownership remains visible after a stick recenters, independently for each axis', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  let manualAxis = 'yaw';
  reports.modify = runtime => {
    Object.assign(runtime.pilot_sample.sticks, { yaw: 1, pitch: -1, [manualAxis]: 0 });
    runtime[`radio_${manualAxis}_phase`] = runtime.pilot_sample[`${manualAxis}_phase`] = 'M';
    runtime.pilot_sample.lua_outputs[manualAxis] = { valid: false, value: 0 };
    runtime.assistance[manualAxis] = { state: 'manual', valid: false, value: null };
  };
  await enterDemo(page);
  for (const axis of ['yaw', 'pitch']) {
    manualAxis = axis;
    const other = axis === 'yaw' ? 'pitch' : 'yaw';
    await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Manual');
    await expect(page.locator(`#demo-${axis}-stick`)).toHaveText('0%');
    await expect(page.locator(`#demo-${axis}-correction`)).toHaveText('—');
    await expect(rail(page, axis, 'pilot')).toHaveAttribute('data-active', 'true');
    await expect(rail(page, axis, 'argos')).toHaveAttribute('data-active', 'false');
    await expect(page.locator(`#demo-${other}-state`)).toHaveText('Assisted');
    await expect(rail(page, other, 'argos')).toHaveAttribute('data-active', 'true');
    const segment = page.locator(`.demo-history-track[data-lane="${axis}"] .demo-segment[data-state="manual"]:not([hidden])`).last();
    await expect(segment).toHaveAttribute('aria-label', /^MANUAL:/);
    await expect(page.locator('#demo-history-detail')).toContainText(axis === 'yaw'
      ? 'Yaw MANUAL · Pitch ARGOS' : 'Yaw ARGOS · Pitch MANUAL');
  }
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('waiting, paused and unknown Lua axes are rendered independently of global active status', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  reports.modify = runtime => {
    runtime.radio_yaw_phase = runtime.pilot_sample.yaw_phase = 'R';
    runtime.pilot_sample.lua_outputs.yaw = { valid: false, value: 0 };
    runtime.assistance.yaw = { state: 'waiting', valid: false, value: null };
    runtime.pilot_sample.lua_outputs.pitch = { valid: false, value: 0 };
    runtime.assistance.pitch = { state: 'paused', valid: false, value: null };
  };
  await enterDemo(page);
  await expect(page.locator('#demo-yaw-state')).toHaveText('Waiting');
  await expect(page.locator('#demo-pitch-state')).toHaveText('Paused');
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');
  reports.modify = runtime => {
    // A newer status has changed the yaw phase after the stored sample.
    runtime.radio_yaw_phase = 'M';
    runtime.assistance.yaw = { state: 'unknown', valid: false, value: null };
  };
  await expect(page.locator('#demo-yaw-state')).toHaveText('Unavailable');
  await expect(page.locator('#demo-pitch-state')).toHaveText('Assisted');
  await expect(page.locator('#camera-image')).toBeVisible();
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

for (const [label, modify, fields] of [
  ['null sample', runtime => { runtime.pilot_sample = null; }, ALL_POCKET_FIELDS],
  ['missing receipt age', runtime => { delete runtime.pilot_sample_age_s; }, ALL_POCKET_FIELDS],
  ['stale sample flag', runtime => { runtime.pilot_sample_fresh = false; }, ALL_POCKET_FIELDS],
  ['expired sample', (runtime, state) => {
    runtime.pilot_sample.received_at = 1_000_000 + state.at - 1;
    runtime.pilot_sample_age_s = 1;
  }, ALL_POCKET_FIELDS],
  ['backend rejects future sample', (runtime, state) => {
    runtime.pilot_sample.received_at = 1_000_000 + state.at + 20;
    runtime.pilot_sample_age_s = null;
    runtime.pilot_sample_fresh = false;
    runtime.assistance.fresh = false;
  }, ALL_POCKET_FIELDS],
  ['absent assistance', runtime => { delete runtime.assistance; }, ['mode', ...AXES.map(axis => `${axis}-state`)]],
  ['unmatched assistance', runtime => { runtime.assistance.fresh = false; }, ['mode', ...AXES.map(axis => `${axis}-state`)]],
  ['wrong report source', runtime => { runtime.assistance.source = 'flight_controller'; }, ['mode', ...AXES.map(axis => `${axis}-state`)]],
  ['malformed mode', runtime => { runtime.assistance.selected_mode = { toString: null }; }, ['mode', ...AXES.map(axis => `${axis}-state`)]],
  ['null yaw state', runtime => { runtime.assistance.yaw = null; }, ['yaw-state']],
  ['malformed yaw enum', runtime => { runtime.assistance.yaw.state = { toString: null }; }, ['yaw-state']],
  ['missing Lua output', runtime => { delete runtime.pilot_sample.lua_outputs.yaw.value; }, ['yaw-state']],
  ['mismatched Lua output', runtime => { runtime.assistance.pitch.value = -21; }, ['pitch-state']],
  ['out-of-range pilot stick', runtime => { runtime.pilot_sample.sticks.roll = 1025; }, ['roll-state', 'roll-stick']],
  ['null pilot stick', runtime => { runtime.pilot_sample.sticks.throttle = null; }, ['throttle-state', 'throttle-stick']],
]) {
  test(`${label} stays unavailable without breaking the independent camera`, async ({ page, model }) => {
    await setupDemoCamera(page, model);
    const reports = attachPocketReports(model);
    await enterDemo(page);
    await expectPocketReady(page);
    reports.modify = modify;
    await expectUnavailable(page, fields);
    await expect(page.locator('#camera-image')).toBeVisible();
    await expect(page.locator('#service-status')).toHaveText('Service connected');
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

test('advancing HTTP snapshots cannot renew a frozen Pocket sample', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const previousCalls = model.calls.filter(call => call.path === '/api/state').length;
  reports.receivedAt = 1_000_000 + model.clock() - .02;
  // Even an incorrectly repeated fresh flag and age must not renew received_at.
  reports.age = .02;
  await expectUnavailable(page, ALL_POCKET_FIELDS);
  expect(model.calls.filter(call => call.path === '/api/state').length).toBeGreaterThan(previousCalls);
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  await expect(page.locator('#camera-image')).toBeVisible();
  reports.receivedAt = null;
  await expectPocketReady(page);
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

for (const failure of ['offline', 'frozen']) {
  test(`${failure} service snapshots clear all Pocket indicators without changing control`, async ({ page, model }) => {
    await setupDemoCamera(page, model);
    attachPocketReports(model);
    await enterDemo(page);
    await expectPocketReady(page);
    model[failure] = true;
    await expectUnavailable(page, ALL_POCKET_FIELDS);
    await expect(page.locator('#service-status')).toHaveText('Service unreachable', { timeout: 5000 });
    await expect(page.locator('#demo-target')).toHaveText('Unavailable');
    await expect(page.locator('#demo-target-state')).toHaveText('Unavailable');
    await expect(page.locator('#demo-aim')).toBeHidden();
    await expect(page.locator('#camera-image')).toBeHidden();
    model[failure] = false;
    await expect(page.locator('#service-status')).toHaveText('Service connected');
    await expectPocketReady(page);
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

test('target loss pauses fresh assisted reports and keeps the remembered identity without a search command', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const person = structuredClone(camera.detections[0]);
  // The paired image can reveal a missing target before the preview status does.
  camera.detections = [];
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await expect(page.locator('#demo-target-state')).toHaveText('Lost');
  await expect(page.locator('#demo-aim')).toBeHidden();
  for (const axis of ['yaw', 'pitch']) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Paused');
  camera.phase = 'paused';
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await expect(page.locator('#demo-target-state')).toHaveText('Searching');
  camera.phase = 'tracking'; camera.detections = [person];
  await expectPocketReady(page);
  await expect(page.locator('#demo-target-state')).toHaveText('Locked');
  await expect(page.locator('#demo-aim')).toBeVisible();
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

function rail(page, axis, kind) {
  return page.locator(`.demo-axis[data-axis="${axis}"] .demo-rail[data-kind="${kind}"]`);
}

test('each pilot and ARGOS bar uses its own signed value and reported source, including valid zero', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  await expect(page.locator('#demo-yaw-correction')).toHaveText('+8%');
  await expect(page.locator('#demo-pitch-correction')).toHaveText('-2%');
  await page.screenshot({ path: test.info().outputPath('demo-fresh-combined.png'), fullPage: true });
  await expect(rail(page, 'yaw', 'pilot')).toHaveAttribute('data-active', 'false');
  await expect(rail(page, 'yaw', 'argos')).toHaveAttribute('data-active', 'true');
  const geometry = async (axis, kind) => rail(page, axis, kind).evaluate(node => {
    const track = node.getBoundingClientRect();
    const marker = node.querySelector('.demo-marker').getBoundingClientRect();
    const fill = node.querySelector('.demo-fill').getBoundingClientRect();
    return { marker: (marker.x + marker.width / 2 - track.x) / track.width, fill: fill.width / track.width };
  });
  expect((await geometry('yaw', 'pilot')).marker).toBeCloseTo(.625, 2);
  expect((await geometry('yaw', 'argos')).marker).toBeCloseTo(.54, 2);
  expect((await geometry('yaw', 'pilot')).fill).toBeCloseTo(.125, 2);
  expect((await geometry('yaw', 'argos')).fill).toBeCloseTo(.04, 2);
  expect((await geometry('pitch', 'pilot')).marker).toBeCloseTo(.25, 2);
  expect((await geometry('pitch', 'argos')).marker).toBeCloseTo(.49, 2);

  reports.modify = runtime => {
    runtime.pilot_sample.sticks.yaw = 0;
    runtime.pilot_sample.lua_outputs.yaw.value = runtime.assistance.yaw.value = 0;
    runtime.pilot_sample.lua_outputs.pitch = { valid: false, value: 0 };
    runtime.assistance.pitch = { state: 'paused', valid: false, value: null };
  };
  await expect(page.locator('#demo-yaw-correction')).toHaveText('0%');
  await expect(page.locator('#demo-yaw-stick')).toHaveText('0%');
  await expect(rail(page, 'yaw', 'argos')).toHaveAttribute('data-active', 'true');
  await expect(rail(page, 'yaw', 'argos')).toHaveAttribute('data-empty', 'false');
  await expect(rail(page, 'yaw', 'argos').locator('.demo-marker')).toBeVisible();
  expect((await geometry('yaw', 'argos')).marker).toBeCloseTo(.5, 2);
  expect((await geometry('yaw', 'argos')).fill).toBe(0);
  await expect(page.locator('#demo-pitch-correction')).toHaveText('—');
  await expect(rail(page, 'pitch', 'argos')).toHaveAttribute('data-empty', 'true');
  await expect(rail(page, 'pitch', 'argos').locator('.demo-marker')).toBeHidden();
  await expect(rail(page, 'pitch', 'argos')).toHaveAttribute('data-active', 'false');
  await expect(rail(page, 'pitch', 'pilot')).toHaveAttribute('data-active', 'false');
  for (const axis of ['roll', 'throttle']) {
    await expect(rail(page, axis, 'pilot')).toHaveAttribute('data-active', 'true');
    await expect(rail(page, axis, 'argos')).toHaveAttribute('data-empty', 'true');
    await expect(page.locator(`#demo-${axis}-correction`)).toHaveText('—');
  }
  reports.modify = runtime => {
    runtime.radio_yaw_phase = runtime.pilot_sample.yaw_phase = 'M';
    runtime.pilot_sample.lua_outputs.yaw = { valid: false, value: 0 };
    runtime.assistance.yaw = { state: 'manual', valid: false, value: null };
  };
  await expect(rail(page, 'yaw', 'pilot')).toHaveAttribute('data-active', 'true');
  await expect(rail(page, 'yaw', 'argos')).toHaveAttribute('data-active', 'false');
  await expect(rail(page, 'pitch', 'argos')).toHaveAttribute('data-active', 'true');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('radar follows paired image position and validated apparent height without inventing range', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const subject = page.locator('#demo-shot-subject');
  const goal = page.locator('#demo-shot-goal');
  await expect(subject).toBeVisible();
  await expect(goal).toBeVisible();
  const circle = node => node.evaluate(element => ({
    x: Number(element.getAttribute('cx')), y: Number(element.getAttribute('cy')), r: Number(element.getAttribute('r')),
  }));
  const initial = await circle(subject), target = await circle(goal);
  expect(initial.x).toBeLessThan(target.x);
  expect(initial.r / target.r).toBeCloseTo(1.2, 2);
  // Preview error is deliberately left at -.4: the paired detection is the
  // source of the drawing, rather than a newer independent control snapshot.
  camera.detections[0].box = [.6, .2, .2, .4];
  await expect.poll(async () => (await circle(subject)).x).toBeGreaterThan(target.x);
  await expect.poll(async () => (await circle(subject)).r / target.r).toBeCloseTo(.8, 2);
  camera.detections[0].box = [.4, .2, .2, .5];
  await expect.poll(async () => (await circle(subject)).x).toBeCloseTo(target.x, 2);
  await expect.poll(async () => (await circle(subject)).r).toBeCloseTo(target.r, 2);
  await page.locator('#demo-shot-details summary').click();
  await expect(page.locator('#demo-centering')).toHaveText('Centered');
  await expect(page.locator('#demo-size')).toContainText('100');
  await expect(page.locator('#demo-shot-details')).toHaveAttribute('open', '');
  reports.modify = runtime => { runtime.distance_preview.valid = false; };
  await expect(goal).toBeHidden();
  await expect(subject).toBeVisible();
  await expect(page.locator('#demo-size')).toHaveText('Unavailable');
  const directionRadius = (await circle(subject)).r;
  camera.detections[0].box = [.3, .2, .2, .6];
  await expect.poll(async () => (await circle(subject)).x).toBeLessThan(target.x);
  expect((await circle(subject)).r).toBe(directionRadius);
  await expect(page.locator('#demo-shot-details')).toHaveAttribute('open', '');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

for (const [label, modify] of [
  ['absent reference', runtime => { delete runtime.distance_preview; }],
  ['zero reference', runtime => { runtime.distance_preview.reference_height = 0; }],
  ['overflowing ratio', runtime => { runtime.distance_preview.reference_height = Number.MIN_VALUE; }],
  ['out-of-image reference', runtime => { runtime.distance_preview.reference_height = 1.2; }],
  ['non-experimental source', runtime => { runtime.distance_preview.experimental = false; }],
  ['yaw-only mode', runtime => { runtime.assistance.selected_mode = 'Y'; }],
]) {
  test(`radar uses only a direction marker with ${label}`, async ({ page, model }) => {
    await setupDemoCamera(page, model);
    const reports = attachPocketReports(model);
    reports.modify = modify;
    await enterDemo(page);
    await expect(page.locator('#demo-shot-subject')).toBeVisible();
    await expect(page.locator('#demo-shot-goal')).toBeHidden();
    await page.locator('#demo-shot-details summary').click();
    await expect(page.locator('#demo-size')).toHaveText('Unavailable');
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

test('a clipped subject keeps image direction while withholding its apparent-size goal', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  attachPocketReports(model);
  await enterDemo(page);
  await expect(page.locator('#demo-shot-goal')).toBeVisible();
  camera.detections[0].box = [0, .2, .2, .6];
  await expect(page.locator('#demo-shot-goal')).toBeHidden();
  await expect(page.locator('#demo-shot-subject')).toBeVisible();
  await page.locator('#demo-shot-details summary').click();
  await expect(page.locator('#demo-size')).toHaveText('Unavailable');
  await expect(page.locator('#demo-centering')).toContainText('left');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('lost radar preserves a static last-seen marker, while unavailable data and context changes clear it', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const subject = page.locator('#demo-shot-subject');
  await expect(subject).toBeVisible();
  const before = await subject.evaluate(node => ['cx', 'cy', 'r'].map(name => node.getAttribute(name)));
  camera.phase = 'paused'; camera.detections = [];
  await expect(page.locator('#demo-radar-tag')).toContainText('Last seen');
  await expect(subject).toBeVisible();
  await expect(page.locator('#demo-shot-goal')).toBeHidden();
  expect(await subject.evaluate(node => ['cx', 'cy', 'r'].map(name => node.getAttribute(name)))).toEqual(before);
  await page.waitForTimeout(400);
  expect(await subject.evaluate(node => ['cx', 'cy', 'r'].map(name => node.getAttribute(name)))).toEqual(before);

  camera.target = 8;
  await expect(page.locator('#demo-target')).toHaveText('Person #8');
  await expect(subject).toBeHidden();
  camera.target = 7; camera.phase = 'tracking';
  camera.detections = [{ track_id: 7, confidence: .94, box: [.2, .2, .2, .6] }];
  await expect(subject).toBeVisible();
  model.offline = true;
  await expect(subject).toBeHidden();
  await expect(page.locator('#demo-shot-goal')).toBeHidden();
  camera.phase = 'paused'; camera.detections = []; model.offline = false;
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  // An unavailable interval must not re-label the old drawing as live evidence.
  await expect(page.locator('#demo-shot-goal')).toBeHidden();
  camera.video = 'replacement-camera';
  await expect(subject).toBeHidden();
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('local history records independent axis changes and unknown intervals without filling pre-entry time', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const segments = (lane, state) => page.locator(`.demo-history-track[data-lane="${lane}"] .demo-segment[data-state="${state}"]:not([hidden])`);
  await expect(segments('yaw', 'assisted')).not.toHaveCount(0);
  await expect(segments('pitch', 'assisted')).not.toHaveCount(0);
  await expect(segments('target', 'visible')).not.toHaveCount(0);
  const inspect = page.locator('#demo-history-inspect');
  await expect(inspect).toHaveAttribute('min', '-30');
  await expect(inspect).toHaveAttribute('max', '0');
  await inspect.evaluate(node => { node.value = '-30'; node.dispatchEvent(new Event('input', { bubbles: true })); });
  await expect(page.locator('#demo-history-detail')).toContainText(/no observation|unavailable|not observed/i);

  reports.modify = runtime => {
    runtime.radio_yaw_phase = runtime.pilot_sample.yaw_phase = 'M';
    runtime.pilot_sample.lua_outputs.yaw = { valid: false, value: 0 };
    runtime.assistance.yaw = { state: 'manual', valid: false, value: null };
  };
  await expect(segments('yaw', 'manual')).not.toHaveCount(0);
  await expect(segments('yaw', 'assisted')).not.toHaveCount(0);
  await expect(segments('pitch', 'manual')).toHaveCount(0);
  await expect(segments('pitch', 'assisted')).not.toHaveCount(0);
  model.offline = true;
  await expect(segments('yaw', 'unknown')).not.toHaveCount(0);
  await expect(segments('pitch', 'unknown')).not.toHaveCount(0);
  model.offline = false; reports.modify = () => {};
  await expectPocketReady(page);
  await inspect.evaluate(node => { node.value = '0'; node.dispatchEvent(new Event('input', { bubbles: true })); });
  await expect(page.locator('#demo-history-detail')).toContainText(/yaw/i);
  await expect(page.locator('#demo-history-detail')).toContainText(/pitch/i);
  const beforePolls = model.calls.filter(call => call.path === '/api/state').length;
  await page.waitForTimeout(1000);
  const addedPolls = model.calls.filter(call => call.path === '/api/state').length - beforePolls;
  expect(addedPolls).toBeGreaterThan(0);
  expect(addedPolls, 'demo uses the existing 100 ms state poll, without a second telemetry poll').toBeLessThanOrEqual(14);
  const stateCalls = model.calls.filter(call => call.path === '/api/state');
  expect(stateCalls.length).toBeGreaterThan(2);
  expect(new Set(model.calls.map(call => call.path))).toEqual(new Set(['/api/state']));
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('history expires observations after its 30-second local window instead of extending the old state across a clock gap', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const assisted = page.locator('.demo-history-track[data-lane="yaw"] .demo-segment[data-state="assisted"]:not([hidden])');
  await expect(assisted).not.toHaveCount(0);
  const count = await page.locator('.demo-segment').count();
  reports.modify = runtime => {
    runtime.assistance.selected_mode = 'M';
    for (const axis of ['yaw', 'pitch']) {
      runtime.pilot_sample.lua_outputs[axis] = { valid: false, value: 0 };
      runtime.assistance[axis] = { state: 'manual', valid: false, value: null };
    }
  };
  // Browser time is the local history's clock. Jumping it while real fixture
  // reports keep their independent monotonic origin also exercises missed time.
  await page.clock.install();
  await page.clock.fastForward(31_000);
  await expect(assisted).toHaveCount(0);
  expect(await page.locator('.demo-segment').count()).toBe(count);
  await page.locator('#demo-history-inspect').evaluate(node => {
    node.value = '-15'; node.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expect(page.locator('#demo-history-detail')).toContainText(/unknown|unavailable|not observed|no observation/i);
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('history and radar memory reset for a new run and camera, with a bounded reusable timeline', async ({ page, model }) => {
  const camera = await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  const yawAssisted = page.locator('.demo-history-track[data-lane="yaw"] .demo-segment[data-state="assisted"]:not([hidden])');
  await expect(yawAssisted).not.toHaveCount(0);
  await expect(page.locator('.demo-history-track')).toHaveCount(3);
  const count = await page.locator('.demo-segment').count();
  expect(count).toBeLessThanOrEqual(3 * 128);
  reports.modify = runtime => {
    runtime.assistance.selected_mode = 'M';
    for (const axis of ['yaw', 'pitch']) {
      runtime.pilot_sample.lua_outputs[axis] = { valid: false, value: 0 };
      runtime.assistance[axis] = { state: 'manual', valid: false, value: null };
    }
  };
  model.run = 'replacement-run';
  await expect(page.locator('#demo-mode')).toHaveText('Manual');
  await expect(yawAssisted).toHaveCount(0);
  await expect(page.locator('#demo-shot-goal')).toBeHidden();
  await expect(page.locator('#demo-shot-subject')).toBeVisible();
  camera.phase = 'paused'; camera.detections = [];
  await expect(page.locator('#demo-radar-tag')).toContainText('Last seen');
  camera.video = 'replacement-video';
  await expect(page.locator('#demo-shot-subject')).toBeHidden();
  await expect(page.locator('.demo-history-track[data-lane="target"] .demo-segment[data-state="visible"]:not([hidden])')).toHaveCount(0);
  expect(await page.locator('.demo-segment').count()).toBe(count);
  await page.locator('#demo-button').click();
  await enterDemo(page);
  await page.locator('#demo-history-inspect').evaluate(node => {
    node.value = '-1'; node.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expect(page.locator('#demo-history-detail')).toContainText(/no observation|unavailable|not observed/i);
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

async function prepareLastReport(page, model) {
  await page.clock.install();
  const camera = await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  reports.receivedAt = 1_000_000 + model.clock() - .02;
  await enterDemo(page);
  await expectPocketReady(page);
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 100));
  return { camera, reports };
}

function expirePocketReport(runtime) {
  runtime.pilot_sample_fresh = false;
  runtime.assistance.fresh = false;
  runtime.assistance.selected_mode = null;
  for (const axis of ['yaw', 'pitch']) runtime.assistance[axis] = { state: 'unknown', valid: false, value: null };
}

async function showLastReport(page, reports) {
  reports.age = .5;
  reports.modify = expirePocketReport;
  await page.clock.runFor(150);
  await expect(page.locator('#demo-summary')).toHaveAttribute('data-held', 'true');
}

test('demo retains a muted last report through backend expiry without extending control history or Locked', async ({ page, model }) => {
  const { reports } = await prepareLastReport(page, model);
  await showLastReport(page, reports);
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');
  await expect(page.locator('#demo-report-hint')).toBeVisible();
  await expect(page.locator('#demo-report-hint')).toHaveText(/Last report · 0\.[5-8] s/);
  await expect(page.locator('#demo-mode-hint')).toBeVisible();
  await expect(page.locator('#demo-yaw-correction')).toHaveText('+8%');
  await expect(page.locator('#demo-yaw-stick')).toHaveText('+25%');
  await expect(page.locator('.demo-rail[data-active=true]')).toHaveCount(0);
  await expect(page.locator('.demo-rail').first()).toHaveCSS('opacity', '0.5');
  await expect(page.locator('#demo-target-state')).toHaveText('Tracked');
  await expect(page.locator('#demo-shot-state')).toHaveText('LAST REPORT');
  await page.clock.runFor(300); // The bounded history samples at 250 ms.
  await expect(page.locator('.demo-history-track[data-lane="yaw"] .demo-segment[data-state="unknown"]:not([hidden])')).not.toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('demo-last-report.png'), fullPage: true });
  reports.age = 1.01;
  await page.clock.runFor(100);
  await expectUnavailable(page, ALL_POCKET_FIELDS);
  await expect(page.locator('#demo-report-hint')).toBeHidden();
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('last report expires locally even when HTTP keeps repeating a fresh age and receipt', async ({ page, model }) => {
  const { reports } = await prepareLastReport(page, model);
  await page.clock.runFor(450);
  await expect(page.locator('#demo-summary')).toHaveAttribute('data-held', 'true');
  await page.clock.runFor(650);
  await expectUnavailable(page, ALL_POCKET_FIELDS);
  // Only a genuinely new receipt can restore a current report.
  reports.receivedAt = null;
  await page.clock.runFor(150);
  await expectPocketReady(page);
  await expect(page.locator('#demo-summary')).toHaveAttribute('data-held', 'false');
});

for (const change of ['disconnected', 'disabled', 'malformed', 'correlation', 'generation', 'session', 'target', 'source', 'run', 'hidden', 'exit']) {
  test(`a ${change} change immediately clears retained Pocket data`, async ({ page, model }) => {
    const { camera, reports } = await prepareLastReport(page, model);
    await showLastReport(page, reports);
    const changes = {
      disconnected: runtime => { runtime.connected = false; },
      disabled: runtime => { runtime.enabled = false; },
      malformed: runtime => { runtime.pilot_sample.sticks.yaw = null; },
      correlation: runtime => { runtime.radio_yaw_phase = 'M'; },
      generation: runtime => { runtime.generation += 1; },
      session: runtime => { runtime.session = 'replacement'; },
      target: () => { camera.target = 8; },
      source: () => { camera.video = 'replacement'; },
      run: () => { model.run = 'replacement'; },
      hidden: () => {}, exit: () => {},
    };
    reports.modify = runtime => { expirePocketReport(runtime); changes[change](runtime); };
    if (change === 'hidden') {
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: true });
        document.dispatchEvent(new Event('visibilitychange'));
      });
    } else if (change === 'exit') {
      await page.locator('#demo-button').click();
      await enterDemo(page);
    }
    await page.clock.runFor(200);
    await expect(page.locator('#demo-summary')).toHaveAttribute('data-held', 'false');
    await expect(page.locator('#demo-mode')).toHaveText('Unavailable');
    await expect(page.locator('#demo-yaw-correction')).toHaveText('—');
    await expect(page.locator('#demo-report-hint')).toBeHidden();
  });
}

for (const next of ['manual', 'waiting', 'paused']) {
  test(`a new ${next} report replaces retained assistance immediately`, async ({ page, model }) => {
    const { reports } = await prepareLastReport(page, model);
    await showLastReport(page, reports);
    reports.age = .02; reports.receivedAt = null;
    reports.modify = runtime => {
      const phase = { manual: 'M', waiting: 'R', paused: 'P' }[next];
      runtime.radio_yaw_phase = runtime.pilot_sample.yaw_phase = phase;
      runtime.pilot_sample.lua_outputs.yaw = { valid: false, value: 0 };
      runtime.assistance.yaw = { state: next, valid: false, value: null };
    };
    await page.clock.runFor(200);
    await expect(page.locator('#demo-summary')).toHaveAttribute('data-held', 'false');
    await expect(page.locator('#demo-yaw-state')).toHaveText({ manual: 'Manual', waiting: 'Waiting', paused: 'Paused' }[next]);
    await expect(page.locator('#demo-yaw-correction')).toHaveText('—');
    await expect(page.locator('#demo-report-hint')).toBeHidden();
  });
}

async function setupSettledTimeline(page, model) {
  await page.clock.install();
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  await enterDemo(page);
  await expectPocketReady(page);
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 100));
  return reports;
}

function setReportedAxis(runtime, axis, state) {
  if (state === 'assisted') return;
  const phase = state === 'manual' ? 'M' : state === 'waiting' ? 'R' : 'A';
  runtime[`radio_${axis}_phase`] = runtime.pilot_sample[`${axis}_phase`] = phase;
  runtime.pilot_sample.lua_outputs[axis] = { valid: false, value: 0 };
  runtime.assistance[axis] = { state, valid: false, value: null };
}

async function inspectTimeline(page, seconds = 0) {
  await page.locator('#demo-history-inspect').evaluate((node, value) => {
    node.value = String(value); node.dispatchEvent(new Event('input', { bubbles: true }));
  }, seconds);
}

const visibleTimeline = (page, lane, state) => page.locator(`.demo-history-track[data-lane="${lane}"] .demo-segment${state ? `[data-state="${state}"]` : ''}:not([hidden])`);

test('brief pause and waiting reports use hatches and a settled caption while Inspect keeps each observed state', async ({ page, model }) => {
  const reports = await setupSettledTimeline(page, model);
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'paused');
  await page.clock.runFor(150);
  await expect(page.locator('#demo-pitch-state')).toHaveText('Paused');
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Paused');
  await expect(page.locator('#demo-pitch-label')).toBeHidden();
  await expect(page.locator('.demo-axis[data-axis="pitch"] .demo-rail[data-active=true]')).toHaveCount(0);
  await inspectTimeline(page);
  await expect(page.locator('#demo-history-detail')).toContainText('Pitch Paused');
  const pauseAt = await page.evaluate(() => performance.now());
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'waiting');
  await page.clock.runFor(200);
  await expect(page.locator('#demo-pitch-label')).toBeHidden();
  await inspectTimeline(page);
  await expect(page.locator('#demo-history-detail')).toContainText('Pitch Waiting');
  const waitingAt = await page.evaluate(() => performance.now());
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'paused');
  await page.clock.runFor(400); // Cross 500 ms plus one 100 ms render tick.
  await expect(page.locator('#demo-pitch-label')).toHaveText('Standby');
  await expect(page.locator('#demo-pitch-label')).toBeVisible();
  const now = await page.evaluate(() => performance.now());
  for (const [at, state] of [[pauseAt, 'Paused'], [waitingAt, 'Waiting']]) {
    await inspectTimeline(page, Math.round((at - now) / 50) * .05);
    await expect(page.locator('#demo-history-detail')).toContainText(`Pitch ${state}`);
  }
  await expect(visibleTimeline(page, 'pitch', 'assisted')).toHaveCount(1);
  await expect(visibleTimeline(page, 'pitch', 'paused')).toHaveCount(0);
  await expect(visibleTimeline(page, 'pitch', 'waiting')).toHaveCount(0);
  await expect(page.locator('.demo-history-track[data-lane="pitch"] .demo-interruption:not([hidden])')).not.toHaveCount(0);
  reports.modify = () => {};
  await page.clock.runFor(150);
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Assisted');
  await page.clock.runFor(600);
  await expect(page.locator('#demo-pitch-label')).toBeHidden();
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Assisted');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

test('brief manual takeover changes live authority immediately, then sustained ownership gets a block and a long label', async ({ page, model }) => {
  const reports = await setupSettledTimeline(page, model);
  await page.clock.runFor(2200);
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'manual');
  await page.clock.runFor(200);
  await expect(page.locator('.demo-axis[data-axis="pitch"] .demo-rail[data-kind="pilot"]')).toHaveAttribute('data-active', 'true');
  await expect(page.locator('.demo-axis[data-axis="pitch"] .demo-rail[data-kind="argos"]')).toHaveAttribute('data-active', 'false');
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Manual');
  await inspectTimeline(page);
  await expect(page.locator('#demo-history-detail')).toContainText('Pitch MANUAL');
  await expect(visibleTimeline(page, 'pitch', 'manual')).toHaveCount(0);
  reports.modify = () => {};
  await page.clock.runFor(300);
  await inspectTimeline(page);
  await expect(visibleTimeline(page, 'pitch', 'manual')).toHaveCount(0);
  await expect(page.locator('.demo-history-track[data-lane="pitch"] .demo-interruption:not([hidden])')).not.toHaveCount(0);
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'manual');
  await page.clock.runFor(700);
  await inspectTimeline(page);
  await expect(visibleTimeline(page, 'pitch', 'manual')).toHaveCount(1);
  await expect(visibleTimeline(page, 'pitch', 'manual')).toHaveText('');
  await page.clock.runFor(1600);
  await inspectTimeline(page);
  await expect(visibleTimeline(page, 'pitch', 'manual')).toHaveText('MANUAL');
});

test('unknown data bypasses caption settling and never becomes hatched assistance', async ({ page, model }) => {
  const reports = await setupSettledTimeline(page, model);
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'waiting');
  await page.clock.runFor(700);
  await expect(page.locator('#demo-pitch-label')).toHaveText('Standby');
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'unknown');
  await page.clock.runFor(150);
  await expect(page.locator('#demo-pitch-label')).toHaveText('Unavailable');
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Unavailable');
  await page.clock.runFor(100); // Give the immediate gap a nonzero screen width.
  await inspectTimeline(page);
  await expect(visibleTimeline(page, 'pitch', 'unknown')).not.toHaveCount(0);
  await expect(visibleTimeline(page, 'pitch').last()).toHaveAttribute('data-state', 'unknown');
  await expect(page.locator('#demo-history-detail')).toContainText('Pitch Unavailable');
  await expect(page.locator('.demo-axis[data-axis="pitch"] .demo-rail[data-active=true]')).toHaveCount(0);
});

test('an established manual span keeps its category as a different-axis observation reaches the 30-second cutoff', async ({ page, model }) => {
  const reports = await setupSettledTimeline(page, model);
  reports.modify = runtime => setReportedAxis(runtime, 'yaw', 'manual');
  // Start the span only after the asynchronous mock response reached the UI.
  // Changing the Node fixture alone does not start a browser observation.
  await page.clock.runFor(150);
  await expect(page.locator('#demo-yaw-detail')).toHaveText('Manual');
  await page.clock.runFor(300);
  reports.modify = runtime => { setReportedAxis(runtime, 'yaw', 'manual'); setReportedAxis(runtime, 'pitch', 'paused'); };
  await page.clock.runFor(100);
  await expect(page.locator('#demo-pitch-detail')).toHaveText('Paused');
  await page.clock.runFor(200);
  await inspectTimeline(page);
  await expect(visibleTimeline(page, 'yaw', 'manual')).toHaveCount(1);
  reports.modify = () => {};
  await page.clock.runFor(100);
  await expect(page.locator('#demo-yaw-detail')).toHaveText('Assisted');
  await page.clock.runFor(29_700);
  await inspectTimeline(page);
  // The retained left edge is inside the manual interval, after its original
  // start was pruned. It must not become an ARGOS-colored short excursion.
  await expect(visibleTimeline(page, 'yaw', 'manual')).toHaveCount(1);
  await expect(page.locator('.demo-segment')).toHaveCount(3 * 128);
  await expect(page.locator('.demo-interruption')).toHaveCount(2 * 128);
});

test('dense interruption history keeps its oldest hatch even when the display pool is full', async ({ page, model }) => {
  test.setTimeout(45_000);
  const reports = await setupSettledTimeline(page, model);
  let count = 0;
  reports.modify = runtime => { if (++count % 2) setReportedAxis(runtime, 'pitch', 'paused'); };
  await page.clock.runFor(29_000);
  await inspectTimeline(page);
  expect(count).toBeGreaterThan(256);
  const hatches = page.locator('.demo-history-track[data-lane="pitch"] .demo-interruption:not([hidden])');
  await expect(hatches).toHaveCount(128);
  await expect(hatches.first()).toHaveAttribute('data-state', 'dense');
  expect(await hatches.first().evaluate(node => parseFloat(node.style.left))).toBeLessThan(5);
  expect(await page.locator('.demo-interruption').count()).toBe(256);
  await expect(page.locator('#demo-history-detail')).toContainText(/Pitch (Paused|ARGOS)/);
});


for (const active of ['yaw', 'pitch']) {
  for (const otherState of ['manual', 'waiting', 'paused', 'unknown']) {
    test(`shot headline leads with assisted ${active} while the other axis is ${otherState}`, async ({ page, model }) => {
      await setupDemoCamera(page, model);
      const reports = attachPocketReports(model);
      const other = active === 'yaw' ? 'pitch' : 'yaw';
      reports.modify = runtime => setReportedAxis(runtime, other, otherState);
      await enterDemo(page);
      await expect(page.locator(`#demo-${active}-state`)).toHaveText('Assisted');
      await expect(page.locator(`#demo-${other}-state`)).toHaveText({ manual: 'Manual', waiting: 'Waiting', paused: 'Paused', unknown: 'Unavailable' }[otherState]);
      await expect(page.locator('#demo-shot-title')).toHaveText(active === 'yaw'
        ? 'Keeping the subject centered.' : 'Keeping the subject the same size.');
      await expect(page.locator('#demo-shot-subtitle')).toHaveText(otherState === 'manual'
        ? active === 'yaw' ? 'Forward / back: pilot control.' : 'Turning: pilot control.'
        : `${active === 'yaw' ? 'Distance' : 'Centering'} assistance: ${otherState === 'unknown' ? 'unavailable' : otherState}.`);
      await expect(page.locator('#demo-shot-state')).toHaveText(otherState === 'manual' ? 'SHARED' : 'ASSISTED');
      await expect(page.locator('#demo-target-state')).toHaveText('Locked');
      expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
    });
  }
}

test('shot reserves overall waiting for cases without an assisted axis', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  reports.modify = runtime => {
    setReportedAxis(runtime, 'yaw', 'waiting');
    setReportedAxis(runtime, 'pitch', 'paused');
  };
  await enterDemo(page);
  await expect(page.locator('#demo-shot-title')).toHaveText('Waiting for assistance.');
  await expect(page.locator('#demo-shot-state')).toHaveText('WAITING');
  reports.modify = runtime => setReportedAxis(runtime, 'yaw', 'waiting');
  await expect(page.locator('#demo-shot-title')).toHaveText('Keeping the subject the same size.');
  reports.modify = runtime => setReportedAxis(runtime, 'pitch', 'waiting');
  await expect(page.locator('#demo-shot-title')).toHaveText('Keeping the subject centered.');
  await expect(page.locator('#demo-shot-subtitle')).toHaveText('Distance assistance: waiting.');
});

test('shot still describes valid zero yaw assistance and distinguishes yaw-only mode', async ({ page, model }) => {
  await setupDemoCamera(page, model);
  const reports = attachPocketReports(model);
  reports.modify = runtime => {
    runtime.radio_mode = runtime.pilot_sample.mode = runtime.assistance.selected_mode = 'Y';
    runtime.pilot_sample.lua_outputs.yaw.value = runtime.assistance.yaw.value = 0;
    setReportedAxis(runtime, 'pitch', 'manual');
  };
  await enterDemo(page);
  await expect(page.locator('#demo-yaw-correction')).toHaveText('0%');
  await expect(page.locator('#demo-shot-title')).toHaveText('Keeping the subject centered.');
  await expect(page.locator('#demo-shot-subtitle')).toHaveText('Distance assistance: not selected.');
  await expect(page.locator('#demo-shot-state')).toHaveText('ASSISTED');
});
