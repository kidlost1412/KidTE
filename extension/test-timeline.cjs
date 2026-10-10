"use strict";
// Lich ghim la du lieu cua tung video. Bo test nay khoa hai thu: co may moi
// cho ket qua GIONG HET ban hard-code cu, va mot timeline sai khong bao gio
// duoc nap len live.
const assert = require("node:assert/strict");
const S = require("./schedule.js");
const fs = require("node:fs");
const path = require("node:path");

// Ban mau co dinh nam canh test (khong phu thuoc du lieu rieng cua may).
function findTimeline(videoId) {
  const candidate = path.join(__dirname, "fixtures", videoId + ".timeline.json");
  if (!fs.existsSync(candidate)) throw new Error("Thieu ban mau " + candidate);
  return candidate;
}

const LIVE1 = JSON.parse(fs.readFileSync(findTimeline("live1"), "utf8"));
const LIVE2 = JSON.parse(fs.readFileSync(findTimeline("live2"), "utf8"));

// --- 1. timeline.json tren dia phai khop ban mac dinh nhung trong code ---
const loaded = S.load(LIVE1);
assert.equal(loaded.ok, true, "timeline.json cua live1 phai hop le: "
  + JSON.stringify(loaded.errors || []));
assert.equal(S.VIDEO_ID, "live1");
assert.equal(S.CYCLE_MS, LIVE1.cycle_ms);
for (let s = 0; s < 1141; s += 1) {
  const fromFile = S.resolve(s * 1000).productKey;
  S.load(S.DEFAULT_TIMELINE);
  const fromCode = S.resolve(s * 1000).productKey;
  S.load(LIVE1);
  assert.equal(fromFile, fromCode, `lech tai ${s}s`);
}

// --- 2. doi video la doi ca bo san pham va chu ky ---
const OTHER = {
  schema_version: 1, video_id: "live2", title: "Video 2", cycle_ms: 300000,
  repin_every_ms: 15000,
  products: {
    nano: { bag_index: 1, short_name: "Nano Đồng", title: "Nano Đồng 250ml",
      include_all: ["nano dong"], exclude_any: [] },
    tonic: { bag_index: 2, short_name: "Nano Tonic", title: "Nano Tonic 250ml",
      include_all: ["nano tonic"], exclude_any: [] }
  },
  events: [
    { at_ms: 0, product: "nano" },
    { at_ms: 120000, product: "tonic" },
    { at_ms: 240000, product: null }
  ],
  script_contexts: [{ at_ms: 0, topic: "Nano Đồng", facts: ["Rửa nấm khuẩn bề mặt."] }]
};
assert.equal(S.load(OTHER).ok, true);
assert.equal(S.VIDEO_ID, "live2");
assert.equal(S.CYCLE_MS, 300000);
assert.equal(S.REPIN_EVERY_MS, 15000);
assert.deepEqual(Object.keys(S.PRODUCTS), ["nano", "tonic"]);
assert.equal(S.resolve(10000).productKey, "nano");
assert.equal(S.resolve(130000).productKey, "tonic");
assert.equal(S.resolve(250000).productKey, null);
// vong lap theo chu ky moi
assert.equal(S.resolve(300000).loopIndex, 1);
assert.equal(S.resolve(310000).productKey, "nano");
assert.equal(S.scriptContextAt(0).topic, "Nano Đồng");

// --- 3. timeline sai khong duoc nap, ban dang dung phai giu nguyen ---
const bad = [
  [{}, "thieu het"],
  [{ ...OTHER, cycle_ms: 0 }, "cycle_ms = 0"],
  [{ ...OTHER, video_id: "" }, "thieu video_id"],
  [{ ...OTHER, products: {} }, "products rong"],
  [{ ...OTHER, events: [] }, "events rong"],
  [{ ...OTHER, events: [{ at_ms: 5000, product: "nano" }] }, "moc dau khong phai 0"],
  [{ ...OTHER, events: [{ at_ms: 0, product: "khong-co" }] }, "product la"],
  [{ ...OTHER, events: [{ at_ms: 0, product: "nano" }, { at_ms: 0, product: "tonic" }] }, "at_ms khong tang"],
  [{ ...OTHER, events: [{ at_ms: 0, product: "nano" }, { at_ms: 999999, product: "tonic" }] }, "at_ms vuot chu ky"],
  [{ ...OTHER, events: [{ at_ms: 0, product: "nano", alternate_with: "tonic" }] }, "thieu alternate_every_ms"]
];
for (const [timeline, label] of bad) {
  const result = S.load(timeline);
  assert.equal(result.ok, false, `phai tu choi: ${label}`);
  assert.ok(result.errors.length, label);
  assert.equal(S.VIDEO_ID, "live2", `phai giu lich dang dung sau khi tu choi ${label}`);
}

// --- 4. luan phien 1L/5L tong quat, khong con dong cung fish ---
const ALT = {
  ...OTHER, video_id: "live3",
  events: [{ at_ms: 0, product: "nano", alternate_with: "tonic",
             alternate_every_ms: 30000 }]
};
assert.equal(S.load(ALT).ok, true);
assert.equal(S.resolve(0).productKey, "nano");
assert.equal(S.resolve(29999).productKey, "nano");
assert.equal(S.resolve(30000).productKey, "tonic");
assert.equal(S.resolve(60000).productKey, "nano");
assert.equal(S.nextBoundary(0).inMs, 30000);

// --- 5. live2 da duoc chu shop duyet: combo dai luan phien voi can 5L ---
assert.equal(S.load(LIVE2).ok, true);
assert.equal(S.VIDEO_ID, "live2");
assert.equal(S.CYCLE_MS, 2573588);
assert.equal(S.resolve(757000).productKey, "combo_fish_humic");
assert.equal(S.resolve(817000).productKey, "fish5");
assert.equal(S.resolve(877000).productKey, "combo_fish_humic");
assert.equal(S.resolve(916999).productKey, "combo_fish_humic");
assert.equal(S.resolve(917000).productKey, "tc01");
assert.equal(S.resolve(1513000).productKey, "combo_fish_humic");
assert.equal(S.resolve(1573000).productKey, "fish5");
assert.equal(S.resolve(1633000).productKey, "combo_fish_humic");
assert.equal(S.resolve(1693000).productKey, "fish5");
assert.equal(S.resolve(1745000).productKey, "topone");
assert.equal(S.resolve(LIVE2.cycle_ms).productKey, "topone");

S.load(LIVE1);
assert.equal(S.VIDEO_ID, "live1");
console.log("LIVE_RE_TIMELINE_TEST_OK");
