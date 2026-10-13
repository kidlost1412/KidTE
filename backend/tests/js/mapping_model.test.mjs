import test from 'node:test';
import assert from 'node:assert/strict';
import {tierInfo, suggestionIndex, sureAssignments, countTiers, completenessChips, canApprove, missingForApproval, draftRows, approvalItems, approvalSummary, SAVE_NOTE, rowThumbs} from '../../src/web/app/pages/mapping-model.js';

const R = (id, tier, extra = {}) => ({product_id: id, title: 't' + id, profile_id: 'P' + id, profile_name: 'Hồ sơ ' + id, tier, size_match: true, is_combo: false, score: 0.9, reasons: [], ...extra});

test('tiers have Vietnamese labels and unknown tiers fall back to "không có"', () => {
  assert.equal(tierInfo('chac_chan').label, 'Chắc chắn');
  assert.equal(tierInfo('can_xem').tone, 'warn');
  assert.equal(tierInfo('???').label, 'Không có gợi ý');
});

test('saving is explicitly described as letting the AI use the profile', () => {
  assert.match(SAVE_NOTE, /AI được dùng hồ sơ/);
});

test('suggestionIndex keeps only rows that have a suggested profile', () => {
  const idx = suggestionIndex([R('1', 'chac_chan'), R('2', 'khong_co', {profile_id: ''}), R('3', 'can_xem'), R('4', 'khong_co')]);
  assert.deepEqual([...idx.keys()], ['1', '3']);
});

test('sureAssignments accepts only sure pairs, never size mismatch or combo', () => {
  const results = [R('1', 'chac_chan'), R('2', 'can_xem'), R('3', 'chac_chan', {size_match: false}), R('4', 'chac_chan', {is_combo: true}), R('5', 'chac_chan', {profile_id: ''})];
  assert.deepEqual(sureAssignments(results), {1: 'P1'});
});

test('sureAssignments respects skipped, already pending and already mapped rows', () => {
  const results = [R('1', 'chac_chan'), R('2', 'chac_chan'), R('3', 'chac_chan'), R('4', 'chac_chan')];
  const out = sureAssignments(results, {skipped: new Set(['1']), pending: new Map([['2', 'X']]), mapped: new Set(['3'])});
  assert.deepEqual(out, {4: 'P4'});
});

test('countTiers tallies every tier', () => {
  assert.deepEqual(countTiers([R('1', 'chac_chan'), R('2', 'can_xem'), R('3', 'can_xem'), R('4', 'khong_co')]), {chac_chan: 1, can_xem: 2, khong_co: 1});
  assert.deepEqual(countTiers(undefined), {chac_chan: 0, can_xem: 0, khong_co: 0});
});

test('completeness chips follow the four content sections', () => {
  const chips = completenessChips({thanh_phan: true, cong_dung: false, huong_dan: true});
  assert.deepEqual(chips.map(c => [c.label, c.done]), [['Thành phần', true], ['Công dụng', false], ['Hướng dẫn', true], ['Lưu ý', false]]);
});

test('canApprove mirrors the backend rule: name plus uses or directions', () => {
  assert.equal(canApprove({name: 'A', completeness: {cong_dung: true}}), true);
  assert.equal(canApprove({name: 'A', completeness: {huong_dan: true}}), true);
  assert.equal(canApprove({name: 'A', completeness: {thanh_phan: true, luu_y: true}}), false);
  assert.equal(canApprove({name: '', completeness: {cong_dung: true}}), false);
  assert.equal(canApprove({name: 'A', approvable: false, completeness: {cong_dung: true}}), false);
  assert.equal(canApprove(null), false);
  assert.match(missingForApproval({name: 'A', completeness: {}}), /Công dụng hoặc Hướng dẫn/);
  assert.equal(missingForApproval({name: 'A', completeness: {cong_dung: true}}), '');
});

test('draftRows lists only local unapproved profiles, ready and fuller ones first', () => {
  const profiles = [
    {id: 'A', name: 'Zeta', created_local: true, status: 'draft', completeness: {}},
    {id: 'B', name: 'Beta', created_local: true, status: 'draft', completeness: {cong_dung: true, luu_y: true}, approvable: true, draft_for: 'sku9'},
    {id: 'C', name: 'Gamma', created_local: true, status: 'approved', completeness: {cong_dung: true}},
    {id: 'D', name: 'Shared', created_local: false, status: 'approved', completeness: {}},
    {id: 'E', name: 'Alpha', created_local: true, status: 'draft', completeness: {thanh_phan: true}},
  ];
  const rows = draftRows(profiles);
  assert.deepEqual(rows.map(r => r.id), ['B', 'E', 'A']);
  assert.equal(rows[0].ready, true);
  assert.equal(rows[1].ready, false);
});

test('approvalItems drops selected rows that are not ready and carries the source listing', () => {
  const rows = draftRows([
    {id: 'B', name: 'Beta', created_local: true, status: 'draft', completeness: {cong_dung: true}, approvable: true, draft_for: 'sku9'},
    {id: 'A', name: 'Zeta', created_local: true, status: 'draft', completeness: {}, draft_for: 'sku1'},
  ]);
  assert.deepEqual(approvalItems(rows, new Set(['A', 'B'])), [{profile_id: 'B', product_id: 'sku9'}]);
  assert.deepEqual(approvalItems(rows, new Set()), []);
});

test('approvalSummary reports blocked rows', () => {
  assert.match(approvalSummary({results: [{ok: true}, {ok: true}]}), /Đã duyệt 2/);
  assert.match(approvalSummary({results: [{ok: true}, {ok: false}]}), /1 hồ sơ chưa đủ nội dung/);
});

test('rowThumbs shows the assigned profile image and marks a suggestion as not yet assigned', () => {
  const profiles = [{id: 'SP-03', name: 'Kích Rễ', image_url: '/api/profile/thumb/SP-03?v=1'}, {id: 'SP-09', name: 'Sạch Sâu', image_url: ''}];
  const assigned = rowThumbs({product_id: 'p1', profile_id: 'SP-03', image_url: '/api/product/thumb/p1?v=2', title: 'T1'}, 'SP-03', profiles);
  assert.equal(assigned.listing.url, '/api/product/thumb/p1?v=2');
  assert.equal(assigned.profile.url, '/api/profile/thumb/SP-03?v=1');
  assert.equal(assigned.profile.label, 'Kích Rễ');
  assert.equal(assigned.profileSuggested, false);
  const suggested = rowThumbs({product_id: 'p5', profile_id: '', image_url: ''}, 'SP-09', profiles);
  assert.equal(suggested.listing.url, '');
  assert.equal(suggested.profile.url, '');
  assert.equal(suggested.profileSuggested, true);
  const none = rowThumbs({product_id: 'p7'}, '', profiles);
  assert.equal(none.profile.label, '');
  assert.equal(none.profileSuggested, false);
});
