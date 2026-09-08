const { test, expect, open } = require('./fixtures.cjs');

test.use({ hasTouch: true });

async function installControl(page, model) {
  const control = { at: 0, enabled: true, available: true, reason: '', owned: false, phase: 'idle', last_input_age: null,
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
      control.owned = true; control.phase = 'claimed';
      value = { token: mock.token, control: snapshot() };
      if (mock.holdClaim) { mock.holdClaim.requested = true; await new Promise(resolve => { mock.holdClaim.release = resolve; }); }
    } else if (payload.token !== mock.token || !control.owned) {
      return route.fulfill({ status: 409, json: { detail: 'Les commandes ont expiré.' } });
    } else if (path.endsWith('/input')) {
      expect(payload.seq).toBeGreaterThan(mock.seq);
      mock.seq = payload.seq;
      control.axes = payload.axes;
      mock.inputsActive += 1; mock.maxInputsActive = Math.max(mock.maxInputsActive, mock.inputsActive);
      value = { control: snapshot() };
      const held = mock.holdInput;
      if (held) { mock.holdInput = null; held.requested = true; await new Promise(resolve => { held.release = resolve; }); }
      mock.inputsActive -= 1;
    } else if (path.endsWith('/action')) {
      const action = payload.action;
      if (action === 'prepare') { control.vehicle.mode = 2; control.profile.ready = true; control.phase = 'prepared'; }
      if (action === 'arm') { control.vehicle.armed = true; control.phase = 'armed'; }
      if (action === 'land') { control.vehicle.mode = 9; control.phase = 'landing'; }
      if (action === 'disarm') { control.vehicle.armed = false; control.phase = 'prepared'; }
      if (action === 'release') { control.owned = false; control.phase = 'released'; mock.token = null; }
      control.command = { action, command_id: 1, sent_at: model.clock(), transport: 'accepted', ack: 0, observed: true, state: 'observed', detail: '' };
      value = { control: snapshot() };
    } else throw new Error(`Unexpected control fixture route ${path}`);
    try { await route.fulfill({ json: value }); } catch { /* Lifecycle release can outlive a deliberately aborted request. */ }
  });
  return mock;
}

const lastAxes = mock => mock.calls.filter(call => call.path.endsWith('/input')).at(-1)?.payload.axes;
const neutral = { forward: 0, right: 0, up: 0, yaw: 0 };
async function ready(page, model) {
  const mock = await installControl(page, model);
  await open(page);
  await page.locator('#view-control').click();
  await page.locator('#control-claim').click();
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
  const forward = page.getByRole('button', { name: 'Avancer, maintenir', exact: true });
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
  const up = await center(page.getByRole('button', { name: 'Monter, maintenir', exact: true }));
  const right = await center(page.getByRole('button', { name: 'Droite, maintenir', exact: true }));
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
  await expect(page.getByRole('button', { name: 'Monter, maintenir', exact: true })).toHaveCSS('touch-action', 'none');
  await expect(page.locator('#camera-stage')).not.toHaveCSS('touch-action', 'none');
});

test('lost pointer capture neutralizes its axis', async ({ page, model }) => {
  const mock = await ready(page, model);
  const forward = page.getByRole('button', { name: 'Avancer, maintenir', exact: true });
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
  await expect(page.getByRole('button', { name: 'Avancer, maintenir', exact: true })).toBeDisabled();
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
  await expect(page.getByRole('button', { name: 'Avancer, maintenir', exact: true })).toBeDisabled();
  await page.waitForTimeout(150);
  expect(mock.claims).toBe(1);
  mock.control.vehicle.armed = false;
  await page.locator('#control-claim').click();
  await expect(page.locator('#control-authority')).toHaveText('Vous avez les commandes');
  model.run = 'run-2';
  await expect.poll(() => mock.control.owned).toBe(false);
  expect(mock.claims).toBe(2);
});

test('service loss clears held axes and recovery leaves control unclaimed', async ({ page, model }) => {
  const mock = await ready(page, model);
  await page.keyboard.down('r');
  await expect.poll(() => lastAxes(mock)?.up).toBe(1);
  model.offline = true;
  await expect(page.locator('#service-status')).toHaveText('Service inaccessible');
  await expect.poll(() => mock.control.owned).toBe(false);
  await expect(page.locator('.control-direction[aria-pressed=true]')).toHaveCount(0);
  model.offline = false;
  await expect(page.locator('#service-status')).toHaveText('Service connecté');
  await page.keyboard.up('r');
  await expect(page.locator('#control-claim')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Monter, maintenir', exact: true })).toBeDisabled();
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
  await expect(page.getByRole('button', { name: 'Avancer, maintenir', exact: true })).toBeDisabled();
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

for (const [width, height] of [[1366, 650], [1024, 768], [768, 1024], [360, 640], [320, 568]]) {
  test(`touch controls remain reachable with the live camera at ${width}x${height}`, async ({ page, model }) => {
    await page.setViewportSize({ width, height });
    await ready(page, model);
    const geometry = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
      return { camera: rect('#camera-stage'), buttons: Array.from(document.querySelectorAll('.control-direction'), button => ({ width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height })),
        pad: rect('.control-inputs'), release: rect('[data-control-action=release]'), scrollWidth: document.documentElement.scrollWidth, width: document.documentElement.clientWidth };
    });
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width);
    for (const button of geometry.buttons) { expect(button.width).toBeGreaterThanOrEqual(44); expect(button.height).toBeGreaterThanOrEqual(44); }
    if (width > 760) {
      expect(geometry.pad.bottom).toBeLessThanOrEqual(height);
      expect(geometry.release.bottom).toBeLessThanOrEqual(height);
      expect(geometry.camera.height).toBeGreaterThan(100);
      expect(geometry.camera.bottom).toBeLessThanOrEqual(height);
    }
    if (width === 1366 || width === 768 || width === 360) await page.screenshot({ path: test.info().outputPath('manual-control.png'), fullPage: true });
  });
}
