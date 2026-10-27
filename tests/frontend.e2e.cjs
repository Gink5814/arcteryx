/* Browser integration test. All quotes, identities and alerts are SYNTHETIC.
 * Runs the repository's real HTML/CSS/JS against a loopback HTTP fixture.
 * Never connects to a retailer, sends notifications or opens the working DB.
 * Requires the already available playwright package and Chromium.
 * Run: node tests/frontend.e2e.cjs
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

const assets = path.resolve(__dirname, '../src/arcteryx_prices/static');
const token = 'synthetic-browser-test-csrf';
const fixture = { watches: [], alerts: [], historical_evidence: [], offline: false, requests: [], failures: [] };
const checks = new Map();
const stamp = '2026-10-26T12:00:00Z';

function metrics(price, days = ['2026-10-26']) {
  return {
    as_of: stamp,
    history: { first_observed_at: price == null ? null : `${days[0]}T12:00:00Z` },
    observed_all_time_min: price == null ? null : { price_minor: price },
    lowest_365d: price == null ? null : { price_minor: price },
    coverage_365d: { observed_days: price == null ? 0 : days.length },
    average_daily_low_90d: {
      display_minor: price, observed_days: price == null ? 0 : days.length,
      complete_day_coverage: false,
      daily_minima: price == null ? [] : days.map(day => ({ day, price_minor: price })),
    },
  };
}

function watch(id, market, price, label) {
  return {
    id, url: `https://arcteryx.com/${market.toLowerCase()}/en/shop/mens/synthetic-browser-fixture`,
    label, sku: 'SYNTHETIC-SAME-SKU', color: 'Synthetic Black', size: 'M',
    market, currency: market === 'CA' ? 'CAD' : 'USD', enabled: true,
    retailer_key: 'arcteryx-official', retailer_name: "Arc'teryx official",
    rule: 'target', target_price_minor: 12345, interval_minutes: 60,
    created_at: `2026-10-${23 + id}T12:00:00Z`,
    current_price_minor: price, availability: price == null ? 'unknown' : 'in_stock',
    status: price == null ? 'never_checked' : 'ok', stale: price == null,
    last_success_at: price == null ? null : stamp,
    last_checked_at: price == null ? null : stamp,
    metrics: metrics(price),
  };
}

async function handler(req, res) {
  const headers = {
    'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'",
  };
  function send(status, value) { res.writeHead(status, headers); res.end(JSON.stringify(value)); }
  if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
  if (req.method === 'GET' && req.url === '/api/state') {
    if (fixture.offline) return send(503, { error: 'SYNTHETIC test service unavailable' });
    return send(200, { watches: fixture.watches, alerts: fixture.alerts,
      historical_evidence: fixture.historical_evidence, csrf_token: token,
      supported_retailers: [
        // Deliberately emulate a legacy API cache; the UI must narrow its scope.
        { key: 'arcteryx-official', name: "Arc'teryx official", markets: ['US', 'CA'] },
        { key: 'rei', name: 'REI Co-op', markets: ['US'] },
      ],
      scope: ['US/USD', 'CA/CAD'], notifications: 'in_app' });
  }
  if (req.method === 'GET') {
    const name = { '/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css' }[req.url];
    if (!name) return send(404, { error: 'Synthetic fixture route not found' });
    res.writeHead(200, { ...headers, 'Content-Type': name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' });
    return res.end(fs.readFileSync(path.join(assets, name)));
  }
  assert.equal(req.headers['x-csrf-token'], token, 'UI mutation must retain CSRF protection');
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = JSON.parse(raw);
  fixture.requests.push({ method: req.method, url: req.url, body });
  if (req.method === 'POST' && req.url === '/api/discover') {
    const rei = body.url.includes('www.rei.com/');
    return send(200, {
      url: body.url, label: rei ? 'SYNTHETIC REI SHELL' : 'SYNTHETIC US SHELL', market: 'US', currency: 'USD',
      variants: [{ sku: 'SYNTHETIC-SAME-SKU', color: 'Synthetic Black', size: 'M',
        price_minor: 12000, availability: 'in_stock' }],
    });
  }
  if (req.method === 'POST' && req.url === '/api/watches') {
    const market = body.url.includes('/ca/') ? 'CA' : 'US';
    const nextId = Math.max(0, ...fixture.watches.map(item => item.id)) + 1;
    const value = { ...watch(nextId, market, null, body.label), ...body };
    if (body.url.includes('www.rei.com/')) Object.assign(value, { retailer_key: 'rei', retailer_name: 'REI Co-op' });
    fixture.watches.push(value);
    return send(201, value);
  }
  const match = req.url.match(/^\/api\/watches\/(\d+)(\/check)?$/);
  if (match) {
    const value = fixture.watches.find(item => item.id === Number(match[1]));
    assert.ok(value, 'fixture mutation identifies an existing watch');
    if (req.method === 'PATCH' && !match[2]) {
      Object.assign(value, body);
      if (!value.enabled) { value.stale = true; value.current_price_minor = null; }
      return send(200, value);
    }
    if (req.method === 'POST' && match[2]) {
      const count = (checks.get(value.id) || 0) + 1;
      checks.set(value.id, count);
      if (count === 1) {
        Object.assign(value, { status: 'ok', stale: false, current_price_minor: 12000,
          availability: 'in_stock', last_success_at: stamp, last_checked_at: stamp,
          metrics: metrics(12000) });
        // The legacy backend alert table did not carry a market column.
        fixture.alerts.push({ id: 1, watch_id: value.id, label: value.label,
          currency: 'USD', price_minor: 12000, rule: 'target', created_at: stamp, url: value.url });
      } else {
        // Retaining a stale numeric field deliberately checks the UI's guard.
        Object.assign(value, { status: 'parse_error', stale: true, current_price_minor: 12000,
          last_checked_at: '2026-10-26T13:00:00Z' });
      }
      return send(200, value);
    }
  }
  return send(404, { error: 'Synthetic fixture route not found' });
}

const server = http.createServer((req, res) => handler(req, res).catch(error => {
  fixture.failures.push(error.message);
  if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'Synthetic test fixture failed' }));
}));

async function main() {
  let browser;
  const screenshots = process.env.ARCTERYX_E2E_SCREENSHOT_DIR;
  if (screenshots) fs.mkdirSync(screenshots, { recursive: true });
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ||
      (fs.existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : chromium.executablePath());
    browser = await chromium.launch({ executablePath, args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, timezoneId: 'UTC' });
    page.setDefaultTimeout(7000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && /Content Security Policy|Refused to/.test(message.text())) errors.push(message.text());
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.getByText('从你想要的那件装备开始').waitFor();
    assert.equal(await page.locator('#watch-list .history-chart').count(), 0, 'empty data must not manufacture a price chart');

    await page.locator('#add-watch').click();
    assert.equal(await page.locator('#watch-url').evaluate(node => node === document.activeElement), true);
    assert.match(await page.locator('#supported-source-hint').innerText(), /REI Co-op（美国）/);
    assert.doesNotMatch(await page.locator('#supported-source-hint').innerText(), /加拿大|CAD/);
    await page.locator('#watch-url').fill('https://arcteryx.com/ca/en/shop/mens/synthetic-browser-fixture');
    assert.equal(await page.locator('#discover-variants').isDisabled(), true, 'Canadian URLs cannot be discovered');
    await page.locator('#manual-fields summary').click();
    await page.locator('#watch-sku').fill('SYNTHETIC-CA');
    await page.locator('#watch-color').fill('Synthetic Black');
    await page.locator('#watch-size').fill('M');
    await page.locator('#watch-target').fill('123.45');
    await page.locator('#save-watch').click();
    assert.match(await page.locator('#form-error').innerText(), /美国具体商品链接/);
    assert.equal(fixture.requests.length, 0, 'Canadian form submission never reaches discovery or watch API');
    await page.getByLabel('商品链接', { exact: true }).fill('https://arcteryx.com/us/en/shop/mens/synthetic-browser-fixture');
    await page.locator('#discover-variants').click();
    await page.getByText('已找到 1 个明确变体。').waitFor();
    await page.getByLabel('选择颜色与尺码', { exact: true }).selectOption('0');
    await page.locator('#watch-target').fill('123.45');
    await page.locator('#save-watch').click();
    await page.getByRole('heading', { name: 'SYNTHETIC US SHELL', exact: true }).waitFor();
    const creation = fixture.requests.find(item => item.url === '/api/watches');
    assert.equal(creation.body.target_price_minor, 12345, 'decimal input is sent as integer cents');
    assert.equal(creation.body.sku, 'SYNTHETIC-SAME-SKU');
    assert.equal(creation.body.color, 'Synthetic Black');
    assert.equal(creation.body.size, 'M');
    assert.equal(await page.locator('#watch-list .history-chart').count(), 0, 'discovery alone creates no historical observation');

    await page.getByRole('button', { name: '立即检查 SYNTHETIC US SHELL', exact: true }).click();
    await page.getByText('有效报价', { exact: true }).waitFor();
    assert.equal(await page.locator('#fresh-count').innerText(), '1');
    await page.locator('#watch-list .history-details summary').click();
    assert.equal(await page.locator('#watch-list .history-chart circle').count(), 1, 'one observation day produces one point');
    assert.equal(await page.locator('#watch-list .history-chart polyline').count(), 0, 'one day cannot imply ninety days of history');

    fixture.watches[0].interval_minutes = 45;
    await refresh();
    await page.getByRole('button', { name: '设置提醒 SYNTHETIC US SHELL', exact: true }).click();
    await page.locator('#settings-dialog').waitFor({ state: 'visible' });
    assert.match(await page.locator('#settings-target-currency').innerText(), /USD/);
    assert.equal(await page.locator('#settings-interval').inputValue(), '45', 'editing preserves a valid custom interval instead of silently replacing it');
    await page.locator('#settings-rule').selectOption('new_low');
    assert.equal(await page.locator('#settings-target').isDisabled(), true, 'new-low rule has no target-price input');
    await page.locator('#settings-rule').selectOption('target');
    await page.locator('#settings-target').fill('99.99');
    await page.locator('#settings-interval').selectOption('120');
    await page.locator('#save-settings').click();
    await page.locator('#settings-dialog').waitFor({ state: 'hidden' });
    const patch = fixture.requests.find(item => item.method === 'PATCH');
    assert.equal(patch.body.target_price_minor, 9999);
    assert.equal(patch.body.interval_minutes, 120);
    assert.equal(fixture.watches[0].target_price_minor, 9999);
    await page.getByText('目标价 ≤ $99.99 USD', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '设置提醒 SYNTHETIC US SHELL', exact: true }).evaluate(node => node === document.activeElement), true, 'saving settings returns keyboard focus to its watch');

    fixture.watches.push(watch(2, 'CA', 15000, 'SYNTHETIC CA SHELL'), watch(3, 'US', 26000, 'SYNTHETIC US EXPENSIVE'));
    fixture.alerts.push({ id: 2, watch_id: 2, label: 'SYNTHETIC CA ALERT', market: 'CA',
      currency: 'CAD', price_minor: 15000, rule: 'target', created_at: stamp, url: fixture.watches[1].url });
    fixture.alerts.push({ id: 3, watch_id: 999, label: 'SYNTHETIC UNKNOWN MARKET ALERT',
      currency: 'USD', price_minor: 10000, rule: 'target', created_at: stamp });
    Object.assign(fixture.watches[2], { retailer_key: 'rei', retailer_name: 'REI Co-op',
      url: 'https://www.rei.com/product/12345/arcteryx-synthetic-browser-fixture' });
    fixture.watches[0].metrics = metrics(12000, ['2026-10-24', '2026-10-26']);
    async function refresh() {
      const response = page.waitForResponse(value => value.url().endsWith('/api/state'));
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await (await response).finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    await refresh();
    await page.locator('.watch-card').nth(1).waitFor();
    await page.locator('#sort-watches').selectOption('price_asc');
    const identities = await page.locator('.watch-card').evaluateAll(cards => cards.map(card => ({
      name: card.querySelector('.watch-title').textContent,
      currency: card.querySelector('.market-badge').textContent,
      price: card.querySelector('.metric-current .metric-value').textContent,
    })));
    assert.equal(identities.length, 2, 'legacy Canadian watches are excluded from the dashboard');
    assert.ok(identities.every(item => item.currency === 'US · USD'));
    assert.deepEqual(identities.filter(item => item.currency.includes('USD')).map(item => item.name), ['SYNTHETIC US SHELL', 'SYNTHETIC US EXPENSIVE']);
    assert.equal(await page.locator('#nav-watch-count').innerText(), '2');
    assert.equal(await page.locator('#nav-alert-count').innerText(), '1', 'known US marketless alerts remain visible, but Canadian and unscoped USD alerts are excluded');
    if (screenshots) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(screenshots, 'desktop-synthetic.png'), fullPage: true });
    }

    assert.equal(await page.locator('[data-market]').count(), 0, 'the US-only dashboard has no market chooser');
    assert.doesNotMatch(await page.locator('#watch-list').innerText(), /SYNTHETIC CA SHELL|CAD/);
    await page.locator('#watch-search').fill('does-not-exist-synthetic');
    assert.equal(await page.locator('.watch-card').count(), 0);
    await page.locator('#reset-filters').click();

    await page.locator('#retailer-filter').selectOption('rei');
    assert.equal(await page.locator('.watch-card').count(), 1);
    assert.match(await page.locator('.watch-retailer').innerText(), /REI Co-op/);
    assert.equal(await page.locator('.watch-title a').getAttribute('href'), 'https://www.rei.com/product/12345/arcteryx-synthetic-browser-fixture');
    await page.locator('#reset-filters').click();
    await page.locator('#watch-search').fill('rei');
    assert.equal(await page.locator('.watch-card').count(), 1, 'retailer names are searchable');
    await page.locator('#reset-filters').click();
    await page.locator('.watch-card').nth(1).waitFor();
    await page.locator('#status-filter').selectOption('in_stock');
    assert.equal(await page.locator('.watch-card').count(), 2);
    await page.locator('#watch-search').fill('US EXPENSIVE');
    assert.equal(await page.locator('.watch-card').count(), 1);
    await page.locator('#reset-filters').click();

    const focused = page.getByRole('button', { name: '立即检查 SYNTHETIC US SHELL', exact: true });
    await focused.focus();
    fixture.watches[0].last_checked_at = '2026-10-26T14:00:00Z';
    fixture.watches[0].metrics.as_of = '2026-10-26T14:00:00Z';
    await refresh();
    await page.waitForFunction(() => [...document.querySelectorAll('.watch-card')].some(card =>
      card.querySelector('.watch-title').textContent === 'SYNTHETIC US SHELL' &&
      card.querySelector('.watch-health').textContent.includes('14:00')));
    await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '立即检查 SYNTHETIC US SHELL');
    const focusedCard = page.locator('.watch-card').filter({ has: page.getByRole('heading', { name: 'SYNTHETIC US SHELL', exact: true }) });
    assert.equal(await focusedCard.locator('.history-details').getAttribute('open') !== null, true, 'refresh retains expanded history');

    fixture.historical_evidence = [
      { evidence_key: 'synthetic-us-deal', source_url: 'https://slickdeals.net/f/12345-synthetic-fixture',
        publisher: 'Slickdeals (SYNTHETIC FIXTURE)', source_kind: 'deal_report', observed_at: '2025-11-28', date_precision: 'day',
        captured_at: stamp, price_minor: 8000, market: 'US', currency: 'USD', retailer_key: 'rei', retailer_name: 'REI Co-op',
        product_name: 'SYNTHETIC HISTORICAL DEAL', sku: null, color: null, size: null, price_type: 'coupon',
        included_in_metrics: false, exclusion_reason: 'SYNTHETIC fixture: exact SKU is not established.',
        excerpt: '<script>window.syntheticUnsafe = true</script> SYNTHETIC quoted deal.', watch_id: null },
      { evidence_key: 'synthetic-ca-deal', source_url: 'javascript:alert(1)',
        publisher: 'SYNTHETIC unsafe link fixture', source_kind: 'deal_report', observed_at: '2025-10-20', date_precision: 'day',
        captured_at: stamp, price_minor: 10000, market: 'CA', currency: 'CAD', retailer_name: 'SYNTHETIC STORE',
        product_name: 'SYNTHETIC CA HISTORICAL DEAL', included_in_metrics: false, price_type: 'member', watch_id: null },
      { evidence_key: 'synthetic-unsafe-us-deal', source_url: 'javascript:alert(1)',
        publisher: 'SYNTHETIC unsafe US link fixture', source_kind: 'deal_report', observed_at: '2025-10-20', date_precision: 'day',
        captured_at: stamp, price_minor: 10000, market: 'US', currency: 'USD', retailer_name: 'SYNTHETIC STORE',
        product_name: 'SYNTHETIC UNSAFE US HISTORICAL DEAL', included_in_metrics: false, price_type: 'member', watch_id: null },
      { evidence_key: 'synthetic-verified-archive', source_url: fixture.watches[0].url,
        publisher: 'SYNTHETIC archived retailer', source_kind: 'retailer_archive', observed_at: '2026-10-24T12:00:00Z', date_precision: 'second',
        captured_at: stamp, price_minor: 12000, market: 'US', currency: 'USD', retailer_name: "Arc'teryx official",
        product_name: 'SYNTHETIC VERIFIED ARCHIVE', included_in_metrics: true, watch_id: 1, price_type: 'public',
        sku: 'SYNTHETIC-SAME-SKU', color: 'Synthetic Black', size: 'M', model_code: 'SYNTHETIC' },
    ];
    await refresh();
    await page.locator('button[data-view="history"]').click();
    await page.locator('#view-history').waitFor({ state: 'visible' });
    await page.locator('#history-watch').selectOption('1');
    assert.equal(await page.locator('#history-content .history-chart circle').count(), 2);
    assert.equal(await page.locator('#history-content .history-chart polyline').count(), 0, 'missing days must not be bridged with invented continuous history');
    assert.match(await page.locator('#history-content').innerText(), /2026-10-24/);
    assert.match(await page.locator('#history-content').innerText(), /2026-10-26/);
    const deal = page.locator('[data-evidence-key="synthetic-us-deal"]');
    assert.match(await deal.innerText(), /原帖发布时间：2025-11-28/);
    assert.match(await deal.innerText(), /未计入上方最低价 \/ 均价；与当前关注未自动匹配/);
    assert.match(await deal.innerText(), /非当前报价/);
    await deal.locator('summary').click();
    assert.match(await deal.innerText(), /使用优惠码/);
    assert.match(await deal.locator('blockquote').innerText(), /<script>/);
    assert.equal(await page.evaluate(() => window.syntheticUnsafe), undefined, 'source excerpts are plain text');
    assert.equal(await deal.locator('a').getAttribute('href'), 'https://slickdeals.net/f/12345-synthetic-fixture');
    assert.equal(await page.locator('[data-evidence-key="synthetic-ca-deal"]').count(), 0, 'legacy Canadian history is excluded');
    assert.equal(await page.locator('[data-evidence-key="synthetic-unsafe-us-deal"] a').count(), 0, 'unsafe evidence URLs cannot become links');
    assert.match(await page.locator('[data-evidence-key="synthetic-unsafe-us-deal"]').innerText(), /会员专享/);
    assert.match(await page.locator('[data-evidence-key="synthetic-verified-archive"]').innerText(), /已计入对应关注的历史统计/);
    assert.equal(await page.locator('#history-content .history-chart circle').count(), 2, 'reference-only reports never add points to the selected variant chart');
    assert.equal(await page.locator('.evidence-card').count(), 3);
    assert.equal(await page.locator('#evidence-market').count(), 0);
    assert.doesNotMatch(await page.locator('#historical-evidence-list').innerText(), /CAD|SYNTHETIC CA HISTORICAL DEAL/);
    await page.locator('button[data-view="alerts"]').click();
    await page.locator('#view-alerts').waitFor({ state: 'visible' });
    assert.match(await page.locator('#alert-list').innerText(), /SYNTHETIC US SHELL/);
    assert.match(await page.locator('#alert-list').innerText(), /USD/);
    assert.doesNotMatch(await page.locator('#alert-list').innerText(), /CAD|SYNTHETIC CA ALERT|SYNTHETIC UNKNOWN MARKET ALERT/);
    await page.locator('button[data-view="overview"]').click();

    await page.getByRole('button', { name: '立即检查 SYNTHETIC US SHELL', exact: true }).click();
    await page.getByText('页面待适配', { exact: true }).waitFor();
    const failedCard = page.locator('.watch-card').filter({ has: page.getByRole('heading', { name: 'SYNTHETIC US SHELL', exact: true }) });
    assert.equal(await failedCard.locator('.metric-current .metric-value').innerText(), '—', 'failed source must hide stale numeric current-price fields');
    assert.match(await failedCard.innerText(), /120\.00/, 'recorded historical price remains available when current quote fails');
    await page.locator('#status-filter').selectOption('attention');
    assert.equal(await page.locator('.watch-card').count(), 1);
    await page.locator('#reset-filters').click();

    fixture.offline = true;
    await refresh();
    await page.locator('#connection-message').waitFor({ state: 'visible' });
    assert.deepEqual(await page.locator('#watch-list .metric-current .metric-value').allTextContents(), ['—', '—'], 'service disconnection hides all current prices');
    assert.equal(await page.locator('#fresh-count').innerText(), '0');
    assert.equal(await page.locator('#add-watch').isDisabled(), true, 'disconnection disables creation until state and CSRF are refreshed');
    assert.equal(await page.locator('#watch-list button:enabled').count(), 0, 'disconnection disables all watch mutations');
    fixture.offline = false;
    await refresh();
    await page.locator('#connection-message').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#add-watch').isEnabled(), true);

    await page.setViewportSize({ width: 390, height: 844 });
    for (const view of ['overview', 'history', 'alerts', 'guide']) {
      await page.locator(`button[data-view="${view}"]`).click();
      await page.locator(`#view-${view}`).waitFor({ state: 'visible' });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${view} fits the mobile viewport`);
      if (screenshots) {
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.screenshot({ path: path.join(screenshots, `mobile-${view}-synthetic.png`), fullPage: true });
      }
    }
    await page.locator('button[data-view="overview"]').click();
    await page.locator('#add-watch').click();
    assert.equal(await page.locator('#watch-url').evaluate(node => node === document.activeElement), true);
    if (screenshots) await page.screenshot({ path: path.join(screenshots, 'mobile-dialog-synthetic.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.locator('#watch-dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('#add-watch').evaluate(node => node === document.activeElement), true, 'closing modal restores keyboard focus');

    await page.locator('#add-watch').click();
    await page.locator('#watch-url').fill('https://rei.com/product/12346/arcteryx-synthetic-browser-fixture');
    assert.match(await page.locator('#market-hint').innerText(), /REI Co-op · 美国商品，价格以 USD/);
    await page.locator('#discover-variants').click();
    await page.getByText('已找到 1 个明确变体。').waitFor();
    await page.locator('#variant-select').selectOption('0');
    await page.locator('#watch-target').fill('120');
    await page.locator('#save-watch').click();
    await page.getByRole('heading', { name: 'SYNTHETIC REI SHELL', exact: true }).waitFor();
    const retailerCreation = fixture.requests.filter(item => item.url === '/api/watches').at(-1);
    assert.equal(retailerCreation.body.url, 'https://www.rei.com/product/12346/arcteryx-synthetic-browser-fixture');
    assert.equal(retailerCreation.body.target_price_minor, 12000);
    await page.locator('#retailer-filter').selectOption('rei');
    assert.equal(await page.locator('.watch-card').count(), 2, 'new retailer watch appears in its retailer filter');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'retailer filters fit mobile layout');
    assert.deepEqual(fixture.failures, []);
    assert.deepEqual(errors, [], 'no JavaScript or CSP runtime failures');
    console.log('PASS: synthetic browser official/REI discovery/create/edit, retailer filters, US-only URL and legacy-record exclusion, USD price sorting, historical source provenance and exclusions, sparse history, alerts, stale/offline prices, mobile and keyboard focus.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
