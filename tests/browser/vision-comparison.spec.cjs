const { test: base, expect } = require('@playwright/test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');

const templatePath = path.resolve(__dirname, '../../argos/perception/static/vision_comparison.html');
const detection = (track_id, box = [.2, .2, .3, .5]) => ({ track_id, confidence: .91, box });
const result = detections => ({ detections, inference_ms: 23.45, processing_ms: 27.8 });
function fixture() {
  const pairs = [
    { tiny: result([detection(7)]), s: result([detection(107, [.22, .2, .3, .5])]) },
    { tiny: result([]), s: result([detection(107), detection(108, [.7, .1, .2, .6])]) },
    { tiny: result([detection(8)]), s: result([detection(109)]) },
    { tiny: result([detection(90001, [.97, .02, .025, .9])]), s: result([]) },
  ];
  return { format: 'argos.vision-comparison-view', version: 1,
    source: { id: 'capture-test', environment: 'simulation', duration_s: 3, selected_frames: 4,
      archive_frame_rows: 9, truncated: false, skipped_counts: { duplicate_image: 5 } },
    models: { tiny: { label: 'YOLOX-Tiny', input_size: 416 }, s: { label: 'YOLOX-S', input_size: 640 } },
    summary: { variants: {
      tiny: { frame_count: 4, frames_with_detections: 3, frames_without_detections: 1 },
      s: { frame_count: 4, frames_with_detections: 3, frames_without_detections: 1 },
    } },
    frames: pairs.map((models, offset) => ({ index: [10, 12, 14, 20][offset],
      at_s: offset === 0 ? -.1 : offset * .5, available_at_s: offset * .5 + .03,
      segment: 0, sequence: offset + 1, video_id: 'camera-test', width: 640, height: 360,
      image: `images/${String(offset).padStart(6, '0')}.jpg`, models })),
    differences: [1, 3], export: { binding: 'archive and frame hashes verified' },
  };
}

const test = base.extend({
  comparison: async ({ page }, use) => {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'argos-comparison-browser-'));
    const nonLocal = [], writes = [], errors = [];
    page.on('request', request => {
      if (!/^(file|data|about):/.test(request.url())) nonLocal.push(request.url());
      if (request.method() !== 'GET') writes.push([request.method(), request.url()]);
    });
    page.on('pageerror', error => errors.push(error.message));
    const images = await page.evaluate(() => {
      const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 360;
      const context = canvas.getContext('2d');
      return ['#78484a', '#47715d', '#4a577c', '#776344'].map(color => {
        context.fillStyle = color; context.fillRect(0, 0, 640, 360);
        context.fillStyle = '#d9d3c5'; context.fillRect(128, 72, 192, 180);
        return canvas.toDataURL('image/jpeg').split(',')[1];
      });
    });
    async function open(data = fixture(), { missing = [], delayed = [] } = {}) {
      await fs.mkdir(path.join(directory, 'images'), { recursive: true });
      for (let offset = 0; offset < images.length; offset += 1) {
        if (!missing.includes(offset)) await fs.writeFile(path.join(directory, 'images', `${String(offset).padStart(6, '0')}.jpg`), Buffer.from(images[offset], 'base64'));
      }
      if (delayed.length) {
        // Delay completion callbacks of actual local image elements. No fake
        // dimensions/data or HTTP server stand in for the JPEG decode.
        await page.addInitScript(indices => {
          const OriginalImage = window.Image;
          window.imageReleases = [];
          window.Image = function () {
            const image = new OriginalImage();
            for (const eventName of ['load', 'error']) {
              let handler;
              Object.defineProperty(image, `on${eventName}`, { configurable: true,
                get: () => handler, set: value => { handler = value; } });
              image.addEventListener(eventName, event => {
                const invoke = () => handler?.call(image, event);
                if (indices.some(index => image.src.endsWith(`/images/${String(index).padStart(6, '0')}.jpg`))) {
                  window.imageReleases.push(invoke);
                } else invoke();
              });
            }
            return image;
          };
        }, delayed);
      }
      const template = await fs.readFile(templatePath, 'utf8');
      expect(template.split('__ARGOS_COMPARISON_DATA__')).toHaveLength(2);
      const json = JSON.stringify(data).replace(/[<>&\u2028\u2029]/g,
        character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
      await fs.writeFile(path.join(directory, 'index.html'), template.replace('__ARGOS_COMPARISON_DATA__', json));
      await page.goto(pathToFileURL(path.join(directory, 'index.html')).href);
    }
    async function hashes() {
      const files = ['index.html', ...(await fs.readdir(path.join(directory, 'images'))).map(name => `images/${name}`)];
      return Promise.all(files.map(async name => [name,
        crypto.createHash('sha256').update(await fs.readFile(path.join(directory, name))).digest('hex')]));
    }
    try { await use({ directory, open, hashes, nonLocal, writes, errors }); }
    finally { await fs.rm(directory, { recursive: true, force: true }); }
  },
});

async function ready(page, counter) {
  await expect(page.locator('#frame-counter')).toHaveText(counter);
  await expect(page.locator('#frame-status')).toHaveText('Same archived image ready in both panels');
  await expect(page.locator('.frame-image')).toHaveCount(2);
}

test('standalone file view stays local and read-only while showing paired results', async ({ page, comparison }) => {
  await comparison.open();
  await ready(page, '1 / 4');
  const hashes = await comparison.hashes();
  await expect(page.locator('#tiny-count')).toHaveText('1 detection');
  await expect(page.locator('#s-count')).toHaveText('1 detection');
  await expect(page.locator('#tiny-inference')).toHaveText('23.4 ms');
  await expect(page.locator('#summary-tiny-count')).toHaveText('3 / 4');
  await expect(page.locator('#summary-differences')).toHaveText('2 / 4 images');
  await expect(page.locator('#frame-meta')).toContainText('Replay cursor 0.030 s');
  await expect(page.locator('#frame-meta')).not.toContainText('-0.1');
  await expect(page.locator('#binding-description')).toHaveText('archive and frame hashes verified');
  await expect(page.locator('#tiny-detections')).toHaveText('#7 · 91%');
  await expect(page.locator('#s-detections')).toHaveText('#107 · 91%');
  expect(await page.locator('.frame-image').evaluateAll(images => images[0].src === images[1].src)).toBe(true);
  await page.locator('#next-frame').click();
  await ready(page, '2 / 4');
  await page.locator('#show-boxes').uncheck();
  expect(comparison.nonLocal).toEqual([]);
  expect(comparison.writes).toEqual([]);
  expect(comparison.errors).toEqual([]);
  expect(await comparison.hashes()).toEqual(hashes);
});

test('count jumps skip equal counts even when boxes differ and stop at boundaries', async ({ page, comparison }) => {
  await comparison.open();
  await ready(page, '1 / 4');
  await expect(page.locator('#count-state')).toHaveText('Same detection count');
  await expect(page.locator('#previous-frame')).toBeDisabled();
  await expect(page.locator('#previous-difference')).toBeDisabled();
  await page.getByRole('button', { name: 'Next count disagreement' }).click();
  await ready(page, '2 / 4');
  await expect(page.locator('#tiny-count')).toHaveText('0 detections');
  await expect(page.locator('#s-count')).toHaveText('2 detections');
  await expect(page.locator('#count-state')).toHaveText('Different detection counts');
  await page.locator('#next-difference').click();
  await ready(page, '4 / 4');
  await expect(page.locator('#next-frame')).toBeDisabled();
  await expect(page.locator('#next-difference')).toBeDisabled();
  await page.locator('#previous-difference').click();
  await ready(page, '2 / 4');
  await page.keyboard.press('ArrowRight');
  await ready(page, '3 / 4');
  await page.keyboard.press('ArrowLeft');
  await ready(page, '2 / 4');
  await page.locator('#frame-slider').fill('0');
  await ready(page, '1 / 4');
});

test('hide-boxes removes actual SVG visibility through navigation and resize', async ({ page, comparison }) => {
  await comparison.open();
  await ready(page, '1 / 4');
  for (const overlay of await page.locator('.overlay').all()) await expect(overlay).toBeVisible();
  await page.locator('#show-boxes').uncheck();
  for (const overlay of await page.locator('.overlay').all()) await expect(overlay).toBeHidden();
  await page.locator('#next-frame').click();
  await ready(page, '2 / 4');
  await page.setViewportSize({ width: 390, height: 844 });
  for (const overlay of await page.locator('.overlay').all()) await expect(overlay).toBeHidden();
  await page.locator('#show-boxes').check();
  for (const overlay of await page.locator('.overlay').all()) await expect(overlay).toBeVisible();
});

for (const width of [1440, 390]) {
  test(`paired image geometry and edge labels remain aligned at ${width}px`, async ({ page, comparison }, testInfo) => {
    await page.setViewportSize({ width, height: 950 });
    await comparison.open();
    await ready(page, '1 / 4');
    for (const variant of ['tiny', 's']) {
      const pane = page.locator(`[data-variant="${variant}"]`);
      const image = await pane.locator('.frame-image').boundingBox();
      const svg = await pane.locator('.overlay').boundingBox();
      const box = await pane.locator('.detection-box').boundingBox();
      expect(image.width / image.height).toBeCloseTo(640 / 360, 2);
      expect(svg).toEqual(image);
      // Playwright includes the 1.5 CSS-pixel non-scaling outline in the box.
      expect(box.x - image.x + .75).toBeCloseTo(image.width * (variant === 'tiny' ? .2 : .22), 0);
      expect(box.y - image.y + .75).toBeCloseTo(image.height * .2, 0);
      expect(box.width - 1.5).toBeCloseTo(image.width * .3, 0);
      expect(box.height - 1.5).toBeCloseTo(image.height * .5, 0);
    }
    const tiny = await page.locator('[data-variant=tiny]').boundingBox();
    const small = await page.locator('[data-variant=s]').boundingBox();
    if (width === 1440) expect(tiny.y).toEqual(small.y);
    else expect(small.y).toBeGreaterThan(tiny.y + tiny.height);
    await page.locator('#frame-slider').fill('3');
    await ready(page, '4 / 4');
    const image = await page.locator('[data-variant=tiny] .frame-image').boundingBox();
    const label = await page.locator('[data-variant=tiny] .box-label').boundingBox();
    expect(label.x).toBeGreaterThanOrEqual(image.x);
    expect(label.y).toBeGreaterThanOrEqual(image.y);
    expect(label.x + label.width).toBeLessThanOrEqual(image.x + image.width);
    expect(label.y + label.height).toBeLessThanOrEqual(image.y + image.height);
    expect(label.height).toBeGreaterThan(9);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`comparison-${width}.png`), fullPage: true });
  });
}

for (const failure of [false, true]) {
  test(`late ${failure ? 'failed' : 'successful'} image cannot replace a newer pair`, async ({ page, comparison }) => {
    await comparison.open(fixture(), { delayed: [1], missing: failure ? [1] : [] });
    await ready(page, '1 / 4');
    await page.locator('#next-frame').click();
    await expect(page.locator('#frame-status')).toHaveText('Loading image 2…');
    await expect(page.locator('.frame-image')).toHaveCount(0);
    await expect(page.locator('.detection-box')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => window.imageReleases.length)).toBe(2);
    await page.locator('#next-frame').click();
    await ready(page, '3 / 4');
    await page.evaluate(() => window.imageReleases.splice(0).forEach(release => release()));
    await ready(page, '3 / 4');
    await expect(page.locator('[data-variant=tiny] .detection-box')).toHaveAttribute('data-track-id', '8');
    await expect(page.locator('[data-variant=s] .detection-box')).toHaveAttribute('data-track-id', '109');
    for (const image of await page.locator('.frame-image').all()) await expect(image).toHaveAttribute('src', 'images/000002.jpg');
    expect(comparison.errors).toEqual([]);
  });
}

test('current image error clears both panes and navigation can recover', async ({ page, comparison }) => {
  await comparison.open(fixture(), { missing: [1] });
  await ready(page, '1 / 4');
  await page.locator('#next-frame').click();
  await expect(page.locator('#frame-status')).toContainText('Image 2 unavailable');
  await expect(page.locator('.frame-image')).toHaveCount(0);
  await expect(page.locator('.detection-box')).toHaveCount(0);
  for (const message of await page.locator('.pane-message').all()) await expect(message).toHaveText('Image unavailable');
  await page.locator('#next-frame').click();
  await ready(page, '3 / 4');
  await expect(page.locator('#frame-status')).toHaveAttribute('data-error', 'false');
});

test('metadata stays text and remote image paths are rejected before any request', async ({ page, comparison }) => {
  const data = fixture();
  data.source.id = '<img src="https://example.invalid/track" onerror="alert(1)">';
  await comparison.open(data);
  await ready(page, '1 / 4');
  await expect(page.locator('#source-id')).toContainText('<img src=');
  await expect(page.locator('#source-id img')).toHaveCount(0);
  expect(comparison.nonLocal).toEqual([]);
  data.frames[1].image = 'https://example.invalid/track.jpg';
  await comparison.open(data);
  await expect(page.locator('#fatal-error')).toContainText('Invalid archived image metadata');
  await expect(page.locator('.frame-image')).toHaveCount(0);
  await expect(page.locator('#next-frame')).toBeDisabled();
  expect(comparison.nonLocal).toEqual([]);
});

test('empty export displays no samples and disables all navigation', async ({ page, comparison }) => {
  const data = fixture(); data.frames = []; data.differences = [];
  await comparison.open(data);
  await expect(page.locator('#frame-status')).toHaveText('No paired images to inspect');
  await expect(page.locator('#frame-counter')).toHaveText('0 / 0');
  for (const id of ['previous-frame', 'next-frame', 'previous-difference', 'next-difference', 'frame-slider']) {
    await expect(page.locator(`#${id}`)).toBeDisabled();
  }
});
