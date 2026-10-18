/** Backup v2 helpers for the backups tab. No imports: dependencies are injected. */
export const RECOVERY_DATABASE = 'kid-te-studio-recovery';

export function kindLabel(kind) {
  return kind === 'full' ? 'Đầy đủ' : 'Một phần';
}

export function kindNote(kind) {
  return kind === 'full' ? 'Gồm toàn bộ nguồn đã kiểm' : 'Còn thiếu một số nguồn, không tính là bản đầy đủ';
}

export function normalizeV2(items) {
  return (Array.isArray(items) ? items : []).filter(i => i && typeof i.id === 'string')
    .map(i => ({id: i.id, kind: i.kind === 'full' ? 'full' : 'partial', created_at: i.created_at || '',
      size: Number(i.size) || 0, verified: i.verified === true}));
}

export function passwordProblem(value) {
  return typeof value === 'string' && value.length >= 8 ? '' : 'Mật khẩu cần ít nhất 8 ký tự.';
}

/** Reads every store of the web app's own IndexedDB recovery database. */
export function readIndexedDb(idb, name = RECOVERY_DATABASE) {
  return new Promise((resolve, reject) => {
    const open = idb.open(name);
    open.onerror = () => reject(open.error || new Error('indexeddb_open_failed'));
    open.onsuccess = () => {
      const db = open.result;
      const names = Array.from(db.objectStoreNames);
      const stores = {};
      if (!names.length) { db.close(); resolve({database: name, version: db.version, stores}); return; }
      const tx = db.transaction(names, 'readonly');
      names.forEach(n => {
        const all = tx.objectStore(n).getAll();
        all.onsuccess = () => { stores[n] = all.result; };
      });
      tx.oncomplete = () => { db.close(); resolve({database: name, version: db.version, stores}); };
      tx.onerror = tx.onabort = () => { db.close(); reject(tx.error || new Error('indexeddb_read_failed')); };
    };
  });
}

export async function exportIndexedDb({idb, request, signal}) {
  const data = await readIndexedDb(idb);
  return request('/api/backup/indexeddb', {method: 'POST', body: {export: data}, signal});
}

export async function startV2({request, waitJob, password, full, signal, onStart}) {
  const started = await request('/api/backup/v2', {method: 'POST', body: {full: !!full, password}, signal});
  if (onStart) onStart(started.job_id);
  return waitJob(started.job_id, {signal});
}

/** Selection: ids = chosen ids, or every id when all=true. */
export function selectionIds(items, selected, all = false) {
  const known = new Set((items || []).map(i => i.id));
  return all ? [...known] : [...selected].filter(id => known.has(id));
}

/** Preview, ask for confirmation (with reclaim size), then delete. Returns null if cancelled. */
export async function deleteV2({request, confirm, ids, signal}) {
  if (!ids.length) return null;
  const checked = await request('/api/backup/v2/delete/preview', {method: 'POST', body: {ids}, signal});
  if (!await confirm(checked)) return null;
  return request('/api/backup/v2/delete', {method: 'POST', body: {ids: checked.ids, token: checked.token}, signal});
}

/* ---- Restore v2 ---------------------------------------------------------- */

/** The dashboard/status may report restore_v2 explicitly; absent means unknown, so keep the action visible. */
export function restoreV2Visible(status) {
  const s = status || {};
  const flag = s.restore_v2_enabled !== undefined ? s.restore_v2_enabled : s.restore_v2;
  return flag !== false;
}

/** Strong confirmation: the operator must type the exact backup id. */
export function confirmProblem(typed, backupId) {
  return typeof typed === 'string' && typed.trim() === backupId ? '' : 'Gõ đúng mã bản sao lưu để xác nhận: ' + backupId;
}

const RESTORE_ERRORS = {
  restore_v2_disabled: 'Tính năng khôi phục chưa được bật trên máy này. Hãy bấm “Tạo báo cáo hỗ trợ” để nhờ bật giúp.',
  maintenance_required: 'Còn phiên LIVE hoạt động. Dừng phiên LIVE trước khi khôi phục.',
  maintenance_job_busy: 'Máy đang bảo trì hoặc có tác vụ khác đang chạy. Chờ xong rồi thử lại.',
  backup_password_required: 'Cần nhập mật khẩu của bản sao lưu.',
};

export function restoreErrorText(err) {
  const code = String((err && (err.code || err.message)) || '');
  if (RESTORE_ERRORS[code]) return RESTORE_ERRORS[code];
  if (code.startsWith('writers_close_failed')) return 'Không đóng được bộ ghi dữ liệu nên chưa khôi phục. Dữ liệu hiện tại vẫn nguyên vẹn.';
  if (code.startsWith('restore_failed') || code === 'restore_verify_failed' || code === 'restore_stage_invalid') {
    return 'Khôi phục thất bại và đã quay về dữ liệu cũ (' + code + '). Dữ liệu hiện tại không bị mất.';
  }
  return code ? 'Khôi phục không thành công: ' + code : 'Khôi phục không thành công.';
}

/** Runs the synchronous maintenance restore. onPhase gets coarse progress text. */
export async function restoreV2({request, backupId, password, typed, signal, onPhase}) {
  if (confirmProblem(typed, backupId)) throw Object.assign(new Error('confirmation_mismatch'), {code: 'confirmation_mismatch'});
  if (passwordProblem(password)) throw Object.assign(new Error('backup_password_required'), {code: 'backup_password_required'});
  if (onPhase) onPhase('Đang bảo trì: đóng bộ ghi, dựng bản tạm, kiểm tra và hoán đổi dữ liệu…');
  const out = await request('/api/backup/restore', {method: 'POST', body: {v2: true, backup_id: backupId, password}, signal});
  if (onPhase) onPhase('Hoàn tất khôi phục.');
  return {restored: out.restored || backupId, aiEnabled: out.ai_enabled === true,
    autoPinEnabled: out.auto_pin_enabled === true, rollbackDir: out.rollback_dir || ''};
}

export function reenableAi({request, signal}) {
  return request('/api/backup/restore/reenable-ai', {method: 'POST', body: {}, signal});
}
