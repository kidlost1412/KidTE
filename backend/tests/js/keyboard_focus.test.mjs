import test from 'node:test';
import assert from 'node:assert/strict';
import {loadPlaywright, startServer, openRoute, measureOverflow, PAGES, CHROME} from '../../tools/ui_screens.mjs';

const playwright = await loadPlaywright();
const MAX_TABS = 80;

async function tabThrough(page) {
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  const seen = [];
  let stuck = 0, last = null;
  for (let i = 0; i < MAX_TABS; i++) {
    await page.keyboard.press('Tab');
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return {body: true};
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0;
      const shadow = s.boxShadow && s.boxShadow !== 'none';
      // custom controls (e.g. visually hidden checkbox) may show the ring on an adjacent track
      const sib = el.nextElementSibling ? getComputedStyle(el.nextElementSibling).boxShadow : 'none';
      const cls = typeof el.className === 'string' ? el.className : '';
      const uid = el.__kf || (el.__kf = ++window.__kfn || (window.__kfn = 1));
      return {uid, id: `${el.tagName.toLowerCase()}.${cls.trim().split(/\s+/).join('.')}|${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 24)}`,
        visible: r.width > 0 && r.height > 0, indicator: outline || shadow || (sib && sib !== 'none')};
    });
    if (info.body) break; // focus left the document: cycle complete
    if (info.uid === last) { if (++stuck >= 3) break; } else stuck = 0;
    last = info.uid;
    seen.push(info);
  }
  return {seen, stuck};
}

test('every top-level page: Tab shows a focus indicator everywhere, no focus trap; no overflow at 1366x768', async () => {
  const {origin, close} = await startServer();
  const browser = await playwright.chromium.launch({headless: true, executablePath: CHROME});
  try {
    const context = await browser.newContext({viewport: {width: 1366, height: 768}, deviceScaleFactor: 1});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    for (const name of PAGES) {
      await openRoute(page, origin, name);
      const {seen, stuck} = await tabThrough(page);
      assert.ok(seen.length >= 3, `${name}: expected focusable controls, got ${seen.length}`);
      assert.ok(stuck < 3, `${name}: focus trap, stuck on ${seen.at(-1)?.id}`);
      const missing = seen.filter(s => !s.indicator).map(s => s.id);
      assert.deepEqual(missing, [], `${name}: focused elements without visible indicator`);
      const hidden = seen.filter(s => !s.visible).map(s => s.id);
      assert.deepEqual(hidden, [], `${name}: focus landed on invisible elements`);
      const overflow = await measureOverflow(page);
      assert.equal(overflow.docOverflow, false, `${name}: horizontal page overflow`);
      assert.deepEqual(overflow.clipped, [], `${name}: elements beyond viewport`);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await close(); }
});
