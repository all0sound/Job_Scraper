const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(process.env.DASHBOARD_URL || 'http://localhost:8765/triage.html');
      await page.waitForSelector('#jobs .job');
      const cards = page.locator('#jobs .job');
      const count = await cards.count();
      assert(count >= 5, `Only ${count} jobs visible`);
      const urls = await cards.evaluateAll(nodes => nodes.map(node => node.dataset.url));
      assert.equal(new Set(urls).size, count, 'Duplicate jobs visible');
      assert.equal(await page.locator('.employment-column').count(), 2);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow');
      assert.deepEqual(errors, []);
      await page.screenshot({ path: `/tmp/job-scraper-${viewport.width}.png`, fullPage: true });
      console.log(JSON.stringify({ viewport, count, columns: 2, errors }));
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
