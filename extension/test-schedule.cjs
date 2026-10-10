const assert = require("node:assert/strict");
const S = require("./schedule.js");
assert.strictEqual(globalThis.KidTESchedule, S);
assert.strictEqual(globalThis.LiveReSchedule, S);

function keyAt(seconds) {
  return S.resolve(seconds * 1000).productKey;
}

// Chu ky phai bang do dai THAT cua live1.mp4 (1140.77s do tu audio), khong
// phai 19:00.000 - lech 0.77s moi vong se don lai thanh ghim sai san pham.
assert.equal(S.CYCLE_MS, 1140770);
assert.equal(S.REPIN_EVERY_MS, 20 * 1000);
assert.equal(S.parseClock("07:35"), 455000);
assert.equal(S.parseClock("27:15"), 1635000);
assert.equal(S.parseClock("1:02:03"), 3723000);
assert.equal(S.parseClock("90"), 90000);
assert.equal(S.parseClock("7:60"), null);
assert.equal(S.parseClock("abc"), null);
assert.equal(S.parseClock(""), null);

assert.equal(keyAt(0), "tc01");
assert.equal(keyAt(166.999), "tc01");
assert.equal(keyAt(167), "topone");
assert.equal(keyAt(301.999), "topone");
assert.equal(keyAt(302), null);
assert.equal(keyAt(389.999), null);
assert.equal(keyAt(390), "tc01");
assert.equal(keyAt(509.999), "tc01");
assert.equal(keyAt(510), "fish1");
assert.equal(keyAt(569.999), "fish1");
assert.equal(keyAt(570), "fish5");
assert.equal(keyAt(630), "fish1");
assert.equal(keyAt(690), "fish5");
assert.equal(keyAt(726.999), "fish5");
assert.equal(keyAt(727), "topone");
assert.equal(keyAt(940), "fish1");
assert.equal(keyAt(999.999), "fish1");
assert.equal(keyAt(1000), "topone");
assert.equal(keyAt(1040), "soil");
assert.equal(keyAt(1080), "topone");
// Vong lap dung o 1140.77s (do dai that cua file), khong phai 1140s.
assert.equal(keyAt(1140.769), "topone");
assert.equal(keyAt(1140.77), "tc01");
assert.equal(S.resolve(1140 * 1000).loopIndex, 0, "1140s van thuoc vong 1");
assert.equal(S.resolve(S.CYCLE_MS).loopIndex, 1);
// 27:15 = 1635.000s; tru mot chu ky 1140.770s con 494.230s = 08:14.23
assert.equal(S.resolve(S.parseClock("27:15")).positionMs, 494230);
assert.equal(S.resolve(S.parseClock("27:15")).productKey, "tc01");
assert.equal(S.resolve(8 * S.CYCLE_MS + 167000).productKey, "topone");
assert.match(S.scriptContextAt(45000).topic, /TC01/);
assert.match(S.scriptContextAt(200000).topic, /Top One/);
assert.match(S.scriptContextAt(1050000).topic, /Sạch Sâu Đất/);

assert.deepEqual(
  { at: S.nextBoundary(0).atPositionMs, key: S.nextBoundary(0).productKey },
  { at: 167000, key: "topone" }
);
assert.deepEqual(
  { at: S.nextBoundary(510000).atPositionMs, key: S.nextBoundary(510000).productKey },
  { at: 570000, key: "fish5" }
);
assert.deepEqual(
  { at: S.nextBoundary(1130000).atPositionMs, key: S.nextBoundary(1130000).productKey },
  { at: 0, key: "tc01" }
);

console.log("LIVE_RE_SCHEDULE_TEST_OK");
