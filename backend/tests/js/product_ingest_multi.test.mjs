import test from 'node:test';
import assert from 'node:assert/strict';
import {buildProductsModels, productName, pickedForSave, saveLabel, defaultProvider, providerHint, imagesAllowed, oversizeMessage, markEdited, draftBody} from '../../src/web/app/pages/product-ingest-model.js';
import {ApiError, friendlyError, request} from '../../src/web/app/core/api.js';
import {confirmTextFor, isMassDelete} from '../../src/web/app/pages/profile-delete-model.js';

const cell = (name, extra = {}) => ({fields: {ten_san_pham: name, cong_dung: ['Kích rễ'], ...extra}, confidence: {ten_san_pham: 0.9, cong_dung: 0.9}, nguon: {ten_san_pham: 'n1'}, uncertain: [], filtered_price: false});
const RESULT = {provider: 'gemini', session_id: 's1', sources: [{id: 'n1', kind: 'file', label: 'DANH MỤC SP CÂY MAI VÀNG.pdf'}],
  products: [cell('NANO LÂN'), cell('NANO KALI'), cell('ĐẠM CÁ')]};

test('catalog result becomes one review model per product, all pre-selected', () => {
  const entries = buildProductsModels(RESULT);
  assert.equal(entries.length, 3);
  assert.deepEqual(entries.map(productName), ['NANO LÂN', 'NANO KALI', 'ĐẠM CÁ']);
  assert.equal(entries[0].model.providerNote, 'Đọc bằng Gemini');
  assert.equal(entries[1].model.sessionId, 's1');
  assert.equal(entries[2].model.rows.find(r => r.key === 'ten_san_pham').source, 'DANH MỤC SP CÂY MAI VÀNG.pdf');
  assert.equal(saveLabel(entries), 'Lưu nháp 3 sản phẩm');
});

test('owner picks which products become drafts; unnamed or unticked ones are skipped', () => {
  const entries = buildProductsModels(RESULT);
  entries[1].picked = false;
  assert.deepEqual(pickedForSave(entries).map(productName), ['NANO LÂN', 'ĐẠM CÁ']);
  markEdited(entries[2].model.rows.find(r => r.key === 'ten_san_pham'), '');
  assert.deepEqual(pickedForSave(entries).map(productName), ['NANO LÂN']);
  assert.equal(saveLabel(entries), 'Lưu nháp');
  const body = draftBody(entries[0].model, {});
  assert.equal(body.fields.ten_san_pham, 'NANO LÂN');
  assert.equal(body.session_id, 's1');
});

test('legacy single-product result (no products array) still works', () => {
  const entries = buildProductsModels({...cell('Đạm Cá 5L'), provider: 'deepseek', session_id: 's2', sources: []});
  assert.equal(entries.length, 1);
  assert.equal(productName(entries[0]), 'Đạm Cá 5L');
  assert.equal(entries[0].model.providerNote, 'Đọc bằng DeepSeek (chỉ chữ)');
});

test('provider select: Gemini by default when a key exists, DeepSeek otherwise; images only with Gemini', () => {
  assert.equal(defaultProvider({gemini_configured: true, deepseek_configured: true}), 'gemini');
  assert.equal(defaultProvider({gemini_configured: false, deepseek_configured: true}), 'deepseek');
  assert.equal(defaultProvider({}), 'gemini');
  assert.equal(imagesAllowed('gemini'), true);
  assert.equal(imagesAllowed('deepseek'), false);
  assert.match(providerHint('deepseek', {deepseek_configured: true}), /ảnh nhãn và PDF scan bị tắt/);
  assert.match(providerHint('deepseek', {}), /Chưa có khoá DeepSeek/);
  assert.match(providerHint('gemini', {}), /Chưa có khoá Gemini/);
  assert.equal(providerHint('gemini', {gemini_configured: true}), '');
});

test('client refuses a file over the server limit with a clear message', () => {
  assert.equal(oversizeMessage([{name: 'a.pdf', size: 58 * 1048576}]), '');
  assert.match(oversizeMessage([{name: 'DANH MỤC.pdf', size: 150 * 1048576}]), /DANH MỤC\.pdf.*100 MB/);
});

test('server 4xx reason is shown as-is, not as a connection failure', () => {
  const reason = 'Tệp “a.pdf” rỗng hoặc quá lớn (tối đa 100 MB)';
  assert.equal(friendlyError(new ApiError(reason, '', 400)), reason);
  assert.equal(friendlyError(new ApiError('Dữ liệu gửi lên quá lớn.', '', 413)), 'Dữ liệu gửi lên quá lớn.');
  assert.equal(friendlyError(new ApiError('Ingest failed badly here', '', 422)), 'Ingest failed badly here');
  assert.match(friendlyError(new ApiError('invalid_body_size', '', 400)), /quá lớn/);
  assert.match(friendlyError(new ApiError('network_error')), /Mất kết nối/);
});

test('a 400 JSON body from the server surfaces its error text through request()', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ok: false, status: 400, json: async () => ({ok: false, error: 'Tệp “x.pdf” quá lớn (tối đa 100 MB)'})});
  try {
    await assert.rejects(request('/api/ingest/extract', {method: 'POST', body: {}}), err => {
      assert.equal(err.status, 400);
      assert.equal(friendlyError(err), 'Tệp “x.pdf” quá lớn (tối đa 100 MB)');
      return true;
    });
  } finally { globalThis.fetch = realFetch; }
});

test('mass delete gets a strong warning (all profiles gone = empty mapping dropdown)', () => {
  assert.equal(isMassDelete(1, 47), false);
  assert.equal(isMassDelete(9, 47), false);
  assert.equal(isMassDelete(10, 47), true);
  assert.equal(isMassDelete(2, 2), true);
  const list = Array.from({length: 47}, (_, i) => ({id: 'P' + i, title: 'SP ' + i}));
  const {text} = confirmTextFor(list, {total: 47});
  assert.match(text, /CẢNH BÁO.*47\/47/);
  assert.match(text, /Hồ sơ đã xoá/);
  assert.doesNotMatch(confirmTextFor(list.slice(0, 3), {total: 47}).text, /CẢNH BÁO/);
});
