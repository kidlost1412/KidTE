import test from 'node:test';
import assert from 'node:assert/strict';
import {buildReviewModel, markEdited, uncertainCount, canSaveDraft, fieldsPayload, draftBody, defaultImageSource, linesToPairs, pairsToLines, suggestionLabel, needsGeminiWarning, sourceLabel} from '../../src/web/app/pages/product-ingest-model.js';

const RESULT = {
  provider: 'gemini', session_id: 'sess1', filtered_price: true,
  fields: {ten_san_pham: 'Đạm Cá 5L', nha_san_xuat: null, quy_cach_dong_goi: 'can 5 lít', thanh_phan_dang_ky: {'Đạm': '5%', 'Lân': ''},
    cong_dung: ['Kích rễ', 'Xanh lá'], doi_tuong_cay_trong: null, huong_dan_su_dung: ['Pha 20ml/16 lít'], luu_y: 'Tránh nắng', mo_ta_ngan: 'Tóm tắt', phan_khuc: null, hieu_qua_chinh: null},
  confidence: {ten_san_pham: 0.95, quy_cach_dong_goi: 0.9, thanh_phan_dang_ky: 0.8, cong_dung: 0.8, huong_dan_su_dung: 0.9, luu_y: 0.3, mo_ta_ngan: 0.7},
  nguon: {ten_san_pham: 'n1', cong_dung: 'n2'},
  uncertain: ['nha_san_xuat', 'luu_y', 'doi_tuong_cay_trong', 'phan_khuc', 'hieu_qua_chinh'],
  sources: [{id: 'n1', kind: 'image', label: 'truoc.jpg', thumb: 'data:image/jpeg;base64,AA'}, {id: 'n2', kind: 'og_image', label: 'Ảnh web', thumb: 'data:image/jpeg;base64,BB'}],
};

test('review model: rows, confidence, uncertain highlighting, sources', () => {
  const m = buildReviewModel(RESULT);
  const row = k => m.rows.find(r => r.key === k);
  assert.equal(row('ten_san_pham').uncertain, false);
  assert.equal(row('ten_san_pham').source, 'truoc.jpg');
  assert.equal(row('nha_san_xuat').empty, true);
  assert.equal(row('nha_san_xuat').uncertain, true);
  assert.equal(row('luu_y').uncertain, true);                // có giá trị nhưng tin cậy thấp
  assert.equal(row('luu_y').empty, false);
  assert.equal(row('thanh_phan_dang_ky').value, 'Đạm: 5%\nLân');
  assert.equal(row('cong_dung').value, 'Kích rễ\nXanh lá');
  assert.equal(m.providerNote, 'Đọc bằng Gemini');
  assert.equal(m.filteredPrice, true);
  assert.equal(m.images.length, 2);
});

test('editing a flagged row clears its highlight and the counter drops', () => {
  const m = buildReviewModel(RESULT);
  const before = uncertainCount(m);
  const row = m.rows.find(r => r.key === 'luu_y');
  markEdited(row, 'Tránh nắng, bảo quản nơi mát');
  assert.equal(row.uncertain, false);
  assert.equal(uncertainCount(m), before - 1);
  const empty = m.rows.find(r => r.key === 'nha_san_xuat');
  markEdited(empty, 'Cty ABC');
  assert.equal(empty.empty, false);
  assert.equal(empty.uncertain, false);
});

test('payload omits empty fields, splits lists/pairs, and carries no price keys', () => {
  const m = buildReviewModel(RESULT);
  const f = fieldsPayload(m);
  assert.deepEqual(f.cong_dung, ['Kích rễ', 'Xanh lá']);
  assert.deepEqual(f.thanh_phan_dang_ky, {'Đạm': '5%', 'Lân': ''});
  assert.ok(!('nha_san_xuat' in f));
  assert.ok(!('gia_goc_duyet' in f) && !('live_promotion' in f));
  const body = draftBody(m, {imageSourceId: 'n1', sourceUrl: 'https://x.vn'});
  assert.equal(body.session_id, 'sess1');
  assert.deepEqual(body.image, {source_id: 'n1'});
  assert.equal(body.source_url, 'https://x.vn');
  assert.ok(!('image' in draftBody(m)));
});

test('cannot save without a name; default image prefers an uploaded photo over og:image', () => {
  const m = buildReviewModel(RESULT);
  assert.equal(canSaveDraft(m), true);
  markEdited(m.rows.find(r => r.key === 'ten_san_pham'), ' ');
  assert.equal(canSaveDraft(m), false);
  assert.equal(defaultImageSource(m), 'n1');
  assert.equal(defaultImageSource(buildReviewModel({...RESULT, sources: [RESULT.sources[1]]})), 'n2');
  assert.equal(defaultImageSource(buildReviewModel({})), '');
});

test('helpers: pairs round trip, labels, Gemini warning', () => {
  assert.deepEqual(linesToPairs('Đạm: 5%\n\n Lân : 3% \nKali'), {'Đạm': '5%', 'Lân': '3%', 'Kali': ''});
  assert.equal(pairsToLines({'A': '1', 'B': ''}), 'A: 1\nB');
  assert.equal(sourceLabel(RESULT.sources, 'n2'), 'Ảnh web');
  assert.equal(sourceLabel(RESULT.sources, 'zz'), '');
  assert.match(suggestionLabel({bag_index: 7, score: 0.834, size_match: false}), /Giỏ #7 · 83% · LỆCH quy cách/);
  assert.match(needsGeminiWarning(true, {gemini_configured: false, deepseek_configured: true}), /Key Gemini/);
  assert.equal(needsGeminiWarning(false, {gemini_configured: false, deepseek_configured: true}), '');
  assert.match(needsGeminiWarning(false, {}), /Chưa có khoá AI/);
});
