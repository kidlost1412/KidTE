const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch {
    const runtimes = path.join(process.env.LOCALAPPDATA, "OpenAI", "Codex", "runtimes", "cua_node");
    const latest = fs.readdirSync(runtimes).sort().at(-1);
    return require(path.join(runtimes, latest, "bin", "node_modules", "playwright"));
  }
}

const { chromium } = loadPlaywright();

let browser;

(async () => {
  browser = await chromium.launch({
    headless: true,
    executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
  });
  const page = await browser.newPage({ viewport: { width: 440, height: 600 } });
  // Visual evidence must capture the settled panel, not its fade-in frame.
  async function screenshot(options) {
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {})));
    });
    return page.screenshot(options);
  }
  await page.addInitScript(() => {
    const listeners = [];
    window.__sentMessages = [];
    window.chrome = {
      storage: {
        local: {
          get(keys, callback) { const value = window.__store || {}; if (callback) callback(value); return Promise.resolve(value); },
          async set(value) { Object.assign(window.__store ||= {}, value); }
        },
        onChanged: { addListener(listener) { listeners.push(listener); } }
      },
      runtime: {
        sendMessage(message) {
          window.__sentMessages.push(message);
          if (message.type === "LIVE_RE_GET_STATE") {
            return Promise.resolve({ ok: true, state: { running: false, startedAtMs: 0, pausedElapsedMs: 0 } });
          }
          if (message.type === "LIVE_RE_LIST_CARTS") {
            return Promise.resolve({ ok: true, active_name: "@shopA", carts: [
              { key: "uid:1", name: "@shopA", product_count: 19 }] });
          }
          if (message.type === "LIVE_RE_SCAN_CATALOG") {
            if (message.cartTarget === "uid:1" && !message.confirmed) {
              return Promise.resolve({ ok: false, needs_confirm: true, cart_name: "@shopA", old_count: 19, new_count: 41 });
            }
            return Promise.resolve({ ok: true, product_count: 41, cart_name: message.cartTarget === "new" ? message.cartName : "@shopA",
              cart_created: message.cartTarget === "new", active_cart_name: "@shopA" });
          }
          const elapsedMs = Number(message.elapsedMs || 0);
          return Promise.resolve({
            ok: true,
            state: {
              running: true,
              startedAtMs: Date.now() - elapsedMs,
              pausedElapsedMs: elapsedMs,
              revision: 1,
              sessionId: "popup-test"
            }
          });
        }
      }
    };
  });

  await page.goto(pathToFileURL(path.join(__dirname, "popup.html")).href);
  await page.waitForTimeout(100);
  fs.mkdirSync(path.join(__dirname,'../backend/runtime/ui-proof'),{recursive:true});
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/tiktok-popup.png'),fullPage:true});
  assert.equal(await page.locator('#clock').innerText(),'—');
  await page.locator('#tab-live').focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#tab-tools').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('#panel-live').isVisible(),false);
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/tiktok-ai.png')});
  await page.locator('#tab-tools').click();
  await page.locator('summary').filter({hasText:'Giỏ sản phẩm'}).click();
  await page.locator('summary').filter({hasText:'Điều khiển thủ công'}).click();
  assert.equal(await page.locator("#jump").innerText(), "Đến mốc");
  // 3.11.14: quét tay "lấy ở đâu lưu ở đó": mặc định Giỏ mới với tên đã nhập.
  assert.match(await page.locator("#cart-name").getAttribute("placeholder"), /Giỏ \d\d:\d\d \d\d\/\d\d$/);
  assert.match(await page.locator("#live-cart-note").innerText(), /Buổi LIVE đang dùng giỏ «@shopA»/);
  assert.equal(await page.locator("#cart-target").inputValue(), "new");
  await page.locator("#cart-name").fill("Tab A");
  await page.locator("#scan-catalog").click();
  await page.waitForTimeout(50);
  assert.match(await page.locator("#catalog-status").innerText(), /Đã lưu 41 sản phẩm vào giỏ «Tab A»/);
  // Ghi đè có chủ đích: hỏi xác nhận kèm số cũ/mới; huỷ thì giữ nguyên.
  let dialogText = "";
  page.once("dialog", (dialog) => { dialogText = dialog.message(); dialog.dismiss(); });
  await page.locator("#cart-target").selectOption("uid:1");
  await page.locator("#scan-catalog").click();
  await page.waitForTimeout(50);
  assert.match(dialogText, /Ghi đè giỏ «@shopA»/);
  assert.match(dialogText, /19 sản phẩm.*41 sản phẩm/s);
  assert.match(await page.locator("#catalog-status").innerText(), /Đã huỷ/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#scan-catalog").click();
  await page.waitForTimeout(50);
  assert.match(await page.locator("#catalog-status").innerText(), /Đã lưu 41 sản phẩm vào giỏ «@shopA»/);
  const scans = (await page.evaluate(() => window.__sentMessages)).filter((m) => m.type === "LIVE_RE_SCAN_CATALOG");
  assert.equal(scans.at(-1).confirmed, true);
  assert.equal(scans.at(-1).cartTarget, "uid:1");

  await page.locator("#jump-time").fill("7:60");
  await page.locator("#jump").click();
  assert.match(await page.locator("#jump-error").innerText(), /không hợp lệ/i);

  await page.locator("#jump-time").fill("27:15");
  await page.evaluate(() => { window.__store = {liveRePageStatus:{ok:true,updatedAtMs:Date.now(),videoSync:{state:'locked'}}}; });
  await page.locator("#jump").click();
  await page.waitForTimeout(100);
  const sent = await page.evaluate(() => window.__sentMessages);
  assert.ok(sent.some((message) =>
    message.type === "LIVE_RE_START_AT" && message.elapsedMs === 1635000
  ));
  assert.match(await page.locator("#jump-error").innerText(), /Đã đồng bộ từ 27:15/i);
  // Chu kỳ thật là 19:00.770, nên 27:15 nằm tại 08:14.230 của vòng hai.
  assert.equal(await page.locator("#clock").innerText(), "08:14");

  await page.goto(pathToFileURL(path.join(__dirname, '../extension-shopee/popup.html')).href);
  await page.waitForTimeout(100);
  assert.equal(await page.locator('header .wordmark').getAttribute('aria-label'),'Kid TE');
  assert.equal(await page.locator('#clock').innerText(),'—');
  assert.equal(await page.locator('#resume').isVisible(),false);
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/shopee-popup.png'),fullPage:true});
  assert.equal(fs.readFileSync(path.join(__dirname,'popup.css'),'utf8'),fs.readFileSync(path.join(__dirname,'../extension-shopee/popup.css'),'utf8'));
  await page.evaluate(() => { window.__store = {shopeeConfig:{aiEnabled:false},shopeeStatus:{ok:true,confirmed:true,at:Date.now(),videoTitle:'Video Đạm Cá và hướng dẫn chăm sóc vườn',positionS:427,productTitle:'Đạm Cá 5L · Dinh dưỡng hữu cơ cho cây trồng, dùng cho chăm sóc và phục hồi đất',nextAtMs:450000}}; return refreshStatus(); });
  assert.equal(await page.locator('#clock').innerText(),'7:07');
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/shopee-live-connected.png')});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 440));
  await page.locator('#pause').click();
  assert.equal(await page.locator('#resume').isVisible(),true);
  assert.match(await page.locator('#action-feedback').innerText(),/tạm dừng/);
  await page.evaluate(() => { window.__store.shopeeStatus.at = Date.now()-30000; return refreshStatus(); });
  assert.equal(await page.locator('#clock').innerText(),'—');
  assert.match(await page.locator('#current').innerText(),/Chưa xác nhận/);
  await page.locator('#tab-live').click();
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/shopee-ai.png')});
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.locator('#panel-live').evaluate(el=>getComputedStyle(el).animationName),'none');
  // Shared on-page monitor: both platforms, disclosure and safe text rendering.
  await page.addScriptTag({path:path.join(__dirname,'live-widget.js')});
  await page.evaluate(() => {
    const widget=KidTELiveWidget.create('shopee','qa-live-widget');
    KidTELiveWidget.update(widget,{ok:true,confirmed:true,videoTitle:'Vườn xanh',positionS:427,productTitle:'Đạm Cá 5L <script>không thực thi</script>'});
  });
  assert.match(await page.locator('#qa-live-widget #product').innerText(),/Đạm Cá 5L <script>/);
  await page.locator('#qa-live-widget #expand').click();
  assert.equal(await page.locator('#qa-live-widget #expand').getAttribute('aria-expanded'),'true');
  await screenshot({path:path.join(__dirname,'../backend/runtime/ui-proof/shopee-widget.png')});
  for (const platform of ['shopee', 'tiktok']) {
    const id = 'qa-movable-' + platform;
    await page.evaluate(({platform,id}) => KidTELiveWidget.create(platform,id),{platform,id});
    const box = page.locator('#' + id + ' #box');
    const bar = page.locator('#' + id + ' #bar');
    const original = await box.boundingBox(), handle = await bar.boundingBox();
    await page.mouse.move(handle.x + 22, handle.y + 18);
    await page.mouse.down();
    await page.mouse.move(handle.x + 100, handle.y - 100, {steps: 5});
    await page.mouse.up();
    const moved = await box.boundingBox();
    assert.ok(moved.x !== original.x || moved.y !== original.y);
    assert.ok(moved.x >= 0 && moved.y >= 0 && moved.x + moved.width <= 440);
    await page.locator('#' + id + ' #minimize').click();
    assert.equal(await box.evaluate(el=>el.classList.contains('compact')),true);
    assert.equal(await page.locator('#' + id + ' #product').isVisible(),false);
    await page.locator('#' + id + ' #hide').click();
    assert.equal(await box.isVisible(),false);
    await page.evaluate(({platform,id})=>{
      document.getElementById(id).remove();
      KidTELiveWidget.create(platform,id);
    },{platform,id});
    assert.equal(await box.isVisible(),false);
    await page.locator('#' + id + ' #restore').click();
    assert.equal(await box.isVisible(),true);
    assert.equal(await box.evaluate(el=>el.classList.contains('compact')),true);
    await page.evaluate(id=>document.getElementById(id).remove(),id);
  }

  await browser.close();
  browser = null;
  console.log("LIVE_RE_POPUP_TEST_OK");
})().catch((error) => {
  console.error(error);
  if (browser) browser.close().catch(() => {});
  process.exitCode = 1;
});
