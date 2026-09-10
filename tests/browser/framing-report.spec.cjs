const { test, expect, ID, REVISION, open } = require('./fixtures.cjs');
const VISUAL = 'c'.repeat(64);

function report(visual = VISUAL) {
  const context = { profile: 'full', range_response: 'normal', target_id: 7, reference_height: .3 };
  const interval = (start_s, end_s, state, extra = {}) => ({ start_s, end_s, state, ...context, reason: '', ...extra });
  const point = (at_s, segment, extra = {}) => ({ at_s, segment, valid: true, error_x: .1, error_y: null, height: .33, size_error: .1, ...context, ...extra });
  return { state: 'complete', id: ID, revision: REVISION, visual_revision: visual, duration_s: 3,
    coverage: { sampled_s: 2.5, unknown_s: .5, metric_s: .9, samples: 6, metric_samples: 4 },
    durations_s: { manual: 1, full: .3, pilot_throttle: .6, unknown_profile: 0, paused: .3, takeover: 0, inactive: .3, unknown: .5 },
    intervals: [interval(0, .3, 'inactive', { profile: null, range_response: null, target_id: null, reference_height: null }), interval(.3, .6, 'full'),
      interval(.6, .9, 'paused', { reason: 'Selected target confidence is too low' }), interval(.9, 1.5, 'pilot_throttle', { profile: 'pilot_throttle', range_response: 'gentle' }),
      interval(1.5, 2, 'unknown', { profile: null, range_response: null, target_id: null, reference_height: null, reason: 'No recent recorded control sample' }),
      interval(2, 3, 'manual', { profile: null, range_response: null, target_id: null, reference_height: null })],
    series: [point(.35, 1, { error_x: .25 }), point(.55, 1, { error_x: .05 }), point(.75, 2, { valid: false, error_x: null, error_y: null, height: null, size_error: null }),
      point(1, 3, { error_x: -.1, size_error: -.1, height: .27, profile: 'pilot_throttle', range_response: 'gentle' }),
      point(1.4, 3, { error_x: .01, size_error: 0, height: .3, profile: 'pilot_throttle', range_response: 'gentle' }),
      point(2.1, 4, { valid: false, error_x: null, height: null, size_error: null, profile: null, range_response: null, target_id: null, reference_height: null })],
    intervals_count: 6, intervals_truncated: false, series_count: 6, series_downsampled: false,
    events: [{ at_s: .6, index: 1, kind: 'framing', detail: 'Selected target confidence is too low', status: null },
      { at_s: 1.5, index: 'interruption-4', kind: 'interruption', detail: 'Control released <img src=x onerror=window.injected=true>', status: 'recorded' }],
    events_count: 2, events_truncated: false, interruptions_count: 1,
    limits: { series: 1200, intervals: 1500, events: 500, sample_age_s: .35, video_age_s: 1 } };
}
async function install(page, model) {
  const mock = { reports: [], replays: [], value: report(), failure: null, hold: null };
  model.metadata.visual = { state: 'complete', revision: VISUAL, frames: 0, samples: 6, events: 2, duration_s: 3 };
  await page.route(/\/api\/recordings\/[a-f0-9]{32}\/framing-report\?/, async route => {
    const url = new URL(route.request().url());
    mock.reports.push({ method: route.request().method(), query: Object.fromEntries(url.searchParams) });
    const value = structuredClone(mock.value), failure = mock.failure;
    const hold = mock.hold;
    if (hold) {
      mock.hold = null; hold.requested = true; model.pending.add(hold);
      await new Promise(resolve => { hold.release = resolve; }); model.pending.delete(hold);
    }
    await route.fulfill(failure ? { status: 409, json: { detail: failure } } : { json: value }).catch(() => {});
  });
  await page.route(/\/api\/recordings\/[a-f0-9]{32}\/visual\?/, async route => {
    const url = new URL(route.request().url()), at = Number(url.searchParams.get('at'));
    mock.replays.push({ method: route.request().method(), at });
    await route.fulfill({ json: { id: ID, revision: REVISION, visual_revision: url.searchParams.get('visual_revision'), at_s: at, state: 'recent',
      sample: { at_s: at }, control: { phase: 'armed', vehicle: { armed: true, mode: 2 }, framing: { phase: 'active', active: true, profile: 'full', range_response: 'normal' } },
      frame: null, events: [] } });
  });
  return mock;
}
async function openFlight(page) {
  await open(page); await page.locator('#view-sessions').click(); await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#replay-play')).toBeEnabled();
}
async function expandReport(page) {
  await page.locator('#framing-report>summary').click();
  await expect(page.locator('.framing-report-results')).toBeVisible();
}

test('framing report loads lazily with archive revisions, explains coverage and seeks only recorded flight', async ({ page, model }) => {
  const mock = await install(page, model);
  await openFlight(page);
  expect(mock.reports).toHaveLength(0);
  await expect(page.locator('#flight-replay-response')).toHaveText('Normal');
  await expandReport(page);
  expect(mock.reports).toEqual([{ method: 'GET', query: { revision: REVISION, visual_revision: VISUAL } }]);
  await expect(page.locator('.framing-report-summary [data-state=pilot_throttle]')).toContainText('0.6 s');
  await expect(page.locator('#framing-report-coverage')).toContainText('0.5 s unobserved / unknown');
  await expect(page.locator('.framing-report-results')).toContainText('Vertical centering is not scored');
  await page.locator('.framing-report-intervals>summary').click();
  await expect(page.locator('#framing-report-interval-list')).toContainText('Profile not recorded · Response not recorded');
  await page.locator('#framing-report-interval-list button[data-state=paused]').click();
  await expect(page.locator('#replay-cursor')).toHaveValue('0.6');
  await expect(page.locator('#framing-report-readout')).toContainText('Selected target confidence is too low');
  await page.locator('.framing-report-event-details>summary').click();
  await expect(page.locator('#framing-report-event-list button').first().locator('.microcopy')).toHaveText('');
  await page.locator('#framing-report-event-list button').last().click();
  await expect(page.locator('#replay-cursor')).toHaveValue('1.5');
  await expect(page.locator('#framing-report-readout')).toContainText('Unobserved / unknown');
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  expect(mock.replays.every(item => item.method === 'GET')).toBe(true);
  expect(model.calls.some(item => item.method === 'POST')).toBe(false);
  expect(mock.reports).toHaveLength(1);
});

test('curves separate pauses and profile changes; pointer and keyboard inspection seek the same replay', async ({ page, model }) => {
  const mock = await install(page, model); await openFlight(page); await expandReport(page);
  const horizontal = page.locator('.framing-report-plot').first();
  await expect(horizontal.locator('path.framing-report-line')).toHaveCount(2);
  await expect(page.locator('.framing-report-plot').last().locator('path.framing-report-line')).toHaveCount(2);
  const chart = horizontal.locator('svg'); const bounds = await chart.boundingBox();
  const viewWidth = await chart.evaluate(element => element.viewBox.baseVal.width);
  await chart.click({ position: { x: bounds.width * (64 + (viewWidth - 86) / 3) / viewWidth, y: bounds.height / 2 } });
  await expect.poll(async () => Number(await page.locator('#replay-cursor').inputValue())).toBeCloseTo(1, 1);
  await page.locator('#framing-report-time').fill('1.4');
  await page.locator('#framing-report-time').dispatchEvent('input');
  await expect(page.locator('#framing-report-readout')).toContainText('Manual throttle · Gentle');
  await expect(page.locator('#framing-report-readout')).toContainText('size error 0%');
  await page.getByRole('button', { name: 'View this moment in replay' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#replay-cursor')).toHaveValue('1.4');
  const count = mock.replays.length;
  await page.locator('#view-observation').click();
  await page.evaluate(({ id, revision, visual_revision }) => document.dispatchEvent(new CustomEvent('argos:archive-framing-seek', { detail: { id, revision, visual_revision, at_s: 2 } })), { id: ID, revision: REVISION, visual_revision: VISUAL });
  expect(mock.replays).toHaveLength(count);
});

test('an old pending report cannot replace a reopened visual revision', async ({ page, model }) => {
  const mock = await install(page, model); await openFlight(page);
  const hold = {}; mock.hold = hold;
  await page.locator('#framing-report>summary').click();
  await expect.poll(() => hold.requested).toBe(true);
  const nextVisual = 'd'.repeat(64); model.metadata.visual.revision = nextVisual;
  mock.value = report(nextVisual); mock.value.events[0].detail = 'New archive revision';
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('.framing-report-results')).toBeVisible();
  await page.locator('.framing-report-event-details>summary').click();
  await expect(page.locator('#framing-report-event-list')).toContainText('New archive revision');
  hold.release(); await page.waitForTimeout(100);
  await expect(page.locator('#framing-report-event-list')).toContainText('New archive revision');
  expect(mock.reports[1].query.visual_revision).toBe(nextVisual);
});

test('unavailable reports can retry; a wrong binding is rejected without retaining previous results', async ({ page, model }) => {
  const mock = await install(page, model); mock.failure = 'The recording changed; reopen it.';
  await openFlight(page); await page.locator('#framing-report>summary').click();
  await expect(page.locator('#framing-report-status')).toHaveText(mock.failure);
  await expect(page.locator('.framing-report-results')).toBeHidden();
  await page.locator('#replay-play').click();
  await expect.poll(async () => Number(await page.locator('#replay-cursor').inputValue())).toBeGreaterThan(.4);
  expect(mock.reports).toHaveLength(1);
  await page.locator('#replay-play').click();
  mock.failure = null;
  await page.getByRole('button', { name: 'Retry report' }).click();
  await expect(page.locator('.framing-report-results')).toBeVisible();
  model.metadata.visual.revision = 'd'.repeat(64);
  await page.locator(`#archive-list button[data-id="${ID}"]`).click();
  await expect(page.locator('#framing-report-status')).toContainText('Invalid framing report response');
  await expect(page.locator('.framing-report-results')).toBeHidden();
});

test('a video-only legacy archive does not invent framing measurements and fits a touch viewport', async ({ page, model }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await install(page, model);
  mock.value.coverage = { sampled_s: 0, unknown_s: 3, metric_s: 0, samples: 0, metric_samples: 0 };
  mock.value.durations_s = Object.fromEntries(Object.keys(mock.value.durations_s).map(key => [key, key === 'unknown' ? 3 : 0]));
  mock.value.intervals = [{ start_s: 0, end_s: 3, state: 'unknown', profile: null, range_response: null, target_id: null, reference_height: null, reason: 'No recent recorded control sample' }];
  mock.value.series = []; mock.value.series_count = 0; mock.value.intervals_count = 1;
  mock.value.events = []; mock.value.events_count = 0;
  await openFlight(page); await expandReport(page);
  await expect(page.locator('#framing-report-status')).toContainText('No control samples');
  await expect(page.locator('.framing-report-line')).toHaveCount(0);
  await expect(page.locator('.framing-report-plot').first()).toContainText('No valid recorded measurements');
  await expect(page.locator('#framing-report-readout')).toContainText('Profile not recorded');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const readable = await page.locator('.framing-report-plot').first().locator('svg').evaluate(element => ({ width: element.getBoundingClientRect().width, viewBox: element.viewBox.baseVal.width, fontSize: Number.parseFloat(getComputedStyle(element.querySelector('text')).fontSize) }));
  expect(readable.fontSize * readable.width / readable.viewBox).toBeGreaterThanOrEqual(10);
});
