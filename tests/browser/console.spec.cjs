const { test, expect, ID, recording, statusText, open, openArchive } = require('./fixtures.cjs');

test('navigation preserves a source draft and only requests live inspection while visible', async ({ page, model }) => {
  await open(page);
  expect(model.calls.filter(call => call.path === '/api/mavlink/messages')).toHaveLength(0);
  await page.locator('#sources-button').click();
  await page.locator('#config-tcp').fill('127.0.0.1:5777');
  await page.locator('#view-messages').click();
  await expect(page.locator('.live-type')).toHaveCount(18);
  await expect(page.locator('#messages-workspace')).toBeFocused();
  await page.locator('.live-freeze').click();
  const calls = model.calls.filter(call => call.path === '/api/mavlink/messages').length;
  model.field = 'new value';
  await page.waitForTimeout(650);
  expect(model.calls.filter(call => call.path === '/api/mavlink/messages')).toHaveLength(calls);
  await expect(page.locator('.live-status')).toContainText('Snapshot frozen');
  await expect(page.locator('.live-fields')).not.toContainText('new value');
  await page.keyboard.press('Escape');
  await expect(page.locator('#view-messages')).toBeFocused();
  await expect(page.locator('#config-tcp')).toHaveValue('127.0.0.1:5777');
  await page.locator('#view-sessions').click();
  const hidden = model.calls.filter(call => call.path === '/api/mavlink/messages').length;
  await page.waitForTimeout(650);
  expect(model.calls.filter(call => call.path === '/api/mavlink/messages')).toHaveLength(hidden);
});

test('a frozen service response cannot keep declarations or capture state current', async ({ page, model }) => {
  await open(page);
  await expect(page.locator('#armed-preview')).toHaveText('Armed reported');
  await expect(page.locator('#detail-system-status')).toHaveText('Active · reported');
  await expect(page.locator('#reception-state')).toHaveText('No reception incidents');
  model.frozen = true;
  await expect(page.locator('#service-status')).toHaveText('Service unreachable', {timeout: 5000});
  await expect(page.locator('#flight-mode')).toHaveText('—');
  await expect(page.locator('#armed-preview')).toHaveText('—');
  await expect(page.locator('#detail-system-status')).toHaveText('Not refreshed');
  await expect(page.locator('#global-recording-state')).toHaveText('Capture · not refreshed');
  model.frozen = false;
  await expect(page.locator('#service-status')).toHaveText('Service connected');
  await expect(page.locator('#armed-preview')).toHaveText('Armed reported');
});

test('capture start, global background error, and recovery are visible in every workspace', async ({ page, model }) => {
  await open(page);
  await page.locator('#global-recording').click();
  await page.locator('#recording-start').click();
  await expect(page.locator('#global-recording-state')).toHaveText('Capture in progress');
  await expect(page.locator('#recording-stop')).toBeFocused();
  for (const view of ['messages', 'sessions', 'observation']) {
    await page.locator(`#view-${view}`).click();
    await expect(page.locator('#global-recording')).toBeVisible();
    await expect(page.locator('#global-recording-state')).toHaveText('Capture in progress');
  }
  await page.locator('#view-messages').click();
  model.recording = recording({state: 'error', id: ID, events: 3, started_at: 100, ended_at: model.clock(), error: 'Disque plein'});
  await expect(page.locator('#global-recording-state')).toHaveText('Capture error');
  await expect(page.locator('#global-recording')).toHaveAttribute('data-tone', 'error');
  await page.locator('#global-recording').click();
  await expect(page.locator('body')).toHaveAttribute('data-view', 'observation');
  await expect(page.locator('#recording-hint')).toContainText('Disque plein');
  await expect(page.locator('#recording-download')).toBeHidden();
  await page.locator('#recording-start').click();
  await page.locator('#recording-stop').click();
  await expect(page.locator('#global-recording-state')).toHaveText('Capture complete');
  await expect(page.locator('#recording-download')).toHaveAttribute('href', `/api/recordings/${ID}/download`);
});

for (const [reason, label] of [['transport_error', 'Interrupted'], ['event_limit', 'Limit reached'], ['size_limit', 'Limit reached'], ['shutdown', 'Interrupted']]) {
  test(`valid capture closure remains downloadable: ${reason}`, async ({ page, model }) => {
    model.recording = recording({state: 'complete', id: ID, events: 3, started_at: 100, ended_at: 103,
      end_reason: reason, end_detail: 'Fin expliquée par le service.', download_url: `/api/recordings/${ID}/download`});
    model.metadata.end_reason = reason;
    model.metadata.end_detail = 'Fin expliquée par le service.';
    await open(page);
    await page.locator('#global-recording').click();
    await expect(page.locator('#detail-recording-state')).toHaveText(label);
    await expect(page.locator('#recording-hint')).toContainText('Fin expliquée par le service.');
    await expect(page.locator('#recording-download')).toBeVisible();
    await expect(page.locator('#recording-limits')).toContainText('100');
    await openArchive(page);
    await expect(page.locator('#replay-end-detail')).toContainText('Fin expliquée par le service.');
    await expect(page.locator('#archive-download')).toBeVisible();
  });
}

test('Pause keeps the confirmed cursor when an automatic replay reply arrives late', async ({ page, model }) => {
  await open(page); await openArchive(page);
  const hold = {when: (path, query) => path.endsWith('/replay') && Number(query.get('at')) > 0};
  model.hold = hold;
  await page.locator('#replay-speed').selectOption('4');
  await page.locator('#replay-play').click();
  await expect.poll(() => hold.requested).toBe(true);
  const confirmed = await page.locator('#replay-cursor').inputValue();
  const mode = await page.locator('#history-mode').innerText();
  await page.locator('#replay-play').click();
  hold.release();
  await page.waitForTimeout(200);
  await expect(page.locator('#replay-cursor')).toHaveValue(confirmed);
  await expect(page.locator('#history-mode')).toHaveText(mode);
  await expect(page.locator('#replay-play')).toHaveText('Play');
  await expect(page.locator('#replay-measures')).toHaveAttribute('aria-busy', 'false');
  await expect(page.locator('#replay-next')).toBeEnabled();
});

test('late live reply from an old connection cannot replace the new connection', async ({ page, model }) => {
  await open(page);
  const hold = {when: path => path === '/api/mavlink/messages'};
  model.hold = hold;
  await page.locator('#view-messages').click();
  await expect.poll(() => hold.requested).toBe(true);
  model.connection = 'connection-2'; model.field = 'New connection';
  await expect(page.locator('.live-fields')).toContainText('New connection');
  hold.release();
  await page.waitForTimeout(100);
  await expect(page.locator('.live-fields')).toContainText('New connection');
  await expect(page.locator('.live-fields')).toContainText('18446744073709551615');
  await expect(page.locator('.live-fields img')).toHaveCount(0);
  await page.locator('#live-source-filter').selectOption('1:42');
  await expect(page.locator('.live-type:visible')).toHaveCount(1);
  await expect(page.locator('.live-type[aria-pressed=true]')).toHaveAttribute('data-key', '1:42:30');
});

test('STATUSTEXT is opt-in, escaped, historical and explicit about incomplete chunks', async ({ page, model }) => {
  model.texts = [statusText({text: '<img src=x onerror=window.injected=true>', complete: false, reason: 'missing_chunk', connection_id: 'previous-connection', utf8_valid: false})];
  await open(page);
  await expect(page.locator('#autopilot-text-list')).toBeHidden();
  await page.locator('#autopilot-texts-summary').click();
  await expect(page.locator('#autopilot-text-list')).toContainText('<img src=x onerror=window.injected=true>');
  await expect(page.locator('#autopilot-text-list img')).toHaveCount(0);
  await expect(page.locator('#autopilot-text-list')).toContainText('Missing chunk');
  await expect(page.locator('#autopilot-text-list')).toContainText('previous connection');
  await expect(page.locator('#autopilot-text-list')).toContainText('Incomplete or invalid UTF-8');
  await expect(page.locator('#autopilot-texts-limits')).toContainText('60 texts');
  await page.screenshot({path: test.info().outputPath('observation-status.png')});
  model.offline = true;
  await expect(page.locator('#autopilot-text-list')).toContainText('Age not refreshed');
  await expect(page.locator('#autopilot-texts-state')).toContainText('Retained history');
});

test('source configuration explains persistence, numbering and declared environment', async ({ page, model }) => {
  model.modifyState = value => { value.environment = value.configuration.environment = 'real'; };
  await open(page);
  await expect(page.locator('#environment')).toHaveText('REPORTED AS REAL');
  await page.locator('#sources-button').click();
  await expect(page.locator('.source-persistence-note')).toContainText('the service restarts');
  await expect(page.locator('#config-scope')).toHaveAttribute('aria-describedby', 'config-scope-help');
  await expect(page.locator('#config-scope-help')).toContainText('does not prove radio loss');
});

test('archive analysis uses one-second buckets and keeps archive tabs separate', async ({ page, model }) => {
  await open(page); await openArchive(page);
  await page.locator('#recording-view-analysis').click();
  await expect(page.locator('.analysis-results')).toBeVisible();
  const calls = model.calls.filter(call => call.path.endsWith('/analysis'));
  expect(calls).toHaveLength(1);
  expect(calls[0].query.get('bins')).toBe('3');
  await expect(page.locator('.analysis-summary')).toContainText('1 Hz');
  await page.locator('#recording-view-messages').click();
  await expect(page.locator('#recording-message-view')).toBeVisible();
  await expect(page.locator('#recording-analysis-view')).toBeHidden();
});

for (const [width, height] of [[1366, 650], [1280, 600], [900, 500], [360, 640], [320, 568]]) {
  test(`live workspace remains readable at ${width}x${height}`, async ({ page, model }) => {
    await page.setViewportSize({width, height}); await open(page);
    await page.locator('#view-messages').click();
    await expect(page.locator('.live-type')).toHaveCount(18);
    await expect(page.locator('#global-recording')).toBeVisible();
    const geometry = await page.evaluate(() => {
      const rect = selector => { const element = document.querySelector(selector), r = element.getBoundingClientRect();
        return {top: r.top, bottom: r.bottom, height: r.height, width: r.width, position: getComputedStyle(element).position}; };
      document.querySelector('.live-type-list').scrollTop = 200;
      document.querySelector('.live-detail-pane').scrollTop = 150;
      return {toolbar: rect('.live-toolbar'), list: rect('.live-type-list'), detail: rect('.live-detail-pane'),
        width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth};
    });
    if (width === 1366 || width === 320) await page.screenshot({path: test.info().outputPath('live-workspace.png')});
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width);
    expect(geometry.toolbar.position).toBe('static');
    expect(geometry.toolbar.bottom).toBeLessThanOrEqual(geometry.list.top);
    if (width > 760) {
      expect(geometry.list.height).toBeGreaterThanOrEqual(height === 500 ? 150 : 240);
      expect(geometry.detail.height).toBeGreaterThanOrEqual(height - 240);
      expect(geometry.toolbar.bottom).toBeLessThanOrEqual(geometry.detail.top);
    } else {
      expect(geometry.list.height).toBeGreaterThan(40);
      await page.locator('.live-type').first().click();
      await expect(page.locator('.live-detail-pane')).toBeVisible();
      await page.locator('.live-detail-pane').evaluate(element => { element.scrollTop = 200; });
      await page.getByRole('button', {name: 'Received types', exact: true}).click();
      await page.locator('.live-type').first().click();
      expect(await page.locator('.live-detail-pane').evaluate(element => element.scrollTop)).toBe(0);
    }
  });
}
