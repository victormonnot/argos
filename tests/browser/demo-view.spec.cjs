const { test, expect, open } = require('./fixtures.cjs');

const AXES = ['roll', 'pitch', 'throttle', 'yaw'];

// Synthetic images and reports stay inside this browser fixture. The production
// console still uses its configured camera and never supplies demonstration data.
async function setupDemoCamera(page, model) {
  const jpeg = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const context = canvas.getContext('2d');
    context.fillStyle = '#364840'; context.fillRect(0, 0, 640, 360);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  });
  const camera = {
    video: 'demo-camera-1', sequence: 0, phase: 'tracking',
    detections: [{ track_id: 7, confidence: .94, box: [.2, .2, .2, .6] }],
  };
  model.modifyState = state => {
    state.environment = state.configuration.environment = 'real';
    Object.assign(state.configuration, { video_source: 'device', video_endpoint: '/dev/video2' });
    Object.assign(state.video, {
      source: 'device', source_id: camera.video, state: 'recent', endpoint: '/dev/video2',
      label: 'USB camera', sequence: camera.sequence, received_at: state.at - .02,
      rx_age_s: .02, width: 640, height: 360,
    });
    state.vision = {
      configured: true, state: 'recent', detail: '', model: 'YOLOX-Tiny', age_limit_s: 1,
      frame_age_s: .02, inference_ms: 25, processed: camera.sequence, tracks: camera.detections.length,
    };
    state.yaw_preview = {
      enabled: true, continuous: true, phase: camera.phase, detail: '', revision: 1,
      target_id: 7, run_id: model.run, video_id: camera.video, frame_sequence: camera.sequence,
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
      width: 640, height: 360, inference_ms: 25, detections: camera.detections,
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
  await expect(page.locator('#demo-mode')).toHaveText('Unavailable');
  for (const axis of AXES) {
    await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Unavailable');
    await expect(page.locator(`#demo-${axis}-stick`)).toHaveText('Unavailable');
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
  // A hidden hit target or synthetic click must not bypass the read-only state.
  await page.locator('.vision-box').dispatchEvent('click');
  await page.locator('.vision-hit').dispatchEvent('click');
  await page.waitForTimeout(250);
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  await page.locator('#demo-button').click();
  await expect(page.locator('body')).not.toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-button')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#demo-summary')).toBeHidden();
  await expect(page.locator('#yaw-preview')).toBeVisible();
  await expect(page.locator('#vision-toggle')).toBeVisible();
  await expect(page.locator('.vision-box')).toHaveAttribute('data-selectable', 'true');
  await expect(page.locator('.vision-hit')).toBeVisible();
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
    await page.locator('#view-observation').click();
    await expect(page.locator('#demo-button')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('#yaw-preview')).toBeVisible();
  }
  await enterDemo(page);
  await page.locator('#global-recording').click();
  await expect(page.locator('body')).not.toHaveClass(/\bdemo-mode\b/);
  await expect(page.locator('#demo-summary')).toBeHidden();
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
  await expect(page.locator('#demo-target-state')).toHaveText('Assistance paused');
  for (const axis of AXES) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Unavailable');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});

for (const size of [{ width: 1366, height: 650 }, { width: 1920, height: 1080 }, { width: 390, height: 844 }]) {
  test(`demo enlarges the video and keeps its summary reachable at ${size.width}x${size.height}`, async ({ page, model }) => {
    await page.setViewportSize(size);
    await setupDemoCamera(page, model);
    const ordinary = await page.locator('#camera-stage').boundingBox();
    await enterDemo(page);
    const expanded = await page.locator('#camera-stage').boundingBox();
    expect(expanded.width * expanded.height).toBeGreaterThan(ordinary.width * ordinary.height);
    await expect(page.locator('#camera-image')).toHaveCSS('object-fit', 'contain');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('#demo-summary').scrollIntoViewIfNeeded();
    await expect(page.locator('#demo-summary')).toBeInViewport();
    for (const axis of AXES) {
      await page.locator(`#demo-${axis}-state`).scrollIntoViewIfNeeded();
      await expect(page.locator(`#demo-${axis}-state`)).toBeInViewport();
    }
    await page.screenshot({ path: test.info().outputPath('demo-view.png'), fullPage: true });
    expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
  });
}

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

async function expectUnavailable(page, ids) {
  for (const id of ids) await expect(page.locator(`#demo-${id}`)).toHaveText('Unavailable');
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
  await expect(page.locator('#demo-target-state')).toHaveText('Assistance paused');
  for (const axis of ['yaw', 'pitch']) await expect(page.locator(`#demo-${axis}-state`)).toHaveText('Paused');
  camera.phase = 'paused';
  await expect(page.locator('#demo-mode')).toHaveText('Yaw + apparent distance');
  await expect(page.locator('#demo-target')).toHaveText('Person #7');
  camera.phase = 'tracking'; camera.detections = [person];
  await expectPocketReady(page);
  await expect(page.locator('#demo-target-state')).toHaveText('Visible in camera');
  expect(model.calls.filter(call => call.method !== 'GET')).toEqual([]);
});
