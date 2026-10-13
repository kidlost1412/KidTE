// Mô hình thuần (không DOM) cho việc xoá / khôi phục hồ sơ sản phẩm.
// Xoá = chuyển vào Thùng rác 30 ngày (khôi phục được); AI ngừng dùng ngay.

/** Xoá hàng loạt (từ 10 hồ sơ, hoặc gần hết kho): cần lời cảnh báo mạnh hơn. */
export function isMassDelete(count, total) {
  return count >= 10 || (total > 0 && count >= total && count > 1) || (total >= 4 && count / total >= 0.8);
}

export function confirmTextFor(profiles, {total = 0} = {}) {
  const list = (profiles || []).filter(Boolean);
  const used = list.reduce((n, p) => n + (Number(p.mapped_skus) || 0), 0);
  const names = list.slice(0, 3).map(p => `“${p.title || p.id}”`).join(', ') + (list.length > 3 ? ` và ${list.length - 3} hồ sơ khác` : '');
  const title = list.length === 1 ? `Xoá hồ sơ ${names}?` : `Xoá ${list.length} hồ sơ sản phẩm?`;
  let text = list.length === 1 ? '' : names + '. ';
  text += 'Hồ sơ chuyển vào Thùng rác, khôi phục được trong 30 ngày. AI ngừng dùng hồ sơ này ngay.';
  if (isMassDelete(list.length, total)) text += ` CẢNH BÁO: đây là ${list.length}${total ? '/' + total : ''} hồ sơ — AI sẽ mất hồ sơ để tư vấn cho các sản phẩm này. Có thể khôi phục lại ở “Hồ sơ đã xoá”.`;
  if (used) text += ` Đang có ${used} sản phẩm gắn hồ sơ này sẽ trở thành “chưa gắn hồ sơ”.`;
  return {title, text};
}

/** Xoá lần lượt; một hồ sơ lỗi không làm hỏng các hồ sơ còn lại. */
export async function deleteProfiles(api, ids) {
  const done = [], failed = [];
  for (const id of ids || []) {
    try {
      await api(`/api/workspace/profiles/${encodeURIComponent(id)}`, {method: 'DELETE', body: {reason: 'Chủ shop xoá hồ sơ sản phẩm'}});
      done.push(id);
    } catch (error) { failed.push({id, error}); }
  }
  return {done, failed};
}

export async function restoreProfiles(api, ids) {
  const done = [], failed = [];
  for (const id of ids || []) {
    try { await api(`/api/workspace/profiles/${encodeURIComponent(id)}/restore`, {method: 'POST', body: {}}); done.push(id); }
    catch (error) { failed.push({id, error}); }
  }
  return {done, failed};
}

export async function loadDeletedProfiles(api) {
  const out = await api('/api/trash?resource=profiles&limit=100');
  return (out.items || []).filter(t => t.resource === 'profiles');
}
