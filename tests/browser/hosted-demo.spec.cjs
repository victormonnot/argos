const fs = require('node:fs');
const path = require('node:path');
const { test, expect, ID, open } = require('./fixtures.cjs');
const index = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/index.html'), 'utf8');
const welcome = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/demo-welcome.html'), 'utf8');

async function demoPages(page) {
  const console = index.replace('<body data-view="observation">', '<body data-demo="true" data-view="observation">');
  await page.route('http://127.0.0.1:4173/', route => route.fulfill({ contentType: 'text/html',
    body: console.replace('</body>', `${welcome}</body>`) }));
  await page.route('http://127.0.0.1:4173/demo', route => route.fulfill({ contentType: 'text/html', body: console }));
}

for (const viewport of [{ width: 1366, height: 850 }, { width: 390, height: 844 }]) {
  test(`welcome previews recorded Observation behind an inert overlay at ${viewport.width}px`, async ({ page, model }) => {
    await page.setViewportSize(viewport);
    await demoPages(page);
    await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
    const navigations = [];
    page.on('request', request => { if (request.isNavigationRequest()) navigations.push(request.url()); });
    await page.goto('/');
    const enter = page.getByRole('button', { name: 'Try ARGOS' });
    await expect(enter).toBeVisible();
    await expect(enter).toBeFocused();
    await expect(page.locator('#demo-welcome:modal')).toBeVisible();
    await expect(page.locator('#demo-limits')).toHaveText('Demo mode');
    await expect(page.locator('body')).toHaveAttribute('data-view', 'observation');
    await expect(page.locator('#replay-play')).toBeEnabled();
    await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
    expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
    expect(await page.locator('#demo-welcome').evaluate(node => getComputedStyle(node).backdropFilter)).toContain('blur');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (viewport.width < 760) {
      await page.mouse.move(20, 40);
      await page.mouse.wheel(0, 400);
      await page.waitForTimeout(100);
      expect(await page.evaluate(() => scrollY)).toBe(0);
    }
    await page.screenshot({ path: test.info().outputPath('welcome.png') });
    const callsBeforeEntry = model.calls.length;
    await enter.click();
    await expect(page.locator('#demo-welcome')).toBeHidden();
    await expect(page.locator('#workspace')).toBeFocused();
    expect(await page.locator('#workspace').evaluate(node => getComputedStyle(node).outlineStyle)).toBe('none');
    await expect(page.locator('#workspace')).toBeVisible();
    await expect(page.locator('#sessions-workspace')).toBeHidden();
    await expect(page.locator('#demo-unavailable-note')).toBeVisible();
    await expect(page.locator('#demo-unavailable-note')).toContainText('Unavailable in demo mode');
    await expect(page.getByRole('link', { name: 'Back to welcome' })).toHaveCount(0);
    await expect(page.locator('#demo-install')).toBeVisible();
    expect(navigations).toEqual(['http://127.0.0.1:4173/']);
    expect(model.calls.length).toBe(callsBeforeEntry);
    if (viewport.width < 760) {
      await page.mouse.wheel(0, 400);
      await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0);
      await page.evaluate(() => scrollTo(0, 0));
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('console.png'), fullPage: true });
    await page.locator('#view-sessions').click();
    await expect(page.locator('#replay-play')).toBeEnabled();
    await expect(page.locator('#replay-play')).toHaveText('Play');
    await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
    await expect(page.locator('#history-mode')).toHaveText('STABILIZE');
    await expect(page.locator('#demo-unavailable-note')).toBeHidden();
    expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
  });
}

test('the welcome dialog keeps the console inert and supports keyboard dismissal', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
  await page.goto('/');
  await expect(page.locator('#demo-enter')).toBeFocused();
  await page.locator('#view-sessions').evaluate(node => node.focus());
  await expect(page.locator('#demo-enter')).toBeFocused();
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => document.activeElement === document.body || document.activeElement.closest('#demo-welcome') !== null)).toBe(true);
  await page.keyboard.press('Escape');
  await expect(page.locator('#demo-welcome')).toBeHidden();
  await expect(page.locator('#workspace')).toBeFocused();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.5');
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('direct entry opens Observation and keeps archived replay, messages and analysis available', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
  await page.goto('/demo');
  await expect(page.locator('#demo-welcome')).toHaveCount(0);
  await expect(page.locator('body')).toHaveAttribute('data-view', 'observation');
  for (const selector of ['#view-observation', '#view-control', '#view-messages', '#view-sessions', '#sources-button', '#global-recording']) {
    await expect(page.locator(selector)).toBeVisible();
  }
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-cursor')).toHaveValue('0');
  await expect(page.locator(`#archive-list button[data-id="${ID}"]`)).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#replay-speed').selectOption('2');
  await page.locator('#replay-play').click();
  await expect.poll(async () => Number(await page.locator('#replay-cursor').inputValue())).toBeGreaterThan(0);
  await page.locator('#replay-play').click();
  await page.locator('#replay-cursor').fill('1.5');
  await expect(page.locator('#replay-time')).toContainText('1.5 s');
  await page.locator('#recording-view-messages').click();
  await expect(page.locator('#messages-status')).toHaveText('No messages match these filters.');
  await page.locator('#recording-view-analysis').click();
  await expect.poll(() => model.calls.some(call => call.path.endsWith('/analysis'))).toBe(true);
  await expect(page.locator('#analysis-status')).not.toContainText('Reading');
  await page.locator('#recording-view-measures').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('1.5');
  await page.locator('#view-observation').click();
  await expect(page.locator('#demo-unavailable-note')).toBeVisible();
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('1.5');
  // Neither initial boot nor later interaction may start the local console.
  expect(model.calls.length).toBeGreaterThan(4);
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('an unavailable demo can retry without falling back to live sources', async ({ page, model }) => {
  await demoPages(page);
  let available = false;
  await page.route('**/api/demo-config', route => route.fulfill(available
    ? { json: { enabled: true, recording_id: ID } }
    : { status: 503, json: { detail: 'Unavailable' } }));
  await page.goto('/demo');
  await expect(page.locator('#demo-retry')).toBeVisible();
  await expect(page.locator('#demo-load-status')).toBeVisible();
  await expect(page.locator('#demo-load-status')).toContainText('could not be loaded');
  expect(model.calls).toEqual([]);
  available = true;
  await page.locator('#demo-retry').click();
  await expect(page.locator('#demo-retry')).toBeHidden();
  await expect(page.locator('#demo-load-status')).toBeHidden();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('an invalid demo configuration never requests an arbitrary recording or a live endpoint', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: '../state' } }));
  await page.goto('/demo');
  await expect(page.locator('#demo-retry')).toBeVisible();
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-content')).toBeHidden();
  expect(model.calls).toEqual([]);
});

test('late configuration and workspace changes during archive loading retain one recorded cursor', async ({ page, model }) => {
  await demoPages(page);
  const config = {};
  await page.route('**/api/demo-config', async route => {
    model.pending.add(config);
    await new Promise(resolve => { config.release = resolve; });
    model.pending.delete(config);
    await route.fulfill({ json: { enabled: true, recording_id: ID } });
  });
  await page.goto('/demo');
  await expect.poll(() => typeof config.release).toBe('function');
  await page.locator('#view-sessions').click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'sessions');
  expect(model.calls).toEqual([]);

  const archive = { when: path => path === `/api/recordings/${ID}` };
  model.hold = archive;
  config.release();
  await expect.poll(() => archive.requested).toBe(true);
  await page.locator('#view-observation').click();
  archive.release();
  await expect.poll(() => model.pending.size).toBe(0);
  await expect(page.locator('body')).toHaveAttribute('data-view', 'observation');
  await expect(page.locator('#workspace')).toBeVisible();
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  expect(model.calls.some(call => call.path.endsWith('/replay'))).toBe(true);

  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#replay-cursor')).toHaveValue('0');
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('all hosted workspaces show only archived data and keep operations disabled', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
  await page.goto('/demo');
  await expect(page.locator('#camera-image')).toBeHidden();
  await expect(page.locator('.vision-box')).toHaveCount(0);
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await expect(page.locator('#vision-toggle')).toBeDisabled();

  await page.locator('#demo-button').click();
  await expect(page.locator('body')).toHaveClass(/demo-mode/);
  await expect(page.locator('#demo-history-status')).toHaveText('Radio reports unavailable');
  await expect(page.locator('#demo-history-inspect')).toBeDisabled();
  await page.setViewportSize({ width: 1200, height: 800 });
  await expect(page.locator('#demo-history-status')).toHaveText('Radio reports unavailable');
  await expect(page.locator('.demo-segment')).toHaveCount(0);
  await page.locator('#demo-button').click();
  await expect(page.locator('body')).not.toHaveClass(/demo-mode/);

  await page.locator('#view-control').click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'control');
  await expect(page.locator('#control-panel')).toBeVisible();
  await expect(page.locator('#demo-unavailable-note')).toContainText('Unavailable in demo mode');
  const controls = page.locator('#control-panel button, #control-panel input, #control-panel select');
  expect(await controls.count()).toBeGreaterThan(10);
  for (const control of await controls.all()) await expect(control).toBeDisabled();
  await page.locator('#control-claim').dispatchEvent('click');
  await page.keyboard.press('w');

  await page.locator('#view-messages').click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'messages');
  await expect(page.locator('#messages-workspace')).toBeVisible();
  await expect(page.locator('#demo-unavailable-note')).toBeVisible();
  await expect(page.locator('.live-type')).toHaveCount(0);
  await page.locator('#view-observation').click();
  await expect(page.locator('#workspace')).toBeVisible();
  // More than two normal polling intervals would have reached state/frame/live APIs.
  await page.waitForTimeout(350);
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('hosted source, capture and reception inspectors open while mutations stay disabled', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
  await page.goto('/demo');
  await page.locator('#sources-button').click();
  await expect(page.locator('[data-panel-content="sources"]')).toBeVisible();
  await expect(page.locator('#sources-apply')).toBeDisabled();
  await expect(page.locator('#demo-unavailable-note')).toBeVisible();
  await page.locator('#sources-form').dispatchEvent('submit');
  await page.locator('#inspector-close').click();
  await expect(page.locator('#sources-button')).toBeFocused();

  for (const [opener, content] of [
    ['#mode-button', '[data-telemetry-content="mode"]'],
    ['.reception-bar [data-panel="video"]', '[data-panel-content="video"]'],
    ['#reception-button', '[data-panel-content="reception"]'],
  ]) {
    await page.locator(opener).click();
    await expect(page.locator(content)).toBeVisible();
  }
  for (const control of await page.locator('[data-reconnect]').all()) await expect(control).toBeDisabled();
  await page.locator('[data-panel-content="reception"] [data-reconnect="video"]').dispatchEvent('click');

  await page.locator('#global-recording').click();
  await expect(page.locator('[data-panel-content="recording"]')).toBeVisible();
  await expect(page.locator('#recording-start')).toBeDisabled();
  await expect(page.locator('#recording-include-visual')).toBeDisabled();
  await page.locator('#recording-start').dispatchEvent('click');
  await page.locator('#view-sessions').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await page.locator('#archive-journal').click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'observation');
  await expect(page.locator('[data-panel-content="recording"]')).toBeVisible();
  await expect(page.locator('#recording-start')).toBeDisabled();
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('the local console still receives live data and can record without hosted restrictions', async ({ page, model }) => {
  await open(page);
  await expect(page.locator('#hosted-demo-notice')).toBeHidden();
  await expect(page.locator('#demo-unavailable-note')).toBeHidden();
  await expect(page.locator('#flight-mode')).toHaveText('STABILIZE');
  await page.locator('#view-messages').click();
  await expect(page.locator('.live-type')).toHaveCount(18);
  await page.locator('#global-recording').click();
  await expect(page.locator('#recording-start')).toBeEnabled();
  await page.locator('#recording-start').click();
  await expect(page.locator('#global-recording-state')).toHaveText('Capture in progress');
  await page.locator('#recording-stop').click();
  await expect(page.locator('#global-recording-state')).toHaveText('Capture complete');
  expect(model.calls.some(call => call.path === '/api/state')).toBe(true);
  expect(model.calls.some(call => call.path === '/api/mavlink/messages')).toBe(true);
  expect(model.calls.filter(call => call.method === 'POST').map(call => call.path)).toEqual([
    '/api/recordings/start', '/api/recordings/stop',
  ]);
});
