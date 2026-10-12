const fs = require('node:fs');
const path = require('node:path');
const { test, expect, ID } = require('./fixtures.cjs');
const index = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/index.html'), 'utf8');
const welcome = fs.readFileSync(path.resolve(__dirname, '../../argos/console/static/demo-welcome.html'), 'utf8');

async function demoPages(page) {
  await page.route('http://127.0.0.1:4173/', route => route.fulfill({ contentType: 'text/html', body: welcome }));
  await page.route('http://127.0.0.1:4173/demo', route => route.fulfill({ contentType: 'text/html',
    body: index.replace('<body data-view="observation">', '<body data-demo="true" data-view="observation">') }));
}

for (const viewport of [{ width: 1366, height: 850 }, { width: 390, height: 844 }]) {
  test(`welcome explains the demo beside its entry at ${viewport.width}px`, async ({ page, model }) => {
    await page.setViewportSize(viewport);
    await demoPages(page);
    await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
    await page.goto('/');
    const enter = page.getByRole('link', { name: 'Try ARGOS' });
    await expect(enter).toBeVisible();
    await expect(enter).toHaveAttribute('href', '/demo');
    await expect(page.locator('#demo-limits')).toContainText('Demo mode with selected features');
    await expect(page.locator('#demo-limits')).toContainText('Live flying and recording are unavailable');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('welcome.png'), fullPage: true });
    expect(model.calls).toEqual([]);
    await enter.click();
    await expect(page.locator('#replay-play')).toBeEnabled();
    await expect(page.locator('#service-status')).toHaveText('Recorded simulation');
    await expect(page.locator('#demo-home')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('console.png'), fullPage: true });
    await page.locator('#demo-home').click();
    await expect(enter).toBeVisible();
  });
}

test('hosted entry opens the archive and keeps replay, messages and analysis read-only', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: ID } }));
  await page.goto('/demo');
  await expect(page.locator('body')).toHaveAttribute('data-view', 'sessions');
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator(`#archive-list button[data-id="${ID}"]`)).toHaveAttribute('aria-pressed', 'true');
  for (const selector of ['#view-observation', '#view-control', '#view-messages', '#sources-button', '#global-recording', '#archive-journal']) {
    await expect(page.locator(selector)).toBeHidden();
  }
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
  await expect(page.locator('#replay-status')).toContainText('could not be loaded');
  expect(model.calls).toEqual([]);
  available = true;
  await page.locator('#demo-retry').click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await expect(page.locator('#demo-retry')).toBeHidden();
  expect(model.calls.every(call => call.method === 'GET' && call.path.startsWith('/api/recordings'))).toBe(true);
});

test('an invalid demo configuration never opens an arbitrary recording or live workspace', async ({ page, model }) => {
  await demoPages(page);
  await page.route('**/api/demo-config', route => route.fulfill({ json: { enabled: true, recording_id: '../state' } }));
  await page.goto('/demo');
  await expect(page.locator('#demo-retry')).toBeVisible();
  await expect(page.locator('#replay-content')).toBeHidden();
  expect(model.calls).toEqual([]);
});
