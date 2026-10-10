"use strict";

const assert = require("node:assert/strict");
const { runShopeeApi } = require("./shopee-api.js");

async function main() {
  global.location = { href: "https://live.shopee.vn/pc/live?session=41590640", hostname: "live.shopee.vn", pathname: "/pc/live" };
  const calls = [];
  const writes = [];
  const ui = { memoizedProps: { sessionId: 41590640, showingProduct: null } };
  global.document = {querySelectorAll: () => [{_reactRootContainer: {_internalRoot: {current: ui}}}]};
  const list = async (params) => {
    calls.push(params);
    return [null, params.offset === 0 ? {
      total_count: 3, has_more: true, next_offset: 2,
      items: [{ item_id: 111, shop_id: 10, title: "TC01", sequence_no: 1 },
        { item_id: 222, shop_id: 10, title: "Đạm cá", sequence_no: 2 }]
    } : {
      total_count: 3, has_more: false,
      items: [{ item_id: 222, shop_id: 10, title: "Đạm cá", sequence_no: 2 },
        { item_id: 333, shop_id: 20, title: "Humic", sequence_no: 3 }]
    }];
  };
  global.window = { webpackJsonp: [] };
  window.webpackJsonp.push = (payload) => {
    const id = Object.keys(payload[1])[0];
    payload[1][id]({}, {}, (moduleId) => {
      assert.equal(moduleId, 86);
      return { b: list, s: async (session, raw) => {
        writes.push({ session, raw });
        ui.memoizedProps.showingProduct = {itemId:raw.item_id,shopId:raw.shop_id,name:raw.title};
        return [null, {}]; },
        o: async (session) => { writes.push({ session, hide: true }); ui.memoizedProps.showingProduct=null; return [null, {}]; } };
    });
  };
  const result = await runShopeeApi({ action: "catalog" });
  assert.equal(result.ok, true);
  assert.equal(result.product_count, 3);
  assert.equal(result.total_count, 3);
  assert.deepEqual(calls.map((x) => x.offset), [0, 2]);
  assert.ok(calls.every((x) => x.visible === true && x.useNew === true));
  assert.equal(writes.length, 0);
  assert.deepEqual(result.products.map((x) => x.product_id), ["111", "222", "333"]);
  assert.equal((await runShopeeApi({ action: "show" })).ok, false);
  assert.equal(writes.length, 0);
  const shown = await runShopeeApi({ action: "show", product_id: "222", shop_id: "10" });
  assert.equal(shown.ok, true);
  assert.equal(shown.confirmed, true);
  assert.equal(shown.confirmation_source, "shopee_ui");
  assert.equal((await runShopeeApi({action:"state"})).product_id, "222");
  assert.deepEqual(writes[0], { session: "41590640", raw: { item_id: 222, shop_id: 10, title: "Đạm cá", sequence_no: 2 } });
  assert.equal((await runShopeeApi({ action: "show", product_id: "999", shop_id: "10" })).ok, false);
  assert.equal(writes.length, 1);
  assert.equal((await runShopeeApi({ action: "hide" })).confirmed, true);
  assert.deepEqual(writes[1], { session: "41590640", hide: true });
  ui.memoizedProps.sessionId=999;
  assert.equal((await runShopeeApi({action:"state"})).available, false, "Never confirm from another session");
  assert.equal((await runShopeeApi({action:"show",product_id:"222",shop_id:"10"})).confirmed, false);
  console.log("Shopee API tests passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
