const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function loadPlaywright() {
  try { return require("playwright"); } catch {
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
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.setContent("<main>TikTok LIVE API test page — no product DOM</main>");
  await page.evaluate(() => {
    window.__LIVE_RE_TEST_MODE__ = true;
    window.__apiCalls = [];
    window.__pinId = "";
    window.__products = [
      { bag_index: 1, product_id: "p1", title: "Kích Rễ Cực Mạnh TC01 chai 100ml" },
      { bag_index: 2, product_id: "p2", title: "Đạm Cá Amino 1L Chất Hữu Cơ 20%" },
      { bag_index: 3, product_id: "p3", title: "BO TRI ONE | Chế Phẩm Top Bo One Tri 200ml" },
      { bag_index: 4, product_id: "p4", title: "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm Can 5 Lít" },
      { bag_index: 5, product_id: "p5", title: "SẠCH SÂU ĐẤT 1KG Công Nghệ Mới" }
    ];
    const initialState = { running: false, startedAtMs: 0, pausedElapsedMs: 0, revision: 0 };
    window.chrome = {
      runtime: {
        id: "live-re-test-extension",
        onMessage: { addListener(listener) { window.__runtimeListener = listener; } },
        async sendMessage(message) {
          if (message.type === "LIVE_RE_GET_STATE") {
            return { ok: true, state: window.__LIVE_RE_DEBUG__?.getState() || initialState, isController: !window.__lostControl };
          }
          if (message.type !== "LIVE_RE_TIKTOK_API") return { ok: true };
          const request = message.request;
          window.__apiCalls.push(JSON.parse(JSON.stringify(request)));
          if (request.action === "snapshot") {
            return {
              ok: true,
              snapshot: {
                room_id: "room-test",
                pin_id: window.__pinId,
                products: window.__products.map((product) => ({
                  ...product,
                  is_pinned: product.product_id === window.__pinId
                }))
              }
            };
          }
          if (request.action === "pin") window.__pinId = request.productId;
          if (request.action === "unpin" && window.__pinId === request.productId) window.__pinId = "";
          return {
            ok: true,
            room_id: "room-test",
            product_id: request.productId,
            op: request.action === "pin" ? 1 : 2
          };
        }
      },
      storage: {
        local: {
          get() { return Promise.resolve({ liveReState: initialState }); },
          set() { return Promise.resolve(); }
        },
        onChanged: { addListener(listener) { window.__storageListener = listener; } }
      }
    };
  });

  await page.addScriptTag({ path: path.join(__dirname, "schedule.js") });
  await page.addScriptTag({ path: path.join(__dirname, "live-widget.js") });
  await page.addScriptTag({ path: path.join(__dirname, "content.js") });
  await page.waitForTimeout(100);

  async function setElapsed(seconds, revision) {
    await page.evaluate(({ seconds, revision }) => {
      window.__LIVE_RE_DEBUG__.applyState({
        running: true,
        startedAtMs: Date.now() - seconds * 1000,
        pausedElapsedMs: 0,
        revision,
        sessionId: `test-${revision}`
      });
    }, { seconds, revision });
    await page.waitForTimeout(180);
  }

  const pinId = () => page.evaluate(() => window.__pinId);

  // Timeline do man hinh phan tich sinh ra mang product_id chinh xac. ID phai
  // duoc uu tien ngay ca khi ten listing khong khop bo tu khoa du phong.
  await page.evaluate(() => {
    window.KidTESchedule.load({
      schema_version: 1, video_id: "exact-id", cycle_ms: 60000,
      products: {
        exact: { product_id: "p5", bag_index: 999, short_name: "Tên cố ý sai",
          title: "Không chứa từ khóa sản phẩm", include_all: ["khong-he-khop"],
          exclude_any: [] }
      },
      events: [{ at_ms: 0, product: "exact" }]
    });
  });
  await setElapsed(0, 100);
  assert.equal(await pinId(), "p5", "exact product_id must override title matching");
  await page.evaluate(() => {
    window.KidTESchedule.load(window.KidTESchedule.DEFAULT_TIMELINE);
    window.__pinId = "";
  });

  await setElapsed(0, 1);
  assert.equal(await pinId(), "p1", "00:00 must pin TC01 through API");
  await setElapsed(167.1, 2);
  assert.equal(await pinId(), "p3", "02:47 must pin Top One through API");
  await setElapsed(302.1, 3);
  assert.equal(await pinId(), "", "05:02 must unpin through API");
  await setElapsed(510.1, 4);
  assert.equal(await pinId(), "p2", "08:30 must pin fish 1L through API");
  await setElapsed(570.1, 5);
  assert.equal(await pinId(), "p4", "09:30 must alternate to fish 5L through API");
  await setElapsed(630.1, 6);
  assert.equal(await pinId(), "p2", "10:30 must alternate back to fish 1L through API");
  await setElapsed(1040.1, 7);
  assert.equal(await pinId(), "p5", "17:20 must pin Sạch Sâu Đất through API");
  const cycleSeconds = await page.evaluate(() => window.KidTESchedule.CYCLE_MS / 1000);
  await setElapsed(cycleSeconds + 0.1, 8);
  assert.equal(await pinId(), "p1", "new loop must return to TC01 through API");
  await setElapsed(27 * 60 + 15, 9);
  assert.equal(await pinId(), "p1", "27:15 must resolve to video position 08:15");

  const beforePulse = await page.evaluate(() => window.__apiCalls.length);
  await page.evaluate(() => window.__LIVE_RE_DEBUG__.queueOperation("pulse"));
  await page.waitForTimeout(750);
  const pulse = await page.evaluate((before) => ({
    pinId: window.__pinId,
    calls: window.__apiCalls.slice(before)
      .filter((call) => call.action === "pin" || call.action === "unpin")
      .map((call) => [call.action, call.productId])
  }), beforePulse);
  assert.equal(pulse.pinId, "p1");
  assert.deepEqual(pulse.calls, [["unpin", "p1"], ["pin", "p1"]]);

  const beforeOwnershipChange = await page.evaluate(() => {
    const count = window.__apiCalls.length;
    window.__LIVE_RE_DEBUG__.queueOperation('pulse');
    setTimeout(() => {window.__lostControl = true;}, 100);
    return count;
  });
  await page.waitForTimeout(750);
  const abandonedRepin = await page.evaluate(before => window.__apiCalls.slice(before)
    .filter(call => ['pin','unpin'].includes(call.action)).map(call => call.action), beforeOwnershipChange);
  assert.deepEqual(abandonedRepin, ['unpin'], 'ownership lost during gap must not pin');
  await page.evaluate(() => {window.__lostControl = false;});

  // Ghim ngoài tiện ích phải được nhận là override sau hai snapshot ổn định,
  // được làm mới chính sản phẩm đó và không bị timeline kéo về ngay.
  await setElapsed(510.1, 10);
  await page.evaluate(() => { window.__pinId = "p3"; });
  await page.evaluate(async () => {
    await window.__LIVE_RE_DEBUG__.observeExternalPin();
    await window.__LIVE_RE_DEBUG__.observeExternalPin();
  });
  const manual = await page.evaluate(() => window.__LIVE_RE_DEBUG__.getManualOverride());
  assert.equal(manual.productId, "p3");
  assert.match(manual.title, /BO TRI ONE/);
  const beforeManualPulse = await page.evaluate(() => window.__apiCalls.length);
  await page.evaluate(() => window.__LIVE_RE_DEBUG__.queueOperation("pulse"));
  await page.waitForTimeout(750);
  const manualPulse = await page.evaluate((before) => window.__apiCalls.slice(before)
    .filter((call) => call.action === "pin" || call.action === "unpin")
    .map((call) => [call.action, call.productId]), beforeManualPulse);
  assert.deepEqual(manualPulse, [["unpin", "p3"], ["pin", "p3"]]);

  // Bỏ ghim thủ công cũng là một override hợp lệ và phải giữ trống.
  await page.evaluate(() => { window.__pinId = ""; });
  await page.evaluate(async () => {
    await window.__LIVE_RE_DEBUG__.observeExternalPin();
    await window.__LIVE_RE_DEBUG__.observeExternalPin();
  });
  assert.equal(
    await page.evaluate(() => window.__LIVE_RE_DEBUG__.getManualOverride().productId),
    ""
  );
  await page.evaluate(() => window.__LIVE_RE_DEBUG__.queueOperation("pulse"));
  await page.waitForTimeout(100);
  assert.equal(await pinId(), "");

  // Hết 60 giây giả lập: trở lại đúng sản phẩm của timeline hiện tại.
  await page.evaluate(() => window.__LIVE_RE_DEBUG__.expireManualOverride());
  await page.waitForTimeout(250);
  assert.equal(await pinId(), "p2");

  const mode = await page.getAttribute("html", "data-live-re-mode");
  assert.equal(mode, "tiktok-api");
  assert.equal(await page.locator(".product-row").count(), 0, "test must not supply product DOM");

  // Tải lại extension làm context cũ mất hiệu lực. Script phải tự ngắt timer,
  // không tiếp tục gọi chrome.storage và không tạo lỗi lặp trong console.
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.evaluate(() => {
    window.chrome.runtime.id = "";
    window.chrome.storage.local.set = () => {
      throw new Error("Extension context invalidated.");
    };
  });
  await page.waitForTimeout(500);
  const invalidatedStatus = await page.evaluate(() =>
    document.querySelector("#live-re-pin-host").shadowRoot.querySelector("#status").textContent
  );
  assert.match(invalidatedStatus, /tải lại tab TikTok/i);
  assert.deepEqual(pageErrors, []);

  await browser.close();
  browser = null;
  console.log("LIVE_RE_API_CONTENT_TEST_OK");
})().catch((error) => {
  console.error(error);
  if (browser) browser.close().catch(() => {});
  process.exitCode = 1;
});
