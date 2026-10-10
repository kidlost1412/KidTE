import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {LIMITS, ENDPOINT, lines, slugify, toRow, toEntry, blankRow, playbookModel, validateRows, errorText, loadPlaybook, savePlaybook, SERVER_ERRORS} from '../../src/web/app/pages/sales-playbook-model.js';

const ENTRY = {id: 'ban_chiu', tinh_huong: 'Khách hỏi bán chịu', dau_hieu: ['bán chịu', 'cho nợ'], huong_xu_ly: 'Đùa duyên rồi chuyển COD.',
  du_kien_duoc_dung: ['thong_tin_shop.giao_hang'], cam: ['không hứa nợ thật', 'không đáp cụt']};

test('rows round-trip through the editor text format', () => {
  const row = toRow(ENTRY);
  assert.equal(row.dauHieu, 'bán chịu\ncho nợ');
  assert.equal(row.cam, 'không hứa nợ thật\nkhông đáp cụt');
  assert.deepEqual(toEntry(row), ENTRY);
  assert.deepEqual(toEntry({...row, dauHieu: ' a ;\n\n a\nb ', cam: ''}).dau_hieu, ['a', 'b']);
  assert.deepEqual(toRow(undefined), blankRow());
  assert.deepEqual(lines(null), []);
});

test('model counts rows and respects the entry cap', () => {
  const m = playbookModel([ENTRY]);
  assert.equal(m.count, 1);
  assert.equal(m.canAdd, true);
  assert.equal(playbookModel(Array.from({length: LIMITS.entries}, (_, i) => ({...ENTRY, id: `m${i}`}))).canAdd, false);
  assert.equal(playbookModel(undefined).rows.length, 0);
});

test('slugify makes backend-valid ids from Vietnamese titles', () => {
  assert.equal(slugify('Khách hỏi bán chịu / cho nợ'), 'khach_hoi_ban_chiu_cho_no');
  assert.equal(slugify('Đắt quá!'), 'dat_qua');
  assert.ok(slugify('x'.repeat(100)).length <= LIMITS.id);
});

test('validateRows mirrors the backend rules', () => {
  assert.deepEqual(validateRows([toRow(ENTRY)]), []);
  const bad = (patch, field) => {
    const errors = validateRows([{...toRow(ENTRY), ...patch}]);
    assert.equal(errors.length, 1, JSON.stringify(patch));
    assert.equal(errors[0].field, field);
  };
  bad({id: 'Có Dấu'}, 'id');
  bad({id: ''}, 'id');
  bad({tinhHuong: ''}, 'tinhHuong');
  bad({dauHieu: ''}, 'dauHieu');
  bad({dauHieu: 'x'.repeat(41)}, 'dauHieu');
  bad({huongXuLy: 'x'.repeat(501)}, 'huongXuLy');
  bad({duKien: Array(7).fill('a').map((x, i) => x + i).join('\n')}, 'duKien');
  bad({cam: 'y'.repeat(121)}, 'cam');
  const dup = validateRows([toRow(ENTRY), toRow(ENTRY)]);
  assert.equal(dup.length, 1);
  assert.equal(dup[0].index, 1);
  assert.match(dup[0].message, /trùng/);
  assert.equal(validateRows(Array.from({length: LIMITS.entries + 1}, (_, i) => toRow({...ENTRY, id: `m${i}`})))[0].index, -1);
});

test('load and save call the right endpoint and block invalid saves', async () => {
  const calls = [];
  const api = async (url, opts) => { calls.push([url, opts]); return {ok: true, entries: [ENTRY]}; };
  const model = await loadPlaybook(api);
  assert.equal(calls[0][0], ENDPOINT);
  assert.equal(model.rows[0].id, 'ban_chiu');
  const saved = await savePlaybook(api, model.rows);
  assert.equal(calls[1][0], '/api/workspace/sales-playbook');
  assert.equal(calls[1][1].method, 'PUT');
  assert.deepEqual(calls[1][1].body, {entries: [ENTRY]});
  assert.equal(saved.count, 1);
  await assert.rejects(() => savePlaybook(api, [{...model.rows[0], id: ''}]), e => Array.isArray(e.validation) && e.validation[0].field === 'id');
  assert.equal(calls.length, 2);   // không gọi API khi dữ liệu sai
});

test('server error codes map to friendly text', () => {
  assert.equal(errorText({code: 'playbook_id_duplicate'}), SERVER_ERRORS.playbook_id_duplicate);
  assert.match(errorText({code: 'playbook_too_large'}), /quá dài/);
  assert.match(errorText({code: 'something_else'}), /Không lưu được/);
  assert.match(errorText(null), /Không lưu được/);
});

test('system page mounts the playbook card next to shop info', async () => {
  const src = await readFile(new URL('../../src/web/app/pages/system.js', import.meta.url), 'utf8');
  assert.match(src, /import \{salesPlaybookCard\} from '\.\/sales-playbook-ui\.js'/);
  assert.ok(src.indexOf('shopInfoCard(),') < src.indexOf('salesPlaybookCard(),'));
  const ui = await readFile(new URL('../../src/web/app/pages/sales-playbook-ui.js', import.meta.url), 'utf8');
  assert.match(ui, /loadPlaybook\(request\)/);
  assert.match(ui, /savePlaybook\(request, rows\)/);
});
