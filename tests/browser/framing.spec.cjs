const { test, expect, open } = require('./fixtures.cjs');

test.use({ hasTouch: true });
const zero = () => ({ forward: 0, right: 0, up: 0, yaw: 0 });

async function installFraming(page, model) {
  const jpeg = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
    const context = canvas.getContext('2d'); context.fillStyle = '#536950'; context.fillRect(0, 0, 640, 360);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  }), 'base64');
  const framing = { enabled: true, revision: 0, phase: 'idle', active: false, paused: false, target_id: null, available: false,
    reason: 'Select a person in the image.', error_x: null, error_y: null, height: null, reference_height: null,
    axes: zero(), frame_age_s: 0, takeover_remaining_s: null };
  const control = { at: 0, enabled: true, available: true, reason: '', owned: false, phase: 'idle', last_input_age: 0,
    selected_mode: 2, prepared: false, throttle: 0, input_seq: -1, axes: zero(),
    vehicle: { armed: false, mode: 0, landed: true, heartbeat_age: .01 }, profile: { ready: true }, command: null, last_error: '', framing };
  const mock = { control, framing, calls: [], frames: new Map(), sequence: 0, token: null, intent: 0, manualSeq: -1,
    video: 'framing-video-1', visionState: 'recent', hold: null, stopError: false, visiblePerson: 7 };
  const snapshot = () => {
    framing.available = control.vehicle.armed && control.vehicle.landed === false && control.vehicle.mode === 2
      && framing.target_id !== null && framing.phase !== 'takeover' && mock.visionState === 'recent';
    return { ...structuredClone(control), at: model.clock() };
  };
  model.modifyState = value => {
    value.control = snapshot();
    Object.assign(value.video, { source: 'gazebo', source_id: mock.video, state: 'recent', label: 'Test camera', endpoint: '/test/camera',
      sequence: mock.sequence, received_at: value.at - .01, rx_age_s: .01, width: 640, height: 360, age_limit_s: 1 });
    value.vision = { configured: true, state: mock.visionState, detail: '', model: 'YOLOX-Tiny', max_hz: 5,
      age_limit_s: 1, frame_age_s: .01, inference_ms: 20, processed: mock.sequence, tracks: 1 };
  };
  await page.route(/\/api\/(?:vision\/)?frame\.jpg$/, async route => {
    const sequence = ++mock.sequence;
    const identity = { run_id: model.run, video_id: mock.video, frame_sequence: sequence, track_id: mock.visiblePerson };
    mock.frames.set(sequence, identity);
    await route.fulfill({ contentType: 'image/jpeg', body: jpeg, headers: { 'X-Run-Id': model.run, 'X-Video-Id': mock.video,
      'X-Frame-Sequence': String(sequence), 'X-Frame-Received-At': String(model.clock() - .01),
      'X-Vision-Result': JSON.stringify({ width: 640, height: 360, inference_ms: 20,
        detections: [{ track_id: mock.visiblePerson, box: [.45, .3, .12, .4], confidence: .92 }] }) } });
  });
  const holdFor = async hold => {
    hold.requested = true; model.pending.add(hold);
    await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
  };
  await page.route('**/api/control/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const payload = route.request().postDataJSON();
    mock.calls.push({ path, payload, at: performance.now() });
    let response;
    const reply = async (status, value) => { try { await route.fulfill({ status, json: value }); } catch { /* Delayed requests can be aborted. */ } };
    if (path.endsWith('/claim')) {
      control.owned = true; control.phase = 'claimed'; mock.token = `private-framing-${mock.calls.length}`; mock.intent = 0;
      return reply(200, { token: mock.token, control: snapshot() });
    }
    if (payload.token !== mock.token || !control.owned) return reply(409, { detail: 'Control unavailable.' });
    if (path.endsWith('/input')) {
      expect(payload.seq).toBeGreaterThan(control.input_seq);
      control.input_seq = payload.seq; control.axes = payload.axes;
      if (Object.values(payload.axes).some(Boolean)) {
        mock.manualSeq = payload.seq;
        if (framing.active || framing.phase === 'takeover') {
          framing.active = false; framing.phase = 'selected'; framing.axes = zero(); framing.revision += 1;
          framing.reason = 'Manual control.'; framing.takeover_remaining_s = null;
        }
      }
      return reply(200, { control: snapshot() });
    }
    if (path.endsWith('/action')) {
      const { action } = payload;
      if (action === 'prepare') { control.vehicle.mode = payload.mode; control.selected_mode = payload.mode; control.prepared = true; control.phase = 'prepared'; }
      if (action === 'arm') { control.vehicle.armed = true; control.phase = 'armed'; }
      if (action === 'land') { control.vehicle.mode = 9; control.phase = 'landing'; framing.active = false; framing.phase = 'idle'; }
      if (action === 'release') { control.owned = false; control.phase = 'released'; framing.active = false; framing.phase = 'idle'; }
      control.command = { action, ...(action === 'prepare' ? { mode: payload.mode } : {}), state: 'observed', observed: true, command_id: 1 };
      return reply(200, { control: snapshot() });
    }
    if (!path.endsWith('/framing')) throw new Error(`Unexpected ${path}`);
    const hold = mock.hold?.operation === payload.operation ? mock.hold : null;
    if (hold) mock.hold = null;
    if (hold && !hold.afterApply) await holdFor(hold);
    if (payload.token !== mock.token || !control.owned) return reply(409, { detail: 'Control unavailable.' });
    if (payload.intent <= mock.intent) return reply(409, { detail: 'Superseded framing intent.' });
    mock.intent = payload.intent;
    if (payload.operation === 'stop' && mock.stopError) return reply(503, { detail: 'Stop confirmation unavailable.' });
    if (payload.operation === 'select') {
      expect(mock.frames.get(payload.frame_sequence)).toEqual({ run_id: payload.run_id, video_id: payload.video_id,
        frame_sequence: payload.frame_sequence, track_id: payload.track_id });
      framing.target_id = payload.track_id; framing.phase = 'selected'; framing.active = false;
      framing.error_x = .12; framing.error_y = -.03; framing.height = .2; framing.reference_height = .2;
      framing.reason = 'Selected. Engage after manual takeoff.';
    } else if (payload.operation === 'engage') {
      if (payload.revision !== framing.revision || payload.input_seq > control.input_seq || payload.input_seq < mock.manualSeq) return reply(409, { detail: 'Manual input superseded engagement.' });
      expect(control.vehicle.mode).toBe(2); expect(control.vehicle.landed).toBe(false); expect(control.axes).toEqual(zero());
      framing.active = true; framing.phase = 'active'; framing.reason = 'Framing active.';
    } else if (payload.operation === 'stop') {
      framing.active = false; framing.phase = framing.target_id === null ? 'idle' : 'selected'; framing.axes = zero();
      framing.reason = 'Manual control.'; framing.takeover_remaining_s = null;
    } else if (payload.operation === 'clear') {
      framing.active = false; framing.phase = 'idle'; framing.target_id = null; framing.reference_height = null;
    } else if (['closer', 'farther'].includes(payload.operation)) {
      expect(framing.active).toBe(true);
      expect(framing.paused).toBe(false);
      framing.reference_height *= payload.operation === 'closer' ? 1.1 : .9;
    } else throw new Error(`Unexpected framing ${payload.operation}`);
    framing.revision += 1;
    response = { control: snapshot() };
    if (hold?.afterApply) await holdFor(hold);
    await reply(200, response);
  });
  return mock;
}

const operations = mock => mock.calls.filter(call => call.path.endsWith('/framing'));
const operation = name => `[data-framing-operation="${name}"]`;

async function ready(page, model, { engage = false, mode = 2, select = true } = {}) {
  const mock = await installFraming(page, model);
  await open(page);
  await page.locator('#view-control').click();
  await page.locator('#control-claim').click();
  if (mode !== 2) await page.locator('#control-mode-select').selectOption(String(mode));
  await page.locator('[data-control-action="prepare"]').click();
  await page.locator('[data-control-action="arm"]').click();
  mock.control.vehicle.landed = false;
  await page.locator('#vision-toggle').check();
  await expect(page.getByRole('button', { name: 'Select person #7 for framing' })).toBeVisible();
  if (select) {
    await page.getByRole('button', { name: 'Select person #7 for framing' }).click();
    await expect(page.locator('#framing-target')).toHaveText('Person #7');
  }
  if (engage) {
    await page.locator(operation('engage')).click();
    await expect(page.locator('#framing-status')).toContainText('Framing active');
  }
  return mock;
}

test('Observation and an unowned lease cannot select or engage flight assistance', async ({ page, model }) => {
  const mock = await installFraming(page, model);
  await open(page);
  await page.locator('#vision-toggle').check();
  await expect(page.locator('.vision-box')).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Select person #7 for framing' })).toHaveCount(0);
  await page.locator('#view-control').click();
  await expect(page.locator(operation('engage'))).toBeDisabled();
  await page.locator('.vision-box-label').dispatchEvent('click');
  expect(operations(mock)).toHaveLength(0);
});

test('selecting sends exact displayed frame identity and engagement stays explicit', async ({ page, model }) => {
  const mock = await ready(page, model);
  expect(operations(mock).map(call => call.payload.operation)).toEqual(['select']);
  await expect(page.locator('#framing-error')).toHaveText('+0.12 / -0.03');
  await expect(page.locator('#framing-height')).toHaveText('20.0% / 20.0%');
  await page.locator(operation('engage')).click();
  await expect(page.locator('#vision-status')).toContainText('Framing assistance active');
  await expect(page.locator('#view-context')).toHaveText('Assisted framing · GPS-free');
  const engage = operations(mock).find(call => call.payload.operation === 'engage').payload;
  expect(engage.input_seq).toBeGreaterThanOrEqual(1);
  expect(engage.revision).toBe(1);
  expect(mock.calls.filter(call => call.path.endsWith('/action')).map(call => call.payload.action)).toEqual(['prepare', 'arm']);
  expect(await page.locator('#framing-controls').innerText()).not.toMatch(/metre|meter|position hold/i);
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeEnabled();
});

test('AltHold and confirmed airborne state are required before engagement', async ({ page, model }) => {
  const mock = await ready(page, model, { mode: 0 });
  await expect(page.locator(operation('engage'))).toBeDisabled();
  mock.control.vehicle.mode = 2; mock.control.selected_mode = 2; mock.control.vehicle.landed = true;
  await page.waitForTimeout(180);
  await expect(page.locator(operation('engage'))).toBeDisabled();
  mock.control.vehicle.landed = null;
  await page.waitForTimeout(180);
  await expect(page.locator(operation('engage'))).toBeDisabled();
  mock.control.vehicle.landed = false;
  await expect(page.locator(operation('engage'))).toBeEnabled();
});

test('touch and keyboard select a person without losing focused identity on incoming frames', async ({ page, model }) => {
  const mock = await ready(page, model, { select: false });
  const select = page.getByRole('button', { name: 'Select person #7 for framing' });
  await select.focus();
  await page.waitForTimeout(350);
  await expect(select).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#framing-target')).toHaveText('Person #7');
  await page.locator(operation('clear')).click();
  await expect(page.locator('#framing-target')).toHaveText('No target');
  await select.tap();
  await expect(page.locator('#framing-target')).toHaveText('Person #7');
  expect(operations(mock).map(call => call.payload.operation)).toEqual(['select', 'clear', 'select']);
});

test('slow selection and engagement keep serialized neutral pilot inputs flowing', async ({ page, model }) => {
  const mock = await ready(page, model, { select: false });
  for (const name of ['select', 'engage']) {
    const hold = { operation: name }; mock.hold = hold;
    if (name === 'select') await page.getByRole('button', { name: 'Select person #7 for framing' }).click();
    else await page.locator(operation('engage')).click();
    await expect.poll(() => hold.requested).toBe(true);
    const before = mock.calls.filter(call => call.path.endsWith('/input')).length;
    await page.waitForTimeout(750);
    expect(mock.calls.filter(call => call.path.endsWith('/input')).length).toBeGreaterThan(before + 3);
    await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeEnabled();
    await expect(page.locator('[data-control-action="land"]')).toBeEnabled();
    hold.release();
    await expect(page.locator('#framing-status')).not.toContainText(name === 'select' ? 'Selecting' : 'Engaging');
  }
});

for (const action of ['direction', 'manual', 'toggle']) {
  test(`${action} supersedes a delayed engagement and prevents late resurrection`, async ({ page, model }) => {
    const mock = await ready(page, model);
    const hold = { operation: 'engage' }; mock.hold = hold;
    await page.locator(operation('engage')).click();
    await expect.poll(() => hold.requested).toBe(true);
    if (action === 'direction') { await page.keyboard.press('ArrowUp'); }
    if (action === 'manual') await page.locator(operation('stop')).click();
    if (action === 'toggle') await page.locator('#vision-toggle').uncheck();
    await expect.poll(() => operations(mock).some(call => call.payload.operation === 'stop')).toBe(true);
    hold.release();
    await page.waitForTimeout(250);
    expect(mock.framing.active).toBe(false);
    const requests = operations(mock);
    expect(requests.find(call => call.payload.operation === 'stop').payload.intent)
      .toBeGreaterThan(requests.find(call => call.payload.operation === 'engage').payload.intent);
    await expect(page.locator('#vision-status')).not.toContainText('Framing assistance active');
  });
}

test('an already accepted but delayed engagement reply cannot overwrite manual takeover', async ({ page, model }) => {
  const mock = await ready(page, model);
  const hold = { operation: 'engage', afterApply: true }; mock.hold = hold;
  await page.locator(operation('engage')).click();
  await expect.poll(() => hold.requested).toBe(true);
  await page.locator(operation('stop')).click();
  await expect.poll(() => mock.framing.active).toBe(false);
  hold.release();
  await page.waitForTimeout(250);
  await expect(page.locator('#framing-status')).toContainText('Manual control');
  await expect(page.locator('#vision-status')).not.toContainText('Framing assistance active');
});

test('a nonzero manual direction exits active assistance and closer/farther only adjust image height', async ({ page, model }) => {
  const mock = await ready(page, model, { engage: true });
  await page.locator(operation('closer')).click();
  await expect(page.locator('#framing-height')).toHaveText('20.0% / 22.0%');
  await page.locator(operation('farther')).click();
  await expect(page.locator('#framing-height')).toHaveText('20.0% / 19.8%');
  await page.keyboard.down('e');
  await expect.poll(() => mock.control.axes.yaw).toBe(1);
  await expect.poll(() => mock.framing.active).toBe(false);
  await page.keyboard.up('e');
  await expect(page.locator('#view-context')).toHaveText('Manual flight · GPS-free');
  await expect(page.locator(operation('closer'))).toBeDisabled();
  expect(mock.control.owned).toBe(true);
});

test('a brief detection pause clears measurements and resumes the same person while manual input keeps priority', async ({ page, model }) => {
  const mock = await ready(page, model, { engage: true });
  const pausedReason = 'Detection interrupted; corrections paused';
  Object.assign(mock.framing, { paused: true, reason: pausedReason, axes: zero(),
    error_x: null, error_y: null, height: null });
  await expect(page.locator('#framing-status')).toHaveText(`Framing paused · ${pausedReason}`);
  await expect(page.locator('#vision-status')).toContainText('Framing paused');
  await expect(page.locator('#view-context')).toHaveText('Framing paused · GPS-free');
  await expect(page.locator('.control-heading .eyebrow')).toHaveText('FRAMING PAUSED');
  await expect(page.locator('#framing-target')).toHaveText('Person #7');
  await expect(page.locator('#framing-error')).toHaveText('— / —');
  await expect(page.locator('#framing-height')).toHaveText('— / 20.0%');
  for (const name of ['engage', 'closer', 'farther']) await expect(page.locator(operation(name))).toBeDisabled();
  await expect(page.locator(operation('stop'))).toBeEnabled();
  await expect(page.locator('[data-control-action="land"]')).toBeEnabled();

  Object.assign(mock.framing, { paused: false, reason: 'Framing active.',
    error_x: -.04, error_y: .02, height: .21 });
  await expect(page.locator('#vision-status')).toContainText('Framing assistance active');
  await expect(page.locator('#view-context')).toHaveText('Assisted framing · GPS-free');
  await expect(page.locator('#framing-status')).toContainText('Framing active');
  await expect(page.locator('#framing-target')).toHaveText('Person #7');
  await expect(page.locator('#framing-error')).toHaveText('-0.04 / +0.02');
  await expect(page.locator('#framing-height')).toHaveText('21.0% / 20.0%');
  for (const name of ['closer', 'farther']) await expect(page.locator(operation(name))).toBeEnabled();
  expect(operations(mock).map(call => call.payload.operation)).toEqual(['select', 'engage']);

  // A pause also hides any older measurements retained in a state snapshot.
  Object.assign(mock.framing, { paused: true, reason: pausedReason });
  await expect(page.locator('#framing-error')).toHaveText('— / —');
  await expect(page.locator('#framing-height')).toHaveText('— / 20.0%');
  await page.keyboard.down('e');
  await expect.poll(() => mock.control.axes.yaw).toBe(1);
  await expect.poll(() => mock.framing.active).toBe(false);
  await expect(page.locator('#view-context')).toHaveText('Manual flight · GPS-free');
  const inputs = mock.calls.filter(call => call.path.endsWith('/input')).length;
  await expect.poll(() => mock.calls.filter(call => call.path.endsWith('/input')).length).toBeGreaterThan(inputs + 1);
  expect(mock.control.axes.yaw).toBe(1);
  await page.keyboard.up('e');
  await expect.poll(() => mock.control.axes.yaw).toBe(0);
  expect(mock.control.owned).toBe(true);
});

test('takeover requires Manual or a real direction; neutral keepalives do not acknowledge loss', async ({ page, model }) => {
  const mock = await ready(page, model, { engage: true });
  Object.assign(mock.framing, { active: false, phase: 'takeover', reason: 'Person lost', takeover_remaining_s: 1.7 });
  await expect(page.locator('#framing-status')).toContainText('Manual takeover required within 1.7 s');
  await page.waitForTimeout(220);
  expect(mock.framing.phase).toBe('takeover');
  await expect(page.locator(operation('engage'))).toBeDisabled();
  await expect(page.locator(operation('closer'))).toBeDisabled();
  await expect(page.locator('#framing-error')).toHaveText('— / —');
  await page.locator(operation('stop')).click();
  await expect.poll(() => mock.framing.phase).toBe('selected');
});

test('an unconfirmed Manual request releases authority rather than assuming takeover', async ({ page, model }) => {
  const mock = await ready(page, model, { engage: true });
  mock.stopError = true;
  await page.locator(operation('stop')).click();
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('#control-feedback')).toContainText('Manual takeover was not confirmed');
});

for (const event of ['blur', 'workspace']) {
  test(`${event} releases the flight lease while a framing request is pending`, async ({ page, model }) => {
    const mock = await ready(page, model);
    const hold = { operation: 'engage' }; mock.hold = hold;
    await page.locator(operation('engage')).click();
    await expect.poll(() => hold.requested).toBe(true);
    if (event === 'blur') await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    else await page.locator('#view-observation').click();
    await expect.poll(() => mock.control.owned).toBe(false);
    hold.release();
    await page.waitForTimeout(150);
    await expect(page.locator('#vision-status')).not.toContainText('Framing assistance active');
  });
}

for (const viewport of [{ width: 1366, height: 768 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
  test(`framing and manual controls remain reachable by touch at ${viewport.width}x${viewport.height}`, async ({ page, model }) => {
    await page.setViewportSize(viewport);
    await ready(page, model, { engage: true });
    for (const name of ['stop', 'closer', 'farther']) {
      const button = page.locator(operation(name));
      await button.scrollIntoViewIfNeeded();
      const box = await button.boundingBox();
      expect(box.height).toBeGreaterThanOrEqual(44); expect(box.width).toBeGreaterThanOrEqual(44);
      await expect(button).toBeEnabled();
    }
    const forward = page.getByRole('button', { name: 'Forward, hold', exact: true });
    await forward.scrollIntoViewIfNeeded(); await expect(forward).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('framing.png'), fullPage: true });
  });
}
