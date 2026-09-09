const { test, expect, open } = require('./fixtures.cjs');

test.use({ hasTouch: true });

async function installControl(page, model) {
  const control = { at: 0, enabled: true, available: true, reason: '', owned: false, phase: 'idle', last_input_age: null,
    selected_mode: 2, prepared: false, throttle: 0, input_seq: -1, lease_started_at: null,
    mode_generation: 0, mode_transition: null, mode_transfer: null,
    axes: { forward: 0, right: 0, up: 0, yaw: 0 }, vehicle: { armed: false, mode: 0, landed: true, heartbeat_age: .01 },
    profile: { ready: false, values: {}, required: {} }, command: null, last_error: '' };
  const mock = { control, calls: [], seq: 0, claims: 0, inputsActive: 0, maxInputsActive: 0, holdInput: null, holdClaim: null, token: null,
    switchReason: '', targetThrottle: .374, holdSwitch: null, holdInputBefore: null };
  const snapshot = () => ({ ...structuredClone(control), at: model.clock(), mode_switch: {
    available: control.vehicle.armed && control.vehicle.landed === false && !control.mode_transition && !mock.switchReason,
    reason: mock.switchReason, target_mode: control.selected_mode === 2 ? 0 : 2,
    target_throttle: control.selected_mode === 2 ? mock.targetThrottle : null,
  } });
  mock.snapshot = snapshot;
  const holdFor = async held => {
    held.requested = true; model.pending.add(held);
    await new Promise(resolve => { held.release = resolve; }); model.pending.delete(held);
  };
  mock.completeSwitch = () => {
    const transfer = control.mode_transition;
    if (!transfer) throw new Error('No pending fixture mode transition.');
    control.mode_generation += 1; control.selected_mode = transfer.to_mode; control.vehicle.mode = transfer.to_mode;
    control.throttle = transfer.to_mode === 0 ? transfer.target_throttle : 0;
    control.mode_transfer = { generation: control.mode_generation, from_mode: transfer.from_mode,
      to_mode: transfer.to_mode, completed_at: model.clock(), throttle: control.throttle };
    control.mode_transition = null; control.phase = 'armed';
    control.command.state = 'observed'; control.command.observed = true;
  };
  model.modifyState = value => {
    value.control = snapshot();
    value.telemetry.heartbeat.fields.base_mode = control.vehicle.armed ? 128 : 0;
    value.telemetry.mode.custom_mode = control.vehicle.mode;
    value.telemetry.mode.label = { 0: 'STABILIZE', 2: 'ALT_HOLD', 9: 'LAND' }[control.vehicle.mode];
  };
  await page.route('**/api/control/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const payload = route.request().postDataJSON();
    mock.calls.push({ path, payload });
    let value;
    if (path.endsWith('/claim')) {
      mock.claims += 1;
      mock.token = `private-test-token-${mock.claims}`;
      control.owned = true; control.phase = 'claimed'; control.prepared = false; control.throttle = 0;
      control.lease_started_at = model.clock(); control.mode_generation = 0; control.mode_transition = null; control.mode_transfer = null;
      control.interruption = null;
      value = { token: mock.token, control: snapshot() };
      if (mock.holdClaim) { mock.holdClaim.requested = true; await new Promise(resolve => { mock.holdClaim.release = resolve; }); }
    } else if (payload.token !== mock.token || !control.owned) {
      return route.fulfill({ status: 409, json: { detail: 'Les commandes ont expired.' } });
    } else if (path.endsWith('/input')) {
      if (mock.holdInputBefore) { const held = mock.holdInputBefore; mock.holdInputBefore = null; await holdFor(held); }
      if (payload.token !== mock.token || !control.owned) return route.fulfill({ status: 409, json: { detail: 'Control expired.' } });
      if (mock.inputErrorOnce) { const detail = mock.inputErrorOnce; mock.inputErrorOnce = null; return route.fulfill({ status: 409, json: { detail } }); }
      if (payload.mode_generation !== control.mode_generation) return route.fulfill({ status: 409,
        json: { detail: 'Flight mode changed; synchronize input.', code: 'stale_mode_generation', control: snapshot() } });
      expect(payload.seq).toBeGreaterThan(mock.seq);
      mock.seq = payload.seq;
      control.input_seq = payload.seq;
      control.axes = payload.axes;
      expect(payload.throttle).toBeGreaterThanOrEqual(0);
      expect(payload.throttle).toBeLessThanOrEqual(1);
      if (control.selected_mode === 2 || !control.vehicle.armed) expect(payload.throttle).toBe(0);
      if (control.selected_mode === 0) expect(payload.axes.up).toBe(0);
      if (control.mode_transition) expect(payload.axes).toEqual(neutral);
      control.throttle = payload.throttle;
      mock.inputsActive += 1; mock.maxInputsActive = Math.max(mock.maxInputsActive, mock.inputsActive);
      value = { control: snapshot() };
      const held = mock.holdInput;
      if (held) { mock.holdInput = null; held.requested = true; await new Promise(resolve => { held.release = resolve; }); }
      mock.inputsActive -= 1;
    } else if (path.endsWith('/action')) {
      const action = payload.action;
      if (action === 'switch_mode') {
        expect(payload.mode_generation).toBe(control.mode_generation);
        expect(payload.input_seq).toBeLessThanOrEqual(control.input_seq);
        expect(payload.input_seq).toBeGreaterThanOrEqual(1);
        expect(control.axes).toEqual(neutral);
        expect(payload.throttle).toBeUndefined();
        expect(control.vehicle.landed).toBe(false);
        control.mode_generation += 1;
        control.mode_transition = { from_mode: control.selected_mode, to_mode: payload.mode,
          started_at: model.clock(), deadline: model.clock() + 1, target_throttle: payload.mode === 0 ? mock.targetThrottle : null,
          bridge_throttle: .4 };
        control.phase = 'switching';
        control.command = { action, mode: payload.mode, state: 'accepted', observed: false, command_id: 1 };
        value = { control: snapshot() };
        if (mock.holdSwitch) { const held = mock.holdSwitch; mock.holdSwitch = null; await holdFor(held); }
        return route.fulfill({ json: value }).catch(() => {});
      }
      if (action === 'prepare') { control.vehicle.mode = payload.mode; control.selected_mode = payload.mode; control.profile.ready = true; control.phase = 'prepared'; control.prepared = true; }
      if (action === 'arm') { control.vehicle.armed = true; control.phase = 'armed'; }
      if (action === 'land') { control.vehicle.mode = 9; control.phase = 'landing'; control.throttle = 0; }
      if (action === 'disarm') { control.vehicle.armed = false; control.phase = 'prepared'; control.throttle = 0; }
      if (action === 'release') {
        control.owned = false; control.phase = 'released'; control.throttle = 0; control.mode_transition = null; mock.token = null;
        if (mock.releaseReason) control.interruption = { at: model.clock(), lease_started_at: control.lease_started_at, reason: mock.releaseReason };
      }
      control.command = { action, ...(action === 'prepare' ? { mode: payload.mode } : {}), command_id: 1, sent_at: model.clock(), transport: 'accepted', ack: 0, observed: true, state: 'observed', detail: '' };
      value = { control: snapshot() };
    } else throw new Error(`Unexpected control fixture route ${path}`);
    try { await route.fulfill({ json: value }); } catch { /* Lifecycle release can outlive a deliberately aborted request. */ }
  });
  return mock;
}

const lastAxes = mock => mock.calls.filter(call => call.path.endsWith('/input')).at(-1)?.payload.axes;
const lastThrottle = mock => mock.calls.filter(call => call.path.endsWith('/input')).at(-1)?.payload.throttle;
const neutral = { forward: 0, right: 0, up: 0, yaw: 0 };
async function ready(page, model, mode = 2) {
  const mock = await installControl(page, model);
  await open(page);
  await page.locator('#view-control').click();
  await page.locator('#control-claim').click();
  if (mode !== 2) await page.locator('#control-mode-select').selectOption(String(mode));
  await page.locator('[data-control-action=prepare]').click();
  await page.locator('[data-control-action=arm]').click();
  await expect(page.locator('[data-control-axis=forward][data-control-value="1"]')).toBeEnabled();
  return mock;
}
async function center(locator) {
  const box = await locator.boundingBox();
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

async function airborneReady(page, model, mode = 2) {
  const mock = await ready(page, model, mode);
  mock.control.vehicle.landed = false;
  await expect(page.locator('#control-mode-select')).toBeEnabled();
  return mock;
}

test('airborne mode choice needs an explicit touch action and pending transfer keeps neutral lease inputs', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  const button = page.locator('#control-mode-switch');
  await expect(button).toBeDisabled();
  await page.locator('#control-mode-select').selectOption('0');
  await expect(button).toBeEnabled();
  await page.waitForTimeout(120);
  expect(mock.calls.some(call => call.payload.action === 'switch_mode')).toBe(false);
  const held = {}; mock.holdSwitch = held;
  await button.tap();
  await expect.poll(() => held.requested).toBe(true);
  await expect(button).toHaveText('Switching…');
  await expect(page.locator('#control-mode')).toHaveText('Mode AltHold');
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeDisabled();
  const count = mock.calls.filter(call => call.path.endsWith('/input')).length;
  await expect.poll(() => mock.calls.filter(call => call.path.endsWith('/input')).length).toBeGreaterThan(count + 1);
  const transferInputs = mock.calls.filter(call => call.path.endsWith('/input') && call.payload.mode_generation === 1);
  expect(transferInputs.length).toBeGreaterThan(0);
  for (const call of transferInputs) { expect(call.payload.axes).toEqual(neutral); expect(call.payload.throttle).toBe(0); }
  expect(mock.calls.some(call => call.payload.action === 'release')).toBe(false);
  mock.completeSwitch(); held.release();
  await expect(page.locator('#control-throttle')).toBeEnabled();
  await expect.poll(() => lastThrottle(mock)).toBe(.374);
  expect(mock.calls.filter(call => call.payload.action === 'switch_mode')).toHaveLength(1);
});

test('transferred gas seeds once, retains decimal steps and rejects late lower-generation state', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  await page.locator('#control-mode-select').selectOption('0');
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => mock.control.mode_generation).toBe(1);
  const old = mock.snapshot();
  mock.completeSwitch();
  await expect.poll(() => lastThrottle(mock)).toBe(.374);
  await expect(page.locator('#control-throttle-value')).toHaveText('37.4 %');
  await page.getByRole('button', { name: 'Increase throttle by 2 percentage points', exact: true }).click();
  await expect.poll(() => lastThrottle(mock)).toBe(.394);
  await page.locator('#control-panel').click({ position: { x: 2, y: 2 } });
  await page.keyboard.press('r');
  await expect.poll(() => lastThrottle(mock)).toBe(.414);
  // A future timestamp cannot make an older mode generation authoritative.
  old.at = model.clock() + 5;
  await page.evaluate(value => document.dispatchEvent(new CustomEvent('argos:control-state', { detail: value })),
    { run_id: model.run, fresh: true, environment: 'simulation', control: old });
  await expect(page.locator('#control-mode')).toHaveText('Mode Stabilize');
  await page.waitForTimeout(150);
  await expect.poll(() => lastThrottle(mock)).toBe(.414);
  // The retained completion record remains present in every later snapshot.
  expect(mock.control.mode_transfer.throttle).toBe(.374);
  await expect(page.locator('#control-throttle-value')).toHaveText('41.4 %');
});

test('Stabilize draft and transfer keep held gas until AltHold is observed', async ({ page, model }) => {
  const mock = await airborneReady(page, model, 0);
  await page.locator('#control-throttle').fill('47.3');
  await expect.poll(() => lastThrottle(mock)).toBe(.473);
  await page.locator('#control-mode-select').selectOption('2');
  await page.waitForTimeout(120);
  expect(lastThrottle(mock)).toBe(.473);
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => mock.control.phase).toBe('switching');
  await expect.poll(() => mock.calls.filter(call => call.path.endsWith('/input')).at(-1).payload.mode_generation).toBe(1);
  expect(lastThrottle(mock)).toBe(.473);
  expect(lastAxes(mock)).toEqual(neutral);
  mock.completeSwitch();
  await expect(page.getByRole('button', { name: 'Climb, hold', exact: true })).toBeEnabled();
  await expect.poll(() => lastThrottle(mock)).toBe(0);
  await expect(page.locator('#control-throttle-group')).toBeHidden();
});

test('stale-generation input syncs from typed conflict without releasing or dropping transferred gas', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  await page.locator('#control-mode-select').selectOption('0');
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => mock.control.mode_generation).toBe(1);
  const held = {}; mock.holdInputBefore = held;
  await expect.poll(() => held.requested).toBe(true);
  mock.completeSwitch(); held.release();
  await expect.poll(() => mock.calls.filter(call => call.path.endsWith('/input')).at(-1).payload.mode_generation).toBe(2);
  await expect.poll(() => lastThrottle(mock)).toBe(.374);
  await expect(page.locator('#control-authority')).toHaveText('You have control');
  expect(mock.calls.some(call => call.payload.action === 'release')).toBe(false);
  expect(mock.claims).toBe(1);
});

test('an unrelated input 409 during transfer still releases control', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  await page.locator('#control-mode-select').selectOption('0');
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => mock.control.mode_generation).toBe(1);
  mock.inputErrorOnce = 'Pilot input rejected.';
  await expect.poll(() => mock.calls.some(call => call.payload.action === 'release')).toBe(true);
  await expect(page.locator('#control-feedback')).toContainText('Pilot input rejected. Control released.');
  await expect(page.locator('#control-throttle')).toBeDisabled();
});

test('late mode-action reply after release cannot restore mode authority', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  const held = {}; mock.holdSwitch = held;
  await page.locator('#control-mode-select').selectOption('0');
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => held.requested).toBe(true);
  await page.locator('[data-control-action=release]').click();
  held.release();
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.locator('#control-throttle')).toBeDisabled();
  await expect(page.locator('#control-throttle-value')).toHaveText('0 %');
  expect(mock.control.owned).toBe(false);
});

test('fresh claim accepts reset generation after a completed earlier transfer', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  await page.locator('#control-mode-select').selectOption('0');
  await page.locator('#control-mode-switch').click();
  await expect.poll(() => mock.control.mode_generation).toBe(1);
  mock.completeSwitch();
  await expect.poll(() => lastThrottle(mock)).toBe(.374);
  await page.locator('[data-control-action=release]').click();
  mock.control.vehicle.armed = false; mock.control.vehicle.landed = true;
  await page.locator('#control-claim').click();
  await expect(page.locator('#control-authority')).toHaveText('You have control');
  await expect.poll(() => mock.calls.filter(call => call.path.endsWith('/input')).at(-1).payload.mode_generation).toBe(0);
  await expect(page.locator('#control-throttle-value')).toHaveText('0 %');
  expect(mock.claims).toBe(2);
});

test('mode transfer readiness explains missing autopilot output without suggesting default gas', async ({ page, model }) => {
  const mock = await airborneReady(page, model);
  mock.switchReason = 'Waiting for recent autopilot thrust.';
  await page.locator('#control-mode-select').selectOption('0');
  await expect(page.locator('#control-mode-switch')).toBeDisabled();
  await expect(page.locator('#control-feedback')).toContainText('Waiting for recent autopilot thrust.');
  expect(mock.calls.some(call => call.payload.action === 'switch_mode')).toBe(false);
  await expect(page.locator('#control-mode-note')).toContainText('autopilot’s recent output');
});

for (const [width, height] of [[1366, 768], [768, 1024], [360, 640]]) {
  test(`airborne mode transfer remains touch-accessible at ${width}x${height}`, async ({ page, model }) => {
    await page.setViewportSize({ width, height });
    const mock = await airborneReady(page, model);
    await page.locator('#control-mode-select').selectOption('0');
    const geometry = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { width: r.width, height: r.height }; };
      return { button: rect('#control-mode-switch'), selector: rect('#control-mode-select'), camera: rect('#camera-stage'),
        scrollWidth: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth };
    });
    expect(geometry.button.width).toBeGreaterThanOrEqual(44); expect(geometry.button.height).toBeGreaterThanOrEqual(44);
    expect(geometry.selector.height).toBeGreaterThanOrEqual(44); expect(geometry.selector.width).toBeGreaterThan(90);
    expect(geometry.camera.height).toBeGreaterThan(100); expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewport);
    await page.screenshot({ path: test.info().outputPath('airborne-mode-choice.png'), fullPage: true });
    await page.locator('#control-mode-switch').tap();
    await expect.poll(() => mock.control.phase).toBe('switching');
    await expect(page.locator('#control-feedback')).toContainText('Directions paused');
    await page.screenshot({ path: test.info().outputPath('airborne-mode-pending.png'), fullPage: true });
    mock.completeSwitch();
    await expect(page.locator('#control-throttle-value')).toHaveText('37.4 %');
    await page.screenshot({ path: test.info().outputPath('airborne-stabilize.png'), fullPage: true });
  });
}

test('pilotage is explicit and mouse hold releases outside the button without latching', async ({ page, model }) => {
  const mock = await installControl(page, model);
  await open(page);
  await page.keyboard.press('ArrowUp');
  expect(mock.calls).toHaveLength(0);
  await page.locator('#view-control').click();
  await expect(page.locator('#camera-stage')).toBeVisible();
  const forward = page.getByRole('button', { name: 'Forward, hold', exact: true });
  await expect(forward).toBeDisabled();
  await page.locator('#control-claim').click();
  await expect(forward).toBeDisabled();
  await expect(page.locator('[data-control-action=arm]')).toBeDisabled();
  await page.locator('[data-control-action=prepare]').click();
  await page.locator('[data-control-action=arm]').click();
  await forward.hover(); await page.mouse.down();
  await expect.poll(() => lastAxes(mock)?.forward).toBe(1);
  await page.mouse.move(100, 200); await page.mouse.up();
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  await expect(forward).toHaveAttribute('aria-pressed', 'false');
  await forward.dispatchEvent('click');
  await page.waitForTimeout(130);
  expect(lastAxes(mock)).toEqual(neutral);
  expect(mock.claims).toBe(1);
  expect(await page.evaluate(() => `${JSON.stringify(localStorage)}${JSON.stringify(sessionStorage)}${document.body.innerHTML}`)).not.toContain('private-test-token');
});

test('two real touch pointers combine axes; pointer cancellation clears every held button', async ({ page, model, context }) => {
  const mock = await ready(page, model);
  const cdp = await context.newCDPSession(page);
  const up = await center(page.getByRole('button', { name: 'Climb, hold', exact: true }));
  const right = await center(page.getByRole('button', { name: 'Right, hold', exact: true }));
  const first = { ...up, id: 1 }, second = { ...right, id: 2 };
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [first] });
  await expect.poll(() => lastAxes(mock)?.up).toBe(1);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [first, second] });
  await expect.poll(() => lastAxes(mock)).toEqual({ ...neutral, up: 1, right: 1 });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [first] });
  await expect.poll(() => lastAxes(mock)).toEqual({ ...neutral, right: 1 });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  await expect(page.locator('.control-direction[aria-pressed=true]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Climb, hold', exact: true })).toHaveCSS('touch-action', 'none');
  await expect(page.locator('#camera-stage')).not.toHaveCSS('touch-action', 'none');
});

test('lost pointer capture neutralizes its axis', async ({ page, model }) => {
  const mock = await ready(page, model);
  const forward = page.getByRole('button', { name: 'Forward, hold', exact: true });
  await forward.evaluate(button => button.addEventListener('pointerdown', event => { button.dataset.testPointer = event.pointerId; }, { once: true }));
  await forward.hover(); await page.mouse.down();
  await expect.poll(() => lastAxes(mock)?.forward).toBe(1);
  await forward.evaluate(button => button.releasePointerCapture(Number(button.dataset.testPointer)));
  await page.mouse.move(100, 200);
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  await page.mouse.up();
});

test('keyboard axes combine, ignore editing and modifiers, and stop when leaving pilotage', async ({ page, model }) => {
  const mock = await ready(page, model);
  await page.keyboard.down('ArrowUp'); await page.keyboard.down('r');
  await expect.poll(() => lastAxes(mock)).toEqual({ ...neutral, forward: 1, up: 1 });
  await page.keyboard.up('ArrowUp'); await page.keyboard.up('r');
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  await page.keyboard.press('Control+ArrowUp');
  await page.keyboard.press('Meta+r');
  await page.waitForTimeout(120);
  expect(lastAxes(mock)).toEqual(neutral);
  await page.locator('#control-panel').evaluate(panel => { const input = document.createElement('input'); input.id = 'test-edit'; panel.prepend(input); input.focus(); });
  await page.keyboard.type('ref'); await page.keyboard.press('ArrowUp');
  await page.waitForTimeout(120);
  expect(lastAxes(mock)).toEqual(neutral);
  await page.locator('#test-edit').evaluate(input => input.remove());
  await page.keyboard.down('ArrowRight');
  await expect.poll(() => lastAxes(mock)?.right).toBe(1);
  await page.locator('#view-messages').click();
  await expect.poll(() => mock.control.owned).toBe(false);
  await page.keyboard.up('ArrowRight');
  await page.keyboard.press('ArrowLeft');
  const count = mock.calls.filter(call => call.path.endsWith('/input')).length;
  await page.waitForTimeout(180);
  expect(mock.calls.filter(call => call.path.endsWith('/input'))).toHaveLength(count);
});

test('window blur releases the lease and focus return never reclaims it', async ({ page, model }) => {
  const mock = await ready(page, model);
  await page.keyboard.down('ArrowUp');
  await expect.poll(() => lastAxes(mock)?.forward).toBe(1);
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('.control-direction[aria-pressed=true]')).toHaveCount(0);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.keyboard.up('ArrowUp');
  await page.waitForTimeout(180);
  expect(mock.claims).toBe(1);
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeDisabled();
});

test('late claim after focus loss is released without enabling motion', async ({ page, model }) => {
  const mock = await installControl(page, model);
  await open(page); await page.locator('#view-control').click();
  const hold = {}; mock.holdClaim = hold;
  await page.locator('#control-claim').click();
  await expect.poll(() => hold.requested).toBe(true);
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  hold.release();
  await expect.poll(() => mock.calls.some(call => call.payload.action === 'release')).toBe(true);
  await expect.poll(() => mock.control.owned).toBe(false);
  expect(mock.calls.filter(call => call.path.endsWith('/input'))).toHaveLength(0);
});

test('expired lease and source change require a new explicit claim', async ({ page, model }) => {
  const mock = await ready(page, model);
  mock.control.owned = false; mock.control.phase = 'expired';
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeDisabled();
  await page.waitForTimeout(150);
  expect(mock.claims).toBe(1);
  mock.control.vehicle.armed = false;
  await page.locator('#control-claim').click();
  await expect(page.locator('#control-authority')).toHaveText('You have control');
  model.run = 'run-2';
  await expect.poll(() => mock.control.owned).toBe(false);
  expect(mock.claims).toBe(2);
});

test('service loss clears held axes and recovery leaves control unclaimed', async ({ page, model }) => {
  const mock = await ready(page, model);
  await page.keyboard.down('r');
  await expect.poll(() => lastAxes(mock)?.up).toBe(1);
  model.offline = true;
  await expect(page.locator('#service-status')).toHaveText('Service unreachable');
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('.control-direction[aria-pressed=true]')).toHaveCount(0);
  model.offline = false;
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  await page.keyboard.up('r');
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Climb, hold', exact: true })).toBeDisabled();
  expect(mock.claims).toBe(1);
});

test('masked document releases control and visibility return never reclaims it', async ({ page, model }) => {
  const mock = await ready(page, model);
  await page.keyboard.down('ArrowUp');
  await expect.poll(() => lastAxes(mock)?.forward).toBe(1);
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('.control-direction[aria-pressed=true]')).toHaveCount(0);
  await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
  await page.keyboard.up('ArrowUp');
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeDisabled();
  expect(mock.claims).toBe(1);
});

test('input requests are serialized and coalesce changed axes without replaying old maneuvers', async ({ page, model }) => {
  const mock = await ready(page, model);
  const hold = {}; mock.holdInput = hold;
  await expect.poll(() => hold.requested).toBe(true);
  await page.keyboard.down('ArrowUp'); await page.keyboard.up('ArrowUp');
  await page.keyboard.down('r');
  hold.release();
  await expect.poll(() => lastAxes(mock)).toEqual({ ...neutral, up: 1 });
  expect(mock.maxInputsActive).toBe(1);
  await page.keyboard.up('r');
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
});

async function delayNextInputBody(page) {
  await page.evaluate(() => {
    const fetch = window.fetch.bind(window);
    let next = true;
    window.inputBodyDelay = { entered: false, abortedAfter: null, resumed: false };
    window.fetch = async (url, init) => {
      if (!next || !String(url).endsWith('/api/control/input')) return fetch(url, init);
      next = false;
      const started = performance.now();
      const response = await fetch(url, init);
      const body = await response.json();
      // Headers and the accepted sequence are already received. Delay the JSON
      // promise to exercise the same deadline through complete body consumption.
      response.json = () => new Promise((resolve, reject) => {
        window.inputBodyDelay.entered = true;
        window.inputBodyDelay.seq = body.control.input_seq;
        window.inputBodyDelay.resume = () => { window.inputBodyDelay.resumed = true; resolve(body); };
        init.signal.addEventListener('abort', () => {
          window.inputBodyDelay.abortedAfter = performance.now() - started;
          reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
      });
      return response;
    };
  });
  await expect.poll(() => page.evaluate(() => window.inputBodyDelay.entered)).toBe(true);
}

test('a stalled input body keeps the 500 ms deadline and its cause after generic release', async ({ page, model }) => {
  const mock = await ready(page, model);
  mock.releaseReason = 'Control released';
  await delayNextInputBody(page);
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('#control-feedback')).toHaveText('The flight-control service did not respond in time. Control released.');
  const delayed = await page.evaluate(() => ({ seq: window.inputBodyDelay.seq, elapsed: window.inputBodyDelay.abortedAfter }));
  expect(delayed.seq).toBe(mock.seq); // The service accepted it; the response deadline still revokes locally.
  expect(delayed.elapsed).toBeGreaterThanOrEqual(475);
  expect(delayed.elapsed).toBeLessThan(650);
  const inputs = mock.calls.filter(call => call.path.endsWith('/input')).length;
  await page.evaluate(() => window.inputBodyDelay.resume());
  await page.waitForTimeout(700);
  expect(mock.calls.filter(call => call.path.endsWith('/input'))).toHaveLength(inputs);
  expect(mock.calls.filter(call => call.payload.action === 'release')).toHaveLength(1);
  expect(mock.claims).toBe(1);
  await expect(page.getByRole('button', { name: 'Forward, hold', exact: true })).toBeDisabled();
  await expect(page.locator('#control-feedback')).toContainText('did not respond in time');
  // Observing another browser's later lease also closes this association.
  mock.control.lease_started_at = model.clock();
  await expect(page.locator('#control-feedback')).not.toContainText('did not respond in time');
  mock.control.vehicle.armed = false;
  await page.locator('#control-claim').click();
  await expect(page.locator('#control-authority')).toHaveText('You have control');
  await expect(page.locator('#control-feedback')).not.toContainText('did not respond in time');
  expect(mock.claims).toBe(2);
});

for (const reason of ['Browser inputs expired', 'Selected target was lost; no manual takeover within 2 seconds. Landing requested']) {
  test(`a specific service interruption stays visible after a client timeout: ${reason}`, async ({ page, model }) => {
    const mock = await ready(page, model);
    mock.releaseReason = reason;
    await delayNextInputBody(page);
    await expect.poll(() => mock.control.owned).toBe(false);
    await expect(page.locator('#control-feedback')).toHaveText(reason);
    await page.evaluate(() => window.inputBodyDelay.resume());
    await page.waitForTimeout(200);
    await expect(page.locator('#control-feedback')).toHaveText(reason);
    expect(mock.claims).toBe(1);
  });
}

test('mode selection requires a ground preparation and is locked after arming', async ({ page, model }) => {
  const mock = await installControl(page, model);
  await open(page); await page.locator('#view-control').click();
  const select = page.locator('#control-mode-select'), arm = page.locator('[data-control-action=arm]');
  await expect(select).toBeDisabled();
  await page.locator('#control-claim').click();
  await expect(arm).toBeDisabled();
  await page.locator('[data-control-action=prepare]').click();
  await expect(arm).toBeEnabled();
  await select.selectOption('0');
  await expect(arm).toBeDisabled();
  await expect(page.locator('#control-feedback')).toContainText('Prepare Stabilize');
  await expect(page.locator('#control-throttle')).toBeDisabled();
  expect(mock.calls.filter(call => call.payload.action === 'prepare')).toHaveLength(1);
  mock.control.vehicle.landed = null;
  await expect(select).toBeDisabled();
  await expect(page.locator('[data-control-action=prepare]')).toBeDisabled();
  mock.control.vehicle.landed = true;
  await page.locator('[data-control-action=prepare]').click();
  expect(mock.calls.filter(call => call.payload.action === 'prepare').at(-1).payload.mode).toBe(0);
  await expect(page.locator('#control-feedback')).toContainText('Preparation Stabilize');
  await arm.click();
  await expect(select).toBeDisabled();
  await expect(page.locator('[data-control-action=prepare]')).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Climb, hold', exact: true })).toBeHidden();
  await expect(page.locator('#control-throttle')).toBeEnabled();
});

test('Stabilize mouse throttle persists at neutral and LAND never pre-sends zero gas', async ({ page, model }) => {
  const mock = await ready(page, model, 0);
  await page.locator('#control-throttle').fill('47');
  await expect.poll(() => lastThrottle(mock)).toBe(.47);
  await page.getByRole('button', { name: 'Increase throttle by 2 percentage points', exact: true }).click();
  await expect.poll(() => lastThrottle(mock)).toBe(.49);
  await page.getByRole('button', { name: 'Decrease throttle by 2 percentage points', exact: true }).click();
  await expect.poll(() => lastThrottle(mock)).toBe(.47);
  const forward = page.getByRole('button', { name: 'Forward, hold', exact: true });
  await forward.hover(); await page.mouse.down();
  await expect.poll(() => lastAxes(mock)?.forward).toBe(1);
  await page.mouse.move(100, 200); await page.mouse.up();
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  expect(lastThrottle(mock)).toBe(.47);
  await expect(page.locator('#control-throttle-value')).toHaveText('47 %');
  await page.locator('[data-control-action=land]').click();
  const landIndex = mock.calls.findIndex(call => call.payload.action === 'land');
  expect(mock.calls.slice(0, landIndex).filter(call => call.path.endsWith('/input')).at(-1).payload.throttle).toBe(.47);
  await expect(page.locator('#control-throttle-value')).toHaveText('0 %');
  await expect(page.locator('#control-throttle')).toBeDisabled();
});

test('real touch combines Stabilize throttle with direction and cancelling touch retains gas', async ({ page, model, context }) => {
  const mock = await ready(page, model, 0);
  const cdp = await context.newCDPSession(page);
  const right = { ...await center(page.getByRole('button', { name: 'Right, hold', exact: true })), id: 1 };
  const gas = { ...await center(page.locator('#control-throttle')), id: 2 };
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [right] });
  await expect.poll(() => lastAxes(mock)?.right).toBe(1);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [right, gas] });
  await expect.poll(() => lastThrottle(mock)).toBeGreaterThan(.4);
  expect(lastAxes(mock)).toEqual({ ...neutral, right: 1 });
  const heldGas = lastThrottle(mock);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  expect(lastThrottle(mock)).toBe(heldGas);
  await page.getByRole('button', { name: 'Increase throttle by 2 percentage points', exact: true }).tap();
  await expect.poll(() => lastThrottle(mock)).toBeCloseTo(heldGas + .02);
  await expect(page.locator('#control-throttle')).toHaveCSS('touch-action', 'none');
});

test('Stabilize keyboard R and F adjust explicit gas without commanding vertical speed', async ({ page, model }) => {
  const mock = await ready(page, model, 0);
  await page.keyboard.press('r'); await page.keyboard.press('r');
  await expect.poll(() => lastThrottle(mock)).toBe(.04);
  await page.keyboard.down('ArrowUp');
  await expect.poll(() => lastAxes(mock)).toEqual({ ...neutral, forward: 1 });
  await page.keyboard.up('ArrowUp'); await page.keyboard.press('f');
  await expect.poll(() => lastThrottle(mock)).toBe(.02);
  await expect.poll(() => lastAxes(mock)).toEqual(neutral);
  await page.keyboard.down('r'); await page.keyboard.down('r'); await page.keyboard.up('r');
  await expect.poll(() => lastThrottle(mock)).toBe(.04);
});

for (const event of ['blur', 'hidden', 'source', 'service', 'disarm']) {
  test(`Stabilize gas resets on ${event} and never resumes automatically`, async ({ page, model }) => {
    const mock = await ready(page, model, 0);
    await page.locator('#control-throttle').fill('37');
    await expect.poll(() => lastThrottle(mock)).toBe(.37);
    if (event === 'blur') await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    if (event === 'hidden') await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
    if (event === 'source') model.run = 'run-2';
    if (event === 'service') model.offline = true;
    if (event === 'disarm') await page.locator('[data-control-action=disarm]').click();
    await expect(page.locator('#control-throttle-value')).toHaveText('0 %');
    if (event !== 'disarm') await expect.poll(() => mock.control.owned).toBe(false);
    mock.control.vehicle.armed = false;
    if (event === 'blur') await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    if (event === 'hidden') await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
    if (event === 'service') model.offline = false;
    if (event !== 'disarm') {
      await page.locator('#control-claim').click();
      await expect(page.locator('[data-control-action=arm]')).toBeDisabled();
      await page.locator('[data-control-action=prepare]').click();
    }
    await page.locator('[data-control-action=arm]').click();
    await expect.poll(() => lastThrottle(mock)).toBe(0);
    await expect(page.locator('#control-throttle-value')).toHaveText('0 %');
  });
}

for (const [width, height] of [[1366, 650], [1024, 768], [768, 1024], [360, 640], [320, 568]]) {
  test(`Stabilize touch gas and camera fit ${width}x${height}`, async ({ page, model }) => {
    await page.setViewportSize({ width, height });
    await ready(page, model, 0);
    const geometry = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { bottom: r.bottom, width: r.width, height: r.height }; };
      return { camera: rect('#camera-stage'), panel: rect('#control-panel'), throttle: rect('#control-throttle'), plus: rect('[data-throttle-step="2"]'),
        pad: rect('.control-inputs'), release: rect('[data-control-action=release]'), scrollWidth: document.documentElement.scrollWidth, width: document.documentElement.clientWidth };
    });
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width);
    expect(geometry.throttle.height).toBeGreaterThanOrEqual(44);
    expect(geometry.plus.width).toBeGreaterThanOrEqual(44);
    expect(geometry.plus.height).toBeGreaterThanOrEqual(44);
    if (width > 760) {
      expect(geometry.pad.bottom).toBeLessThanOrEqual(height);
      expect(geometry.release.bottom).toBeLessThanOrEqual(geometry.panel.bottom);
      expect(geometry.camera.height).toBeGreaterThan(100);
    }
    if (width === 1366 || width === 768 || width === 360) await page.screenshot({ path: test.info().outputPath('stabilize-control.png'), fullPage: true });
  });
}

for (const [width, height] of [[1366, 650], [1024, 768], [768, 1024], [360, 640], [320, 568]]) {
  test(`touch controls remain reachable with the live camera at ${width}x${height}`, async ({ page, model }) => {
    await page.setViewportSize({ width, height });
    await ready(page, model);
    const geometry = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
      return { camera: rect('#camera-stage'), panel: rect('#control-panel'), buttons: Array.from(document.querySelectorAll('.control-direction'), button => ({ width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height })),
        pad: rect('.control-inputs'), release: rect('[data-control-action=release]'), scrollWidth: document.documentElement.scrollWidth, width: document.documentElement.clientWidth };
    });
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width);
    for (const button of geometry.buttons) { expect(button.width).toBeGreaterThanOrEqual(44); expect(button.height).toBeGreaterThanOrEqual(44); }
    if (width > 760) {
      expect(geometry.pad.bottom).toBeLessThanOrEqual(height);
      expect(geometry.release.bottom).toBeLessThanOrEqual(geometry.panel.bottom);
      expect(geometry.camera.height).toBeGreaterThan(100);
      expect(geometry.camera.bottom).toBeLessThanOrEqual(height);
    }
    if (width === 1366 || width === 768 || width === 360) await page.screenshot({ path: test.info().outputPath('manual-control.png'), fullPage: true });
  });
}
