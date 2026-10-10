import {h, clear, disposer, debounce} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, dateTime, number, fold, shortDuration, toDate} from '../core/format.js';
import {btn, iconBtn, chip, empty, errorBox, skeletonRows, personAvatar, pageHead, tabs, kv, bag, notice} from '../ui/kit.js';
import {toast, openDrawer, confirmDialog, promptDialog} from '../ui/overlays.js';
import {loadCatalog} from './video/data.js';

const TABS = [
  {id: 'customers', label: 'Khách hàng', icon: 'users'},
  {id: 'facts', label: 'AI nhớ gì về khách', icon: 'fileText'},
  {id: 'sessions', label: 'Phiên LIVE', icon: 'activity'},
];

async function loadAllPages(resource) {
  const first = await request(`/api/workspace/${resource}?view=summary&limit=500&page=1`);
  let items = first.items || [];
  for (let p = 2; p <= Math.min(first.pages || 1, 30); p += 1) {
    const more = await request(`/api/workspace/${resource}?view=summary&limit=500&page=${p}`);
    items = items.concat(more.items || []);
  }
  return items;
}

const eff = row => row.effective || row;
const sourcePlatform = x => x.platform === 'shopee' || String(x.customer_key || x.room_id || '').startsWith('shopee:') || x.data_source === 'shopee-live' ? 'shopee' : 'tiktok';
const sourceChip = x => chip(sourcePlatform(x) === 'shopee' ? 'Shopee' : 'TikTok', 'info');
// "Chưa phân loại"/"Khác" không nói gì về khách — không hiện thành chip.
const realTopics = list => (list || []).filter(t => t && !['Chưa phân loại', 'Khác'].includes(t));

export default {
  mount(ctx) {
    const d = disposer();
    const tab = TABS.some(t => t.id === ctx.route.sub) ? ctx.route.sub : 'customers';
    let rows = null;
    let error = null;
    let query = '';
    let platformFilter = '';
    let sort = tab === 'sessions' ? 'started' : tab === 'facts' ? 'updated' : 'last';
    let productsById = {};
    const search = h('input', {class: 'input', type: 'search', placeholder: tab === 'sessions' ? 'Tìm theo chủ đề, sản phẩm…' : 'Tìm theo tên khách, @id, chủ đề…', 'aria-label': 'Tìm'});
    const body = h('div', {class: 'card'}, skeletonRows(8, 44));
    const summary = h('span', {class: 'fs-xs muted'});
    const sortSlot = h('span');
    const sourceSelect = h('select', {class: 'select', 'aria-label': 'Nguồn khách', onChange: e => { platformFilter = e.target.value; render(); }},
      h('option', {value: ''}, 'Mọi sàn'), h('option', {value: 'shopee'}, 'Shopee'), h('option', {value: 'tiktok'}, 'TikTok'));
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Khách hàng & phiên LIVE', 'Ai đã hỏi gì, bao nhiêu lần, đã được trả lời chưa. Mọi con số được tính từ bình luận đã thu — không ước đoán.'),
      tabs(TABS, tab, id => ctx.navigate('customers', id)),
      h('div', {class: 'toolbar', style: {marginTop: '14px'}}, h('div', {class: 'search-input'}, icon('search'), search), sourceSelect, sortSlot, h('span', {class: 'grow'}), summary),
      body));
    search.addEventListener('input', debounce(() => { query = fold(search.value); render(); }, 150, d));
    loadCatalog().then(cat => { productsById = cat.byId; render(); }).catch(() => {});

    const sortOptions = {
      customers: [['last', 'Mới hoạt động'], ['comments', 'Hỏi nhiều nhất'], ['unanswered', 'Nhiều câu bị bỏ sót']],
      facts: [['updated', 'Mới cập nhật'], ['customer', 'Theo khách'], ['kind', 'Theo loại']],
      sessions: [['started', 'Mới nhất'], ['comments', 'Nhiều bình luận'], ['unanswered', 'Nhiều câu bị bỏ sót']],
    }[tab];
    clear(sortSlot, h('select', {class: 'select', style: {width: 'auto'}, 'aria-label': 'Sắp xếp', onChange: e => { sort = e.target.value; render(); }}, sortOptions.map(([v, l]) => h('option', {value: v, selected: v === sort}, l))));

    // Xoá = vào Thùng rác 30 ngày (khôi phục được). Không đụng dữ liệu gốc đã ghi.
    const RESOURCE = {customers: 'customers', facts: 'facts', sessions: 'sessions'}[tab];
    async function removeRow(row, label) {
      const ok = await confirmDialog({title: `Xoá ${label}?`, text: 'Chuyển vào Thùng rác, khôi phục được trong 30 ngày (Cài đặt → Thùng rác).', confirm: 'Xoá', tone: 'danger'});
      if (!ok) return;
      try {
        await request(`/api/workspace/${RESOURCE}/${encodeURIComponent(row.id)}`, {method: 'DELETE', body: {reason: 'Xoá trong trang Khách hàng'}});
        rows = rows.filter(r => r.id !== row.id);
        render();
        toast('Đã xoá.', 'ok', {action: 'Hoàn tác', onAction: async () => { await request(`/api/workspace/${RESOURCE}/${encodeURIComponent(row.id)}/restore`, {method: 'POST', body: {}}); load(); }});
      } catch (e) { toast(friendlyError(e), 'danger'); }
    }
    const delBtn = (row, label) => iconBtn('trash', 'Xoá', e => { if (e && e.stopPropagation) e.stopPropagation(); removeRow(row, label); }, {size: 'sm'});

    function productChips(ids) {
      return h('div', {class: 'row wrap', style: {gap: '4px'}}, (ids || []).slice(0, 6).map(id => { const p = productsById[id]; return p ? h('span', {title: p.name}, bag(p.bag_index)) : null; }));
    }

    function render() {
      if (error) { clear(body, h('div', {class: 'card-body'}, errorBox(friendlyError(error), load))); return; }
      if (!rows) return;
      let items = rows.filter(row => (!platformFilter || sourcePlatform(eff(row)) === platformFilter) && (!query || fold(JSON.stringify(eff(row))).includes(query)));
      const by = {
        last: r => -(toDate(eff(r).last_seen) || 0), comments: r => -(eff(r).comment_count || 0), unanswered: r => -(eff(r).unanswered_count || 0),
        started: r => -(toDate(eff(r).started_at) || 0), customer: r => String(eff(r).display_name || ''),
        updated: r => -(toDate(eff(r).last_seen) || 0), kind: r => String(eff(r).fact_label || ''),
      }[sort] || (() => 0);
      items = items.slice().sort((a, b) => { const x = by(a), y = by(b); return x < y ? -1 : x > y ? 1 : 0; });
      summary.textContent = `${number(items.length)} / ${number(rows.length)}`;
      if (!items.length) { clear(body, empty('Không có dữ liệu phù hợp', query ? 'Thử từ khoá khác.' : 'Khi có phiên LIVE, dữ liệu sẽ xuất hiện ở đây.', {iconName: 'users'})); return; }
      const shown = items.slice(0, 300);
      if (tab === 'customers') {
        clear(body, h('div', {class: 'table-wrap'}, h('table', {class: 'table'},
          h('thead', {}, h('tr', {}, ['Khách', 'Bình luận', 'Đã trả lời', 'Bị bỏ sót', 'Phiên', 'Hay hỏi về', 'Sản phẩm quan tâm', 'Lần cuối', ''].map(t => h('th', {}, t)))),
          h('tbody', {}, shown.map(row => {
            const x = eff(row);
            return h('tr', {style: {cursor: 'pointer'}, onClick: () => openCustomer(x)},
              h('td', {}, h('div', {class: 'row'}, personAvatar(x.display_name, x.customer_key), h('div', {}, h('strong', {class: 'fs-sm'}, x.display_name || '—'), sourceChip(x), h('div', {class: 'fs-xs muted'}, '@' + (x.unique_id || x.customer_key))))),
              h('td', {class: 'num'}, number(x.comment_count)),
              h('td', {class: 'num'}, number((x.ai_reply_count || 0) + (x.manual_reply_count || 0))),
              h('td', {}, x.unanswered_count ? chip(number(x.unanswered_count), 'danger') : h('span', {class: 'muted'}, '0')),
              h('td', {class: 'num'}, number(x.session_count)),
              h('td', {}, h('div', {class: 'row wrap', style: {gap: '4px'}}, realTopics(x.topics).slice(0, 3).map(t => chip(t)))),
              h('td', {}, productChips(x.product_ids)),
              h('td', {class: 'nowrap fs-xs', title: dateTime(x.last_seen)}, relative(x.last_seen)),
              h('td', {onClick: e => e.stopPropagation()}, delBtn(row, `khách ${x.display_name || ''}`)));
          })))), items.length > 300 ? h('div', {class: 'card-foot fs-xs muted'}, `Đang hiện 300/${items.length} — dùng ô tìm kiếm để thu hẹp.`) : null);
      } else if (tab === 'facts') {
        clear(body, h('div', {class: 'card-body fs-xs muted', style: {borderBottom: '1px solid var(--divider)'}}, 'AI tự ghi lại từ lời khách (cây trồng, diện tích, vấn đề…) để lần sau nói chuyện như khách quen. Sửa khi AI nhớ sai; xoá thì AI quên luôn.'),
          h('div', {class: 'table-wrap'}, h('table', {class: 'table'},
          h('thead', {}, h('tr', {}, ['Khách', 'Loại', 'AI nhớ', 'Khách đã nói', 'Cập nhật', ''].map(t => h('th', {}, t)))),
          h('tbody', {}, shown.map(row => {
            const x = eff(row);
            return h('tr', {},
              h('td', {}, h('div', {class: 'row'}, personAvatar(x.display_name, x.customer_key, 28), h('span', {class: 'fs-sm'}, x.display_name || x.customer_key))),
              h('td', {class: 'fs-sm'}, sourceChip(x), chip(x.fact_label || x.fact_key, 'info')), h('td', {class: 'fs-sm fw-7'}, x.fact_value),
              h('td', {class: 'fs-xs muted', style: {maxWidth: '360px'}}, x.source_comment ? `“${x.source_comment}”` : '—'),
              h('td', {class: 'fs-xs nowrap'}, relative(x.last_seen)),
              h('td', {class: 'nowrap'}, btn('Sửa', {size: 'sm', kind: 'ghost', icon: 'edit', onClick: () => editFact(row)}), delBtn(row, 'thông tin này (AI sẽ quên)')));
          })))));
      } else {
        clear(body, h('div', {class: 'table-wrap'}, h('table', {class: 'table'},
          h('thead', {}, h('tr', {}, ['Phiên', 'Thời lượng', 'Bình luận', 'Khách', 'AI trả lời', 'Shop trả lời', 'Bị bỏ sót', 'Chủ đề nổi bật', 'Sản phẩm', ''].map(t => h('th', {}, t)))),
          h('tbody', {}, shown.map(row => {
            const x = eff(row);
            const dur = (toDate(x.ended_at) || 0) - (toDate(x.started_at) || 0);
            return h('tr', {},
              h('td', {}, sourceChip(x), h('strong', {class: 'fs-sm'}, dateTime(x.started_at)), h('div', {class: 'fs-xs muted'}, x.status === 'active' ? 'Đang LIVE' : 'Đã kết thúc')),
              h('td', {class: 'fs-sm'}, dur > 0 ? shortDuration(dur) : '—'),
              h('td', {class: 'num'}, number(x.comment_count)), h('td', {class: 'num'}, number(x.customer_count)),
              h('td', {class: 'num'}, number(x.ai_reply_count)), h('td', {class: 'num'}, number(x.manual_reply_count)),
              h('td', {}, x.unanswered_count ? chip(number(x.unanswered_count), 'danger') : h('span', {class: 'muted'}, '0')),
              h('td', {}, h('div', {class: 'row wrap', style: {gap: '4px'}}, realTopics(x.top_topics).slice(0, 3).map(t => chip(t)))),
              h('td', {}, productChips(x.product_ids)),
              h('td', {class: 'nowrap'}, btn('Xem bình luận', {size: 'sm', icon: 'message', onClick: () => ctx.navigate('inbox', 'session', x.session_id || row.id)}), delBtn(row, 'phiên LIVE này')));
          })))));
      }
    }

    async function editFact(row) {
      const x = eff(row);
      const value = await promptDialog({title: `Sửa: ${x.fact_label || x.fact_key} của ${x.display_name}`, label: 'AI sẽ nhớ', value: x.fact_value, confirm: 'Lưu'});
      if (value === null || !value.trim() || value.trim() === x.fact_value) return;
      try {
        await request(`/api/workspace/facts/${encodeURIComponent(row.id)}`, {method: 'PATCH', body: {changes: {fact_value: value.trim()}, reason: 'Chủ shop sửa thông tin khách'}});
        toast('Đã sửa — AI dùng thông tin mới từ câu trả lời kế tiếp.', 'ok');
        load();
      } catch (e) { toast(friendlyError(e), 'danger'); }
    }

    function historyBlock(x) {
      const slot = h('div', {class: 'col gap-2'}, h('div', {class: 'fs-xs muted'}, 'Đang tải…'));
      request('/api/system/customer-history?key=' + encodeURIComponent(x.customer_key)).then(out => {
        const hist = out.history || {};
        if (!hist.phien_truoc || !hist.phien_truoc.length) { clear(slot, h('div', {class: 'fs-sm muted'}, 'Chưa có bình luận ở phiên LIVE nào (hoặc khách chỉ có tên hiển thị, không có ID tài khoản để khớp chắc chắn).')); return; }
        clear(slot,
          h('div', {class: 'fs-xs muted'}, `Định danh: ${hist.dinh_danh} · ${hist.so_phien_da_binh_luan} phiên · từ ${hist.lan_dau}`),
          (hist.luu_y || []).map(n => notice('info', null, n)),
          hist.phien_truoc.map(sess => h('div', {class: 'backup-part'},
            h('strong', {class: 'fs-sm'}, `Phiên ${sess.ngay} · ${sess.gio} · ${sess.so_binh_luan} bình luận`),
            sess.binh_luan.map(c => h('div', {class: 'col', style: {gap: '2px'}},
              h('div', {class: 'fs-sm'}, h('span', {class: 'muted'}, c.luc + ' '), c.khach, c.dang_ghim ? h('span', {class: 'fs-xs muted'}, ` (đang ghim ${c.dang_ghim})`) : null),
              h('div', {class: 'fs-xs', style: {color: c.tra_loi === 'chưa ai trả lời' ? 'var(--warn-text)' : 'var(--text-3)'}}, '↳ ' + c.tra_loi))))));
      }).catch(e => clear(slot, h('div', {class: 'fs-sm muted'}, friendlyError(e))));
      return h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'AI thấy lịch sử này khi khách bình luận lần sau'), slot);
    }

    function openCustomer(x) {
      openDrawer({
        title: x.display_name || x.unique_id || 'Khách hàng', sub: '@' + (x.unique_id || x.customer_key),
        body: h('div', {class: 'card-body col gap-4'},
          h('div', {class: 'row gap-3'}, personAvatar(x.display_name, x.customer_key, 56), h('div', {}, h('div', {class: 'fs-lg fw-7'}, x.display_name), h('div', {class: 'fs-xs muted'}, `Hoạt động lần cuối ${relative(x.last_seen)}`))),
          kv([['Tổng bình luận', number(x.comment_count)], ['AI đã trả lời', number(x.ai_reply_count)], ['Chủ shop trả lời', number(x.manual_reply_count)], ['Bị bỏ sót', number(x.unanswered_count)], ['Số phiên LIVE', number(x.session_count)]]),
          h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Hay hỏi về'), h('div', {class: 'row wrap'}, realTopics(x.topics).map(t => chip(t, 'info')))),
          h('div', {class: 'col'}, h('div', {class: 'section-title'}, 'Sản phẩm đã quan tâm'), h('div', {class: 'col', style: {gap: '6px'}}, (x.product_ids || []).map(id => { const p = productsById[id]; return p ? h('div', {class: 'row'}, bag(p.bag_index), h('span', {class: 'fs-sm'}, p.name)) : null; }))),
          historyBlock(x),
          btn('Xem mọi bình luận của khách này', {kind: 'primary', icon: 'message', onClick: () => ctx.navigate('inbox', 'q', x.unique_id || x.display_name)})),
      });
    }

    async function load() {
      error = null;
      try { rows = await loadAllPages(tab); } catch (e) { error = e; }
      render();
    }
    load();
    return {unmount: () => d.dispose()};
  },
};
