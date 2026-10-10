"use strict";
/**
 * Khoa lai chot nghiep vu cua Video 2 bang chinh logic ghim that.
 *
 * Ke hoach yeu cau bao toan: 15 moc, 6 san pham, hai doan
 * "Combo Dam ca + Humic" dai hon hai phut luan phien voi Dam ca 5L theo chu
 * ky 60 giay, va doan combo NGAN khong bi ap luan phien ngoai quy tac.
 * Ngoai ra phai kiem giay truoc/sau moi lan chuyen moc va khi video lap vong.
 *
 * Timeline that cua may van hanh la du lieu rieng, khong nam trong ma nguon.
 * O day dung mot ban tai dung dung cau truc va dung cac con so da chot, nen
 * test chay duoc tren ban checkout sach; khi co timeline that thi dat bien
 * LIVE_RE_TIMELINE tro toi file do de kiem tren chinh no.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");

const schedule = require("./schedule.js");

const SAN_PHAM = {
  topone: { title: "Bo tri Top One SPI GGO 10SC 200ml" },
  tc01: { title: "Kich re TC01 100ml" },
  fish1: { title: "Dam ca Amino 1L" },
  fish5: { title: "Dam ca Amino 5L" },
  combo_fish_humic: { title: "Combo Dam ca + Humic" },
  nano_dong_500: { title: "Nano Dong 500ml" }
};

// Dung cac moc da chot (giay -> san pham), giong het bo tren may van hanh.
const MOC = [
  [0, "topone"], [180, "tc01"], [441, "fish1"], [600, "fish5"],
  [757, "combo_fish_humic", "fish5", 60000],
  [917, "tc01"], [1328, "combo_fish_humic"], [1356, "tc01"],
  [1513, "combo_fish_humic", "fish5", 60000],
  [1745, "topone"], [2053, "fish5"], [2136, "tc01"], [2252, "fish1"],
  [2372, "nano_dong_500"], [2470, "topone"]
];
const CYCLE_MS = 2573588;

function timelineMacDinh() {
  return {
    schema_version: 1,
    video_id: "live2",
    cycle_ms: CYCLE_MS,
    repin_every_ms: 30000,
    products: Object.fromEntries(Object.entries(SAN_PHAM).map(([key, val], i) => [
      key, { bag_index: i + 1, include_all: [val.title], exclude_any: [] }
    ])),
    events: MOC.map(([giay, sp, alt, every]) => {
      const e = { at_ms: giay * 1000, product: sp };
      if (alt) { e.alternate_with = alt; e.alternate_every_ms = every; }
      return e;
    }),
    script_contexts: []
  };
}

const duongDan = process.env.LIVE_RE_TIMELINE;
const timeline = duongDan
  ? JSON.parse(fs.readFileSync(duongDan, "utf8"))
  : timelineMacDinh();

assert.deepEqual(schedule.validate(timeline), [], "timeline phai hop le");
schedule.load(timeline);

// ------------------------------------------------------------------
// 1. Cau truc da chot
// ------------------------------------------------------------------
assert.equal(timeline.events.length, 15, "Video 2 phai co dung 15 moc");
assert.equal(Object.keys(timeline.products).length, 6,
  "Video 2 phai co dung 6 san pham");

const luanPhien = timeline.events.filter((e) => e.alternate_with);
assert.equal(luanPhien.length, 2, "dung hai doan luan phien");
for (const e of luanPhien) {
  assert.equal(e.alternate_every_ms, 60000, "chu ky luan phien la 60 giay");
  assert.equal(e.alternate_with, "fish5", "luan phien voi Dam ca 5L");
  assert.equal(e.product, "combo_fish_humic", "doan luan phien la combo");
}

// Doan combo NGAN khong duoc co luan phien.
const comboNgan = timeline.events.filter(
  (e) => e.product === "combo_fish_humic" && !e.alternate_with);
assert.equal(comboNgan.length, 1, "co dung mot doan combo ngan");
for (const e of comboNgan) {
  const sau = timeline.events.find((x) => x.at_ms > e.at_ms);
  const daiMs = (sau ? sau.at_ms : timeline.cycle_ms) - e.at_ms;
  assert.ok(daiMs < 120000,
    `doan combo khong luan phien phai ngan hon hai phut, dang la ${daiMs}ms`);
}
for (const e of luanPhien) {
  const sau = timeline.events.find((x) => x.at_ms > e.at_ms);
  const daiMs = (sau ? sau.at_ms : timeline.cycle_ms) - e.at_ms;
  assert.ok(daiMs > 120000,
    `doan combo luan phien phai dai hon hai phut, dang la ${daiMs}ms`);
}

// ------------------------------------------------------------------
// 2. Giay truoc / dung / sau moi lan chuyen moc
// ------------------------------------------------------------------
for (let i = 0; i < timeline.events.length; i += 1) {
  const e = timeline.events[i];
  const truoc = timeline.events[i - 1];
  if (e.at_ms > 0) {
    assert.equal(schedule.resolve(e.at_ms - 1).eventAtMs, truoc.at_ms,
      `giay truoc moc ${i + 1} phai con thuoc moc truoc do`);
  }
  assert.equal(schedule.resolve(e.at_ms).eventAtMs, e.at_ms,
    `dung giay cua moc ${i + 1} phai da chuyen`);
  assert.equal(schedule.resolve(e.at_ms + 1).eventAtMs, e.at_ms,
    `giay sau moc ${i + 1} van thuoc moc do`);
}

// ------------------------------------------------------------------
// 3. Luan phien 60 giay chay dung trong suot doan dai
// ------------------------------------------------------------------
for (const e of luanPhien) {
  const sau = timeline.events.find((x) => x.at_ms > e.at_ms);
  const het = sau ? sau.at_ms : timeline.cycle_ms;
  for (let t = e.at_ms; t < het; t += 1000) {
    const pha = Math.floor((t - e.at_ms) / 60000);
    const mongDoi = pha % 2 === 0 ? e.product : e.alternate_with;
    assert.equal(schedule.resolve(t).productKey, mongDoi,
      `tai ${t}ms (pha ${pha}) phai ghim ${mongDoi}`);
  }
  // Ranh gioi doi phien: ngay truoc va ngay tai moc 60s.
  for (let pha = 1; e.at_ms + pha * 60000 < het; pha += 1) {
    const moc = e.at_ms + pha * 60000;
    assert.notEqual(schedule.resolve(moc - 1).productKey,
      schedule.resolve(moc).productKey,
      `phai doi san pham ngay tai giay thu ${pha * 60}`);
  }
}

// ------------------------------------------------------------------
// 4. Video lap vong: vong 2 phai giong het vong 1
// ------------------------------------------------------------------
for (let t = 0; t < timeline.cycle_ms; t += 5000) {
  const v1 = schedule.resolve(t);
  const v2 = schedule.resolve(t + timeline.cycle_ms);
  assert.equal(v2.productKey, v1.productKey,
    `vong 2 tai ${t}ms phai ghim y het vong 1`);
  assert.equal(v2.loopIndex, v1.loopIndex + 1, "chi so vong phai tang");
  assert.notEqual(v2.transitionToken, v1.transitionToken,
    "token chuyen phai khac giua hai vong de ghim lai duoc");
}
// Ngay tai diem lap vong.
assert.equal(schedule.resolve(timeline.cycle_ms).productKey,
  schedule.resolve(0).productKey, "quay ve dau vong phai ghim san pham dau");
assert.equal(schedule.resolve(timeline.cycle_ms - 1).eventAtMs,
  timeline.events.at(-1).at_ms, "giay cuoi vong van thuoc moc cuoi");

// ------------------------------------------------------------------
// 5. Moc ke tiep bao dung ca khi dang trong doan luan phien
// ------------------------------------------------------------------
const trongLuanPhien = luanPhien[0].at_ms + 10000;
const ke = schedule.nextBoundary(trongLuanPhien);
assert.equal(ke.inMs, 50000, "con 50 giay nua la doi phien");
assert.equal(ke.productKey, luanPhien[0].alternate_with,
  "doi phien thi sang Dam ca 5L");

console.log("LIVE_RE_VIDEO2_TIMELINE_TEST_OK");
