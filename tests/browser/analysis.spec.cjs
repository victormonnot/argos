const { test, expect, ID, open, openArchive } = require('./fixtures.cjs');

async function openAnalysis(page) {
  await open(page); await openArchive(page);
  await page.locator('#recording-view-analysis').click();
  await expect(page.locator('.analysis-results')).toBeVisible();
}

const requests = model => model.calls.filter(call => call.path.endsWith('/analysis'));

for (const transition of ['workspace', 'browser tab']) {
  for (const draft of ['2', '0']) {
    test(`an unapplied ${draft === '0' ? 'invalid' : 'valid'} threshold survives leaving the ${transition}`, async ({ page, model }) => {
      await openAnalysis(page);
      const threshold = page.locator('#analysis-gap-threshold');
      const message = draft === '0' ? 'The threshold must be between' : 'Apply the threshold';
      await threshold.fill(draft);
      await expect(page.locator('#analysis-status')).toContainText(message);
      if (transition === 'workspace') {
        await page.locator('#recording-view-measures').click();
        await page.locator('#recording-view-analysis').click();
      } else {
        for (const hidden of [true, false]) await page.evaluate(hidden => {
          Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
          document.dispatchEvent(new Event('visibilitychange'));
        }, hidden);
      }
      await expect(threshold).toHaveValue(draft);
      await expect(page.locator('#analysis-status')).toContainText(message);
      await expect(page.locator('.analysis-results')).toBeHidden();
      expect(requests(model).map(call => call.query.get('gap_threshold_s'))).toEqual(['1']);
      if (draft === '0') {
        await page.getByRole('button', { name: 'Apply', exact: true }).click();
        expect(await threshold.evaluate(input => input.checkValidity())).toBe(false);
        expect(requests(model)).toHaveLength(1);
        await threshold.fill('2');
      }
      await page.getByRole('button', { name: 'Apply', exact: true }).click();
      await expect(page.locator('.analysis-results')).toBeVisible();
      await expect(page.locator('.analysis-gaps')).toContainText('at least 2 s');
      expect(requests(model).map(call => call.query.get('gap_threshold_s'))).toEqual(['1', '2']);
      expect(model.calls.some(call => call.method !== 'GET')).toBe(false);
    });
  }
}

test('changing analysis filters keeps a threshold draft pending until Apply', async ({ page, model }) => {
  await openAnalysis(page);
  await page.locator('#analysis-gap-threshold').fill('2');
  await page.locator('#analysis-source').selectOption('1:1');
  await page.locator('#analysis-type').selectOption('0');
  await expect(page.locator('#analysis-status')).toContainText('Apply the threshold');
  await expect(page.locator('.analysis-results')).toBeHidden();
  expect(requests(model)).toHaveLength(1);
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(page.locator('.analysis-results')).toBeVisible();
  expect(requests(model)).toHaveLength(2);
  expect(Object.fromEntries(requests(model)[1].query)).toMatchObject({
    gap_threshold_s: '2', system: '1', component: '1', message_id: '0',
  });
  // Source/type filters still apply immediately once the threshold is committed.
  await page.locator('#analysis-source').selectOption('');
  await expect(page.locator('.analysis-results')).toBeVisible();
  expect(requests(model)).toHaveLength(3);
  expect(requests(model)[2].query.has('system')).toBe(false);
  expect(requests(model)[2].query.get('gap_threshold_s')).toBe('2');
  expect(model.calls.some(call => call.method !== 'GET')).toBe(false);
});

test('reopening a recording preserves the visible threshold draft without applying it', async ({ page, model }) => {
  await openAnalysis(page);
  await page.locator('#analysis-gap-threshold').fill('2');
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#replay-play')).toBeEnabled();
  await page.locator('#recording-view-analysis').click();
  await expect(page.locator('#analysis-gap-threshold')).toHaveValue('2');
  await expect(page.locator('#analysis-status')).toContainText('Apply the threshold');
  await expect(page.locator('.analysis-results')).toBeHidden();
  expect(requests(model)).toHaveLength(1);
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await expect(page.locator('.analysis-results')).toBeVisible();
  expect(requests(model).map(call => call.query.get('gap_threshold_s'))).toEqual(['1', '2']);
});

test('analysis without a threshold draft still reloads on return and retries a failed request', async ({ page, model }) => {
  await openAnalysis(page);
  let fail = true;
  await page.route(`**/api/recordings/${ID}/analysis?*`, route => {
    if (!fail) return route.fallback();
    fail = false;
    return route.fulfill({ status: 503, json: { detail: 'Archive temporarily unavailable' } });
  });
  await page.locator('#recording-view-measures').click();
  await page.locator('#recording-view-analysis').click();
  await expect(page.locator('#analysis-status')).toHaveText('Archive temporarily unavailable');
  await expect(page.locator('.analysis-results')).toBeHidden();
  await page.getByRole('button', { name: 'Retry analysis' }).click();
  await expect(page.locator('.analysis-results')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry analysis' })).toBeHidden();
  expect(requests(model).map(call => call.query.get('gap_threshold_s'))).toEqual(['1', '1']);
  expect(model.calls.some(call => call.method !== 'GET')).toBe(false);
});
