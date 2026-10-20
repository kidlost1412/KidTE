// node --test backend/tests/js
import test from 'node:test';
import assert from 'node:assert/strict';
import * as M from '../../src/web/app/pages/video/model.js';

const timeline = () => ({
  video_id: 'v1', title: 'Live 1', cycle_ms: 600000, repin_every_ms: 20000,
  products: {
    sku_1: {product_id: '1', bag_index: 1, short_name: 'TC01', title: 'TC01 dài', include_all: ['tc01'], exclude_any: []},
    sku_2: {product_id: '2', bag_index: 2, short_name: 'Đạm cá', title: 'Đạm cá dài', include_all: ['dam ca'], exclude_any: []},
    fish5: {bag_index: 4, short_name: 'Đạm 5L', title: 'Đạm 5L', include_all: ['dam ca 5'], exclude_any: []},
  },
  events: [
    {at_ms: 0, product: 'sku_1'},
    {at_ms: 60000, product: 'sku_2', alternate_with: 'fish5', alternate_every_ms: 60000},
    {at_ms: 300000, product: null},
  ],
  script_contexts: [{at_ms: 0, topic: 'Mở đầu', facts: ['a']}, {at_ms: 60000, topic: 'Đạm', facts: []}, {at_ms: 300000, topic: 'Nghỉ', facts: []}],
});

test('library round-trip giữ nguyên events và script_contexts', () => {
  const doc = M.fromLibrary(timeline());
  assert.equal(doc.segments.length, 3);
  assert.equal(doc.segments[0].topic, 'Mở đầu');
  const out = M.toLibraryChanges(doc);
  assert.deepEqual(out.events, timeline().events);
  assert.deepEqual(out.script_contexts, timeline().script_contexts);
});

test('withEnds tính điểm kết thúc = mốc kế tiếp hoặc hết video', () => {
  const ends = M.withEnds(M.fromLibrary(timeline())).map(s => s.end);
  assert.deepEqual(ends, [60000, 300000, 600000]);
});

test('pinnedAt xử lý luân phiên theo chu kỳ', () => {
  const doc = M.fromLibrary(timeline());
  assert.equal(M.pinnedAt(doc, 30000).product, 'sku_1');
  assert.equal(M.pinnedAt(doc, 90000).product, 'sku_2');
  assert.equal(M.pinnedAt(doc, 150000).product, 'fish5');
  assert.equal(M.pinnedAt(doc, 400000).product, null);
});

test('splitAt chèn mốc mới, giữ SP cũ nếu không chỉ định; mốc sát nhau thì dùng lại', () => {
  let doc = M.fromLibrary(timeline());
  const r = M.splitAt(doc, 120000);
  assert.equal(r.doc.segments.length, 4);
  assert.equal(r.doc.segments[r.index].start, 120000);
  assert.equal(r.doc.segments[r.index].product, 'sku_2');
  const again = M.splitAt(r.doc, 120300, 'sku_1');
  assert.equal(again.doc.segments.length, 4, 'không tạo mốc trùng trong 0.5s');
  assert.equal(again.doc.segments[again.index].product, 'sku_1');
  doc = again.doc;
  assert.equal(doc.segments.map(s => s.start).join(','), '0,60000,120000,300000');
});

test('pinFrom bỏ qua khi đang đúng sản phẩm', () => {
  const doc = M.fromLibrary(timeline());
  const r = M.pinFrom(doc, 20000, 'sku_1');
  assert.equal(r.unchanged, true);
  const r2 = M.pinFrom(doc, 20000, 'sku_2');
  assert.equal(r2.doc.segments.length, 4);
});

test('moveStart bị kẹp giữa hai đoạn kề và không cho đổi mốc 0 của timeline', () => {
  const doc = M.fromLibrary(timeline());
  assert.equal(M.moveStart(doc, 0, 5000), doc);
  assert.equal(M.moveStart(doc, 1, -10).segments[1].start, M.MIN_SEGMENT_MS);
  assert.equal(M.moveStart(doc, 1, 999999).segments[1].start, 300000 - M.MIN_SEGMENT_MS);
});

test('removeAt: đoạn giữa -> đoạn trước kéo dài; đoạn đầu -> đoạn sau về 0', () => {
  const doc = M.fromLibrary(timeline());
  const mid = M.removeAt(doc, 1).doc;
  assert.deepEqual(mid.segments.map(s => s.start), [0, 300000]);
  const first = M.removeAt(doc, 0).doc;
  assert.deepEqual(first.segments.map(s => [s.start, s.product]), [[0, 'sku_2'], [300000, null]]);
});

test('checks phát hiện giỏ lệch, SP không còn bán, đoạn quá ngắn', () => {
  let doc = M.fromLibrary(timeline());
  doc = M.splitAt(doc, 62000, 'sku_1').doc; // đoạn 60s-62s = 2s
  const issues = M.checks(doc, {1: {bag_index: 3, name: 'TC01'}, 2: {bag_index: 2, name: 'Đạm'}});
  const codes = issues.map(i => i.code);
  assert.ok(codes.includes('bag_changed'));
  assert.ok(codes.includes('not_in_catalog'), 'fish5 không có product_id trong danh mục');
  assert.ok(codes.includes('short'));
  assert.equal(M.blocking(issues), false);
});

test('review: đọc gợi ý AI, sửa -> edited, ghi lại đúng product_id', () => {
  const job = {job_id: 'j', duration_s: 600, draft: {
    title: 'liv3', cycle_ms: 600000,
    catalog: [{product_id: '1', bag_index: 1, title: 'TC01'}, {product_id: '2', bag_index: 2, title: 'Đạm'}],
    events: [
      {at_ms: 30000, product_id: '1', confidence: 'high', evidence: 'tc01 nè', review_status: 'pending', candidates: []},
      {at_ms: 90000, product_id: '2', confidence: 'medium', evidence: 'đạm cá', review_status: 'pending'},
    ]}};
  let doc = M.fromReview(job);
  assert.equal(doc.kind, 'review');
  assert.equal(M.reviewCounts(doc).pending, 2);
  assert.ok(M.checks(doc).some(i => i.code === 'pending'));
  doc = M.update(doc, 1, {product: 'sku_1'});
  assert.equal(doc.segments[1].review, 'edited');
  doc = M.setReview(doc, [0], 'approved');
  const draft = M.toReviewDraft(doc, job.draft);
  assert.deepEqual(draft.events.map(e => [e.at_ms, e.product_id, e.review_status]), [[30000, '1', 'approved'], [90000, '1', 'edited']]);
  assert.equal(draft.events[0].evidence, 'tc01 nè', 'giữ bằng chứng AI');
  assert.equal(M.pinnedAt(doc, 5000).product, 'sku_1', 'trước mốc đầu vẫn ghim SP mốc đầu');
});

test('history undo/redo', () => {
  const doc = M.fromLibrary(timeline());
  const h = M.createHistory(doc);
  const next = M.splitAt(doc, 100000).doc;
  h.push(next, 'cắt');
  assert.equal(h.get().segments.length, 4);
  assert.equal(h.undo().label, 'cắt');
  assert.equal(h.get().segments.length, 3);
  h.redo();
  assert.equal(h.get().segments.length, 4);
});

test('library draft mở lại được đúng nội dung', () => {
  let doc = M.fromLibrary(timeline());
  doc = M.splitAt(doc, 200000, 'sku_1').doc;
  doc = {...doc, title: 'Đổi tên'};
  const draft = M.toLibraryDraft(doc);
  const reopened = M.fromLibrary(timeline(), {draft});
  assert.equal(reopened.title, 'Đổi tên');
  assert.equal(reopened.segments.length, 4);
  assert.ok(M.sameContent(reopened, doc));
});

test('findPinMismatches: host nhắc SP khác nhiều lần -> đề xuất ghim', () => {
  const doc = M.fromLibrary(timeline());
  const mentions = [
    {ms: 20000, endMs: 22000, key: 'sku_2', text: 'đạm cá nè'},
    {ms: 30000, endMs: 33000, key: 'sku_2', text: 'đạm cá tốt'},
    {ms: 70000, endMs: 72000, key: 'sku_2'}, {ms: 80000, endMs: 82000, key: 'sku_2'}, // đã ghim đúng
    {ms: 400000, endMs: 401000, key: 'sku_1'}, // chỉ 1 lần -> bỏ qua
  ];
  const out = M.findPinMismatches(doc, mentions);
  assert.equal(out.length, 1);
  assert.equal(out[0].key, 'sku_2');
  assert.equal(out[0].startMs, 20000);
  assert.equal(out[0].currentKey, 'sku_1');
});

test('findPinMismatches bỏ qua khi đã có mốc đúng SP gần đó', () => {
  let doc = M.fromLibrary(timeline());
  doc = M.splitAt(doc, 25000, 'sku_2').doc;
  const out = M.findPinMismatches(doc, [{ms: 20000, endMs: 21000, key: 'sku_2'}, {ms: 26000, endMs: 27000, key: 'sku_2'}]);
  assert.equal(out.length, 0);
});

test('nextPending vòng lại từ đầu', () => {
  const job = {job_id: 'j', duration_s: 100, draft: {cycle_ms: 100000, catalog: [], events: [
    {at_ms: 0, product_id: '1', review_status: 'pending'}, {at_ms: 10000, product_id: '1', review_status: 'approved'}, {at_ms: 20000, product_id: '1', review_status: 'pending'}]}};
  const doc = M.fromReview(job);
  assert.equal(M.nextPending(doc, 0), 2);
  assert.equal(M.nextPending(doc, 2), 0);
  assert.equal(M.nextPending(M.setReview(doc, [0, 2], 'approved'), 0), -1);
});

test('withEnds nhớ kết quả theo mảng đoạn', () => {
  const doc = M.fromLibrary(timeline());
  assert.equal(M.withEnds(doc), M.withEnds(doc));
  const next = M.splitAt(doc, 100000).doc;
  assert.notEqual(M.withEnds(next), M.withEnds(doc));
});

test('pinRange ghim đúng khoảng rồi trả lại sản phẩm cũ', () => {
  const doc = M.fromLibrary(timeline());           // 0-60 sku_1, 60-300 sku_2/alt, 300- none
  const r = M.pinRange(doc, 100000, 150000, 'sku_1');
  const starts = r.doc.segments.map(s => [s.start, s.product]);
  assert.deepEqual(starts, [[0, 'sku_1'], [60000, 'sku_2'], [100000, 'sku_1'], [150000, 'sku_2'], [300000, null]]);
  assert.equal(r.doc.segments[r.index].start, 100000);
  // mốc kế tiếp đã sát -> không thêm mốc trả lại
  const r2 = M.pinRange(doc, 280000, 295000, 'sku_1');
  assert.deepEqual(r2.doc.segments.map(s => s.start), [0, 60000, 280000, 300000]);
});

test('timeline remembers its cart (cart_key) across save/load', async () => {
  const M = await import('../../src/web/app/pages/video/model.js');
  const timeline = {video_id: 'v1', cycle_ms: 60000, products: {}, events: [{at_ms: 0, product: null}], catalog_cart: 'uid:1'};
  const doc = M.fromLibrary(timeline);
  assert.equal(doc.cartKey, 'uid:1');
  const next = {...doc, cartKey: 'uid:2'};
  assert.equal(M.toLibraryChanges(next).cart_key, 'uid:2');
  assert.equal(M.fromLibrary(timeline, {draft: M.toLibraryDraft(next)}).cartKey, 'uid:2');
  assert.equal(M.sameContent(doc, next), false);
  assert.equal('cart_key' in M.toLibraryChanges({...doc, cartKey: ''}), false);
  const review = M.fromReview({job_id: 'j', duration_s: 10, catalog_cart: 'uid:3', draft: {events: []}});
  assert.equal(review.cartKey, 'uid:3');
  assert.equal(M.toReviewDraft({...review, cartKey: 'uid:4'}, {events: []}).catalog_cart, 'uid:4');
});
