"use strict";
const assert = require("node:assert/strict");
const { desired } = require("./core.js");
const config = { enabled: true, videoId: "live1", mappings: {
  a: { shop_id: "10", product_id: "111" }, b: { shop_id: "10", product_id: "222" }
} };
const timeline = { live1: { cycle_ms: 100000, products: { a: { short_name: "A" }, b: { short_name: "B" } },
  events: [{ at_ms: 0, product: "a" },
    { at_ms: 30000, product: "a", alternate_with: "b", alternate_every_ms: 10000 },
    { at_ms: 70000, product: null }] } };
const position = { ok: true, available: true, video_id: "live1", position_s: 1,
  margin: 0.2, min_margin: 0.06, age_s: 1 };
assert.equal(desired(config, position, timeline).product_id, "111");
assert.equal(desired(config, { ...position, position_s: 41 }, timeline).product_id, "222");
const shared = { ...config, mappings: { a: { shop_id: "10", product_id: "111" },
  b: { shop_id: "10", product_id: "111" } },
  alternates: { a: { shop_id: "10", product_id: "222" } } };
assert.equal(desired(shared, { ...position, position_s: 31 }, timeline).product_id, "111");
assert.equal(desired(shared, { ...position, position_s: 41 }, timeline).product_id, "222");
assert.equal(desired(shared, { ...position, position_s: 51 }, timeline).product_id, "111");
assert.equal(desired(shared, { ...position, position_s: 31 }, timeline).token,
  desired(shared, { ...position, position_s: 51 }, timeline).token);
assert.equal(desired(config, { ...position, position_s: 72 }, timeline).action, "hide");
assert.equal(desired(config, { ...position, margin: 0.01 }, timeline).ok, false);
assert.equal(desired(config, { ...position, margin: undefined }, timeline).ok, false);
assert.equal(desired(config, { ...position, age_s: undefined }, timeline).ok, false);
assert.equal(desired(config, { ...position, age_s: -1 }, timeline).ok, false);
assert.equal(desired(config, { ...position, available: false }, timeline).ok, false);
assert.equal(desired(config, { ...position, video_id: "other" }, timeline).ok, false);
assert.equal(desired({ ...config, mappings: {} }, position, timeline).ok, false);
console.log("Shopee timeline tests passed");

(async () => {
  const { liveSession, findLiveTab } = require('./core.js');
  assert.equal(liveSession('https://live.shopee.vn/pc/live/?session=123&source=obs'), '123');
  assert.equal(liveSession('https://live.shopee.vn/pc/live?session=abc'), '');
  assert.equal(liveSession('https://evil.example/pc/live?session=123'), '');
  const target = { id: 8, url: 'https://live.shopee.vn/pc/live/?session=123' };
  const tabs = { query: async query => query.active ? [{ id: 9, url: 'https://example.com' }] : [target] };
  assert.equal((await findLiveTab(tabs)).id, 8);
  await assert.rejects(findLiveTab({ query: async query => query.active ? [] : [target, { id: 10, url: 'https://live.shopee.vn/pc/live?session=456' }] }), /nhiều phiên/);
  console.log('Shopee tab discovery tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

assert.equal(require("./core.js").liveSession("https://live.shopee.vn/pc/preview?session=41617785", true), "41617785");
assert.equal(require("./core.js").liveSession("https://live.shopee.vn/pc/preview?session=41617785"), "");
