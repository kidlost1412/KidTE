import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source = await readFile(new URL('../../src/web/app/core/update_notice.js', import.meta.url), 'utf8');
const {updateNotice, upToDateLine, formatClock, newerPackages, headerLines, updateComponents, allUpdateComponents, progressView, formatDate, formatCheckedAt, LIVE_RESTART_TEXT} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('fresh machine batch installs missing features with app updates in one selection', () => {
  const state = {installed: {app: '1.15.7', runtime: '1.0.0', extensions: '1.0.1'}, available: [
    {id: 'app', version: '1.15.8', installed_version: '1.15.7', newer: true},
    {id: 'runtime', version: '1.0.0', installed_version: '1.0.0'},
    {id: 'extensions', version: '1.0.1', installed_version: '1.0.1'},
    ...['media-tools', 'captcha-solver', 'video-sync', 'video-analysis-cpu', 'gpu-cuda12'].map(id => ({id, version: '1.0.0'})),
    {id: 'unrecognized', version: '1.0.0', newer: true},
  ]};
  const expected = {app: '1.15.8', 'media-tools': '1.0.0', 'captcha-solver': '1.0.0', 'video-sync': '1.0.0', 'video-analysis-cpu': '1.0.0'};
  assert.deepEqual(allUpdateComponents(state), expected);
  assert.deepEqual(allUpdateComponents(state, {includeGpu: true}), {...expected, 'gpu-cuda12': '1.0.0'});
  assert.deepEqual(allUpdateComponents({...state, setup_hardware: {eligible: true}}), {...expected, 'gpu-cuda12': '1.0.0'});
  assert.deepEqual(allUpdateComponents(null), {});
});

test('batch leaves unchanged or newer installed versions alone and updates installed NVIDIA', () => {
  const state = {installed: {'media-tools': '3.0.0'}, available: [
    {id: 'media-tools', version: '2.0.0', newer: false},
    {id: 'video-sync', version: '1.0.0', installed_version: '1.0.0', newer: false},
    {id: 'gpu-cuda12', version: '1.0.1', installed_version: '1.0.0', newer: true},
  ]};
  assert.deepEqual(allUpdateComponents(state), {'gpu-cuda12': '1.0.1'});
});

test('fresh-machine setup shows progress and restart notice even when app is latest', () => {
  assert.match(updateNotice({initial_setup:true, busy:true, update_available:false}).text, /thiết lập máy mới/);
  const n = updateNotice({restart_required:true, update_available:false, pending:{'media-tools':'1.0.0'}});
  assert.equal(n.visible, true);
  assert.match(n.text, /Khởi động lại/);
  assert.equal(updateNotice({restart_required:true, pending:{'media-tools':'1.0.0'}}, n.version).visible, false);
});

const newer = {update_available: true, update_total_bytes: 48_200_000, restart_required: false,
  available: [
    {id: 'core', version: '1.14.7', download_bytes: 48_200_000, newer: true, installed_version: '1.14.6'},
    {id: 'media-tools', version: '2.1.0', download_bytes: 5_000_000, newer: false, installed_version: '2.1.0'},
  ]};

test('banner shows core version and rounded size when a newer package exists', () => {
  const n = updateNotice(newer);
  assert.equal(n.visible, true);
  assert.equal(n.badge, true);
  assert.equal(n.version, '1.14.7');
  assert.equal(n.text, 'Có bản mới 1.14.7 (~48 MB) — Cập nhật ngay');
});

test('dismissing the banner hides it for that version only, badge stays', () => {
  const n = updateNotice(newer, '1.14.7');
  assert.equal(n.visible, false);
  assert.equal(n.badge, true);
  assert.equal(updateNotice(newer, '1.14.6').visible, true);
});

test('restart pending swaps banner text to the restart hint', () => {
  const n = updateNotice({...newer, restart_required: true});
  assert.equal(n.text, 'Đã tải bản 1.14.7 · Khởi động lại để hoàn tất');
  assert.equal(n.badge, true);
});

test('no banner and no badge when nothing newer or state missing', () => {
  assert.deepEqual(updateNotice({...newer, update_available: false}).badge, false);
  assert.equal(updateNotice({update_available: false, available: []}).visible, false);
  assert.equal(updateNotice(null).visible, false);
  assert.equal(updateNotice(undefined).badge, false);
});

test('uncommon case: uninstalled feature is never flagged as newer', () => {
  const state = {update_available: false, available: [{id: 'gpu-cuda12', version: '1.0.0', newer: false}]};
  assert.deepEqual(newerPackages(state), []);
  assert.equal(updateNotice(state).visible, false);
});

test('size falls back to package download bytes when total is absent', () => {
  const n = updateNotice({update_available: true, available: [{id: 'core', version: '2.0.0', download_bytes: 300_000, newer: true}]});
  assert.equal(n.text, 'Có bản mới 2.0.0 (~1 MB) — Cập nhật ngay');
});

test('up-to-date line shows version and clock of last check', () => {
  const iso = new Date(2026, 9, 12, 8, 5, 0).toISOString();
  const line = upToDateLine({update_available: false, installed: {core: '1.14.6'}, last_checked_at: iso});
  assert.equal(line, `Bạn đang dùng bản mới nhất (1.14.6) · kiểm tra lúc ${formatClock(iso)}`);
  assert.match(line, /kiểm tra lúc \d{2}:\d{2}$/);
  assert.equal(formatClock(new Date(2026, 9, 12, 8, 5).toISOString()), '08:05');
});

test('up-to-date line is empty while an update exists and mentions no check when never run', () => {
  assert.equal(upToDateLine({update_available: true, installed: {core: '1.14.6'}}), '');
  assert.equal(upToDateLine({update_available: false, installed: {}, last_checked_at: null}), 'Chưa kiểm tra bản mới trong phiên này.');
  assert.equal(formatClock('not-a-date'), '');
});

test('extension reload notice shows the chrome://extensions instruction', async () => {
  const mod = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  assert.equal(mod.extensionNotice({}).visible, false);
  const n = mod.extensionNotice({extension_reload: {version: '1.0.1'}});
  assert.equal(n.visible, true);
  assert.match(n.text, /chrome:\/\/extensions/);
  assert.match(n.text, /Tải lại/);
});

test('header lines: using, newest with date, up-to-date tick', () => {
  const h = headerLines({enabled: true, current_version: '1.15.0', latest: {version: '1.15.1', date: '2026-10-12T08:20:48Z'}, update_available: true});
  assert.equal(h.using, 'Đang dùng: Kid TE 1.15.0');
  assert.match(h.newest, /^Mới nhất trên GitHub: 1\.15\.1 \(\d\d\/10\/2026\)$/);
  assert.equal(h.upToDate, false);
  const same = headerLines({enabled: true, current_version: '1.15.0', latest: {version: '1.15.0', date: ''}, update_available: false});
  assert.equal(same.upToDate, true);
  assert.equal(same.upToDateText, 'Bạn đang dùng bản mới nhất ✓');
  assert.equal(formatDate('bad'), '');
});

test('one-click update covers every newer package (required + installed optional)', () => {
  const state = {update_available: true, available: [
    {id: 'runtime', version: '1.0.0', newer: true}, {id: 'app', version: '1.15.0', newer: true},
    {id: 'extensions', version: '1.0.0', newer: true}, {id: 'gpu-cuda12', version: '1.0.1', newer: true},
    {id: 'media-tools', version: '1.0.0', newer: false}]};
  assert.deepEqual(updateComponents(state), {runtime: '1.0.0', app: '1.15.0', extensions: '1.0.0', 'gpu-cuda12': '1.0.1'});
});

test('progress view uses overall bytes and clamps percent', () => {
  const v = progressView({progress: {component: 'app', received: 10, total: 100, overall_received: 25_000_000, overall_total: 50_000_000, index: 2, count: 3}});
  assert.equal(v.percent, 50);
  assert.equal(v.text, '50% · 25.0 / 50.0 MB');
  assert.equal(v.index, 2);
  assert.equal(progressView({progress: null}), null);
});

test('live restart confirm text is the owner-approved sentence', () => {
  assert.match(LIVE_RESTART_TEXT, /^Đang LIVE — app sẽ tắt khoảng 20–30 giây rồi tự mở lại/);
});

test('formatCheckedAt shows Vietnam local time hh:mm dd/mm', () => {
  assert.equal(formatCheckedAt('2026-10-12T13:32:13+00:00'), '20:32 12/10');
  assert.equal(formatCheckedAt('2026-12-31T18:00:00Z'), '01:00 01/01');
  assert.equal(formatCheckedAt(''), '');
});
