const { test, expect, open } = require('./fixtures.cjs');

test.use({ hasTouch: true });

async function installControl(page, model) {
  const control = { at: 0, enabled: true, available: true, reason: '', owned: false, phase: 'idle', last_input_age: null,
    selected_mode: 2, prepared: false, throttle: 0,
    axes: { forward: 0, right: 0, up: 0, yaw: 0 }, vehicle: { armed: false, mode: 0, landed: true, heartbeat_age: .01 },
    profile: { ready: false, values: {}, required: {} }, command: null, last_error: '' };
  const mock = { control, calls: [], seq: 0, claims: 0, inputsActive: 0, maxInputsActive: 0, holdInput: null, holdClaim: null, token: null };
  const snapshot = () => ({ ...structuredClone(control), at: model.clock() });
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
      value = { token: mock.token, control: snapshot() };
      if (mock.holdClaim) { mock.holdClaim.requested = true; await new Promise(resolve => { mock.holdClaim.release = resolve; }); }
    } else if (payload.token !== mock.token || !control.owned) {
      return route.fulfill({ status: 409, json: { detail: 'Les commandes ont expired.' } });
    } else if (path.endsWith('/input')) {
      expect(payload.seq).toBeGreaterThan(mock.seq);
      mock.seq = payload.seq;
      control.axes = payload.axes;
      expect(payload.throttle).toBeGreaterThanOrEqual(0);
      expect(payload.throttle).toBeLessThanOrEqual(1);
      if (control.selected_mode === 2 || !control.vehicle.armed) expect(payload.throttle).toBe(0);
      if (control.selected_mode === 0) expect(payload.axes.up).toBe(0);
      control.throttle = payload.throttle;
      mock.inputsActive += 1; mock.maxInputsActive = Math.max(mock.maxInputsActive, mock.inputsActive);
      value = { control: snapshot() };
      const held = mock.holdInput;
      if (held) { mock.holdInput = null; held.requested = true; await new Promise(resolve => { held.release = resolve; }); }
      mock.inputsActive -= 1;
    } else if (path.endsWith('/action')) {
      const action = payload.action;
      if (action === 'prepare') { control.vehicle.mode = payload.mode; control.selected_mode = payload.mode; control.profile.ready = true; control.phase = 'prepared'; control.prepared = true; }
      if (action === 'arm') { control.vehicle.armed = true; control.phase = 'armed'; }
      if (action === 'land') { control.vehicle.mode = 9; control.phase = 'landing'; control.throttle = 0; }
      if (action === 'disarm') { control.vehicle.armed = false; control.phase = 'prepared'; control.throttle = 0; }
      if (action === 'release') { control.owned = false; control.phase = 'released'; control.throttle = 0; mock.token = null; }
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
