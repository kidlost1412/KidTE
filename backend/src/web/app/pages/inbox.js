import {h, clear, disposer, debounce} from '../core/dom.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, dateTime, clock, fold, number} from '../core/format.js';
import {btn, chip, empty, errorBox, skeletonRows, personAvatar, productAvatar, field, textarea, select, segmented, notice, kv, bag} from '../ui/kit.js';
import {toast, confirmDialog} from '../ui/overlays.js';
import {loadCatalog} from './video/data.js';
import {lessonCard, watchRun, tryResult, tryAI} from './learning-ui.js';
import {CONVERSATION_STATUS, DELIVERY, RESPONSE_QUALITY, OPERATOR_STATUS_CHOICES, lookup} from '../labels.js';

const QUESTION_TYPES = ['Chưa phân loại', 'Giá và khuyến mãi', 'Liều lượng', 'Pha chung', 'Cách sử dụng', 'Tồn kho và quy cách', 'Đặt hàng', 'Chọn sản phẩm', 'Phản hồi sau mua', 'Chào hỏi', 'Không cần trả lời'];

// Đã dạy AI hoặc đã đánh giá = đã xử lý: ra khỏi các hàng chờ, còn thấy ở "Đã dạy AI" / "Tất cả".
const handled = x => Boolean(x.taught) || (x.response_quality && x.response_quality !== 'unreviewed');
const isAI = s => ['ai_sent', 'ai_unconfirmed'].includes(s);
const FILTERS = [
  {id: 'todo', label: 'Cần xử lý', icon: 'alert', match: x => ['unanswered', 'waiting', 'ai_failed'].includes(x.conversation_status) && !handled(x), alert: true},
  {id: 'review', label: 'Duyệt câu AI', icon: 'sparkles', match: x => isAI(x.conversation_status) && !handled(x), hint: 'Câu AI đã gửi mà bạn chưa xem — giữ nguyên = đúng, sửa = dạy AI'},
  {id: 'unanswered', label: 'Bị bỏ sót', icon: 'xCircle', match: x => x.conversation_status === 'unanswered' && !handled(x), alert: true},
  {id: 'failed', label: 'AI gửi lỗi', icon: 'alert', match: x => x.conversation_status === 'ai_failed' && !handled(x), alert: true},
  {id: 'waiting', label: 'Đang chờ', icon: 'clock', match: x => x.conversation_status === 'waiting'},
  {id: 'taught', label: 'Đã dạy AI', icon: 'checkCircle', match: x => Boolean(x.taught)},
  {id: 'replied', label: 'Đã trả lời', icon: 'message', match: x => ['ai_sent', 'ai_unconfirmed', 'manual_replied', 'answered'].includes(x.conversation_status)},
  {id: 'ai', label: '· bởi AI', icon: 'bot', match: x => isAI(x.conversation_status)},
  {id: 'manual', label: '· bởi chủ shop', icon: 'user', match: x => x.conversation_status === 'manual_replied'},
  {id: 'skipped', label: 'Bỏ qua an toàn', icon: 'shield', match: x => x.conversation_status === 'skipped_safely' || x.conversation_status === 'skipped'},
  {id: 'ignored', label: 'Không cần trả lời', icon: 'eyeOff', match: x => x.conversation_status === 'ignored'},
  {id: 'all', label: 'Tất cả', icon: 'inbox', match: () => true},
];

const eff = row => row.effective || row;
const platformOf = x => x.platform === 'shopee' || x.data_source === 'shopee-live' || String(x.session_id || '').startsWith('shopee:') ? 'shopee' : 'tiktok';

export default {
  mount(ctx) {
    const d = disposer();
    let rows = null;
    let loadError = null;
    let filter = FILTERS.some(f => f.id === ctx.route.sub) ? ctx.route.sub : 'todo';
    let selectedId = ctx.route.id || '';
    let query = '';
    let sessionFilter = '';
    let platformFilter = '';
    let productsById = {};
    const checked = new Set();
    let detailToken = 0;
    let lastOrder = [];
    let lessonHint = null;              // {id, runId}: bài học vừa tạo, hiện ngay sau khi làm mới
    const detailDisposers = [];         // theo dõi lượt học của hội thoại đang mở
    d.add(() => detailDisposers.splice(0).forEach(fn => fn()));
    // Liên kết từ trang khác: #inbox/q/<từ khoá> · #inbox/session/<mã phiên>
    let initialQuery = '';
    if (ctx.route.sub === 'q') { filter = 'all'; initialQuery = ctx.route.id || ''; selectedId = ''; }
    if (ctx.route.sub === 'session') { filter = 'all'; sessionFilter = ctx.route.id || ''; selectedId = ''; }
    if (ctx.route.sub === 'platform' && ['tiktok','shopee'].includes(ctx.route.id)) { filter = 'all'; platformFilter = ctx.route.id; selectedId = ''; }

    const rail = h('nav', {class: 'inbox-rail', 'aria-label': 'Lọc bình luận'});
    const searchInput = h('input', {class: 'input', type: 'search', placeholder: 'Tìm theo tên khách, nội dung, câu trả lời…', 'aria-label': 'Tìm bình luận'});
    const listHead = h('div', {class: 'inbox-tools'}, h('div', {class: 'search-input grow'}, icon('search'), searchInput));
    listHead.append(h('select', {class: 'select', value: platformFilter, 'aria-label': 'Sàn của bình luận', onChange: e => {platformFilter = e.target.value; renderList();}},
      h('option', {value: '', selected: !platformFilter}, 'Cả hai sàn'), h('option', {value: 'tiktok', selected: platformFilter === 'tiktok'}, 'TikTok'), h('option', {value: 'shopee', selected: platformFilter === 'shopee'}, 'Shopee')));
    const listMeta = h('div', {class: 'row between wrap fs-xs muted', style: {padding: '6px 14px', borderBottom: '1px solid var(--divider)'}});
    const listBody = h('div', {class: 'inbox-rows'}, skeletonRows(8, 58));
    const list = h('section', {class: 'inbox-list', 'aria-label': 'Danh sách bình luận'}, listHead, listMeta, listBody);
    const detail = h('section', {class: 'inbox-detail', 'aria-label': 'Chi tiết'});
    const batchSlot = h('div');
    const root = h('div', {class: 'inbox'}, rail, list, detail, batchSlot);
    clear(ctx.shell.main, root);
    ctx.shell.setFullBleed(false);
    ctx.shell.main.classList.add('full');
    d.add(() => ctx.shell.main.classList.remove('full'));

    searchInput.addEventListener('input', debounce(() => { query = fold(searchInput.value); renderList(); }, 150, d));
    if (initialQuery) { searchInput.value = initialQuery; query = fold(initialQuery); }

    // -------------------------------------------------------- dữ liệu
    async function loadAll() {
      loadError = null;
      try {
        const first = await request('/api/workspace/conversations?view=summary&limit=500&page=1');
        let items = first.items || [];
        for (let p = 2; p <= (first.pages || 1); p += 1) {
          const more = await request(`/api/workspace/conversations?view=summary&limit=500&page=${p}`);
          items = items.concat(more.items || []);
        }
        rows = items;
      } catch (e) { loadError = e; }
      renderRail(); renderList();
      if (selectedId) showDetail(selectedId);
      else renderDetailEmpty();
    }
    loadCatalog().then(cat => { productsById = cat.byId; renderList(); }).catch(() => {});

    function filtered() {
      if (!rows) return [];
      const f = FILTERS.find(x => x.id === filter) || FILTERS[0];
      return rows.filter(row => {
        const x = eff(row);
        if (!f.match(x)) return false;
        if (platformFilter && platformOf(x) !== platformFilter) return false;
        if (sessionFilter && x.session_id !== sessionFilter) return false;
        if (query && !fold(`${x.nickname} ${x.unique_id} ${x.original_text} ${x.reply_text} ${x.corrected_text} ${x.selected_product_name}`).includes(query)) return false;
        return true;
      });
    }

    function renderRail() {
      const counts = {};
      FILTERS.forEach(f => { counts[f.id] = rows ? rows.filter(r => f.match(eff(r))).length : null; });
      clear(rail,
        h('div', {class: 'rail-title'}, 'Hộp thư'),
        FILTERS.map(f => h('button', {class: 'rail-item', type: 'button', title: f.hint || '', 'aria-current': String(f.id === filter), style: f.label.startsWith('·') ? {paddingLeft: '28px'} : null,
          onClick: () => { filter = f.id; checked.clear(); ctx.replaceSilently('inbox', filter, selectedId); renderRail(); renderList(); }},
          icon(f.icon), h('span', {class: 'grow truncate'}, f.label.replace(/^· /, '')),
          counts[f.id] !== null ? h('span', {class: 'rail-count' + (f.alert && counts[f.id] ? ' alert' : '')}, number(counts[f.id])) : null)),
        h('div', {class: 'rail-title'}, 'Mẹo'),
        h('p', {class: 'fs-xs muted', style: {padding: '0 10px'}}, 'Sửa câu trả lời rồi bấm “Dạy AI” (Ctrl+Enter). AI rút ra điều cần nhớ và dùng cho MỌI câu hỏi liên quan — không chỉ chép lại câu này.'));
    }

    function statusChip(x) { const st = lookup(CONVERSATION_STATUS, x.conversation_status); return chip(st.short || st.label, st.tone); }

    function renderList() {
      if (loadError) { clear(listBody, h('div', {style: {padding: '16px'}}, errorBox(friendlyError(loadError), loadAll))); return; }
      if (!rows) return;
      const items = filtered();
      clear(listMeta, h('span', {}, `${number(items.length)} bình luận`, sessionFilter ? h('span', {}, ' · trong 1 phiên ', h('button', {class: 'btn ghost sm', type: 'button', onClick: () => { sessionFilter = ''; renderList(); }}, icon('x'), 'bỏ lọc phiên')) : null),
        items.length ? h('label', {class: 'row', style: {gap: '6px', cursor: 'pointer'}}, h('input', {type: 'checkbox', checked: items.every(r => checked.has(r.id)) && items.length > 0, onChange: e => { items.forEach(r => e.target.checked ? checked.add(r.id) : checked.delete(r.id)); renderList(); }}), 'Chọn tất cả') : null);
      lastOrder = items.map(r => r.id);
      // Bình luận đến lúc AI TẮT không phải lỗi của AI — dọn một lần cho hàng chờ gọn.
      const offline = ['todo', 'unanswered'].includes(filter) ? items.filter(r => (eff(r).no_reply_reason || '').startsWith('AI đang tắt')) : [];
      if (offline.length >= 3) {
        listMeta.append(h('div', {class: 'offline-note'}, icon('info', {size: 14}), h('span', {class: 'grow'}, `${offline.length} bình luận đến lúc AI đang tắt`),
          btn('Bỏ qua tất cả', {size: 'sm', kind: 'ghost', onClick: async () => {
            const ok = await confirmDialog({title: `Bỏ qua ${offline.length} bình luận đến lúc AI tắt?`, text: 'Chúng rời hàng “Cần xử lý” (vẫn xem được ở “Tất cả”). Hoàn tác được từng bình luận.', confirm: 'Bỏ qua'});
            if (!ok) return;
            offline.forEach(r => checked.add(r.id));
            await applyBatch('skipped', 'AI tắt lúc bình luận đến — bỏ qua hàng loạt');
          }})));
      }
      if (!items.length) {
        clear(listBody, empty(filter === 'todo' || filter === 'unanswered' ? 'Không còn bình luận cần xử lý' : 'Không có bình luận phù hợp', query ? 'Thử từ khoá khác.' : 'Khi LIVE, bình luận mới sẽ xuất hiện ở đây.', {iconName: filter === 'todo' ? 'checkCircle' : 'inbox'}));
      } else {
        clear(listBody, items.slice(0, 400).map(row => {
          const x = eff(row);
          const product = productsById[x.selected_product_id];
          const node = h('div', {class: 'msg-row', role: 'button', tabindex: '0', 'aria-current': String(row.id === selectedId), dataset: {id: row.id}},
            h('input', {type: 'checkbox', checked: checked.has(row.id), 'aria-label': 'Chọn', onClick: e => { e.stopPropagation(); if (e.target.checked) checked.add(row.id); else checked.delete(row.id); renderBatch(); }}),
            personAvatar(x.nickname || x.unique_id, x.user_id || x.unique_id, 34),
            h('div', {style: {minWidth: '0'}},
              h('div', {class: 'msg-head'}, h('strong', {}, x.nickname || x.unique_id || 'Khách'), statusChip(x), x.taught ? chip('Đã dạy AI', 'ok', {iconName: 'sparkles'}) : null, h('span', {class: 'time', title: dateTime(x.ts)}, relative(x.ts))),
              h('div', {class: 'msg-text clamp-2'}, x.corrected_text || x.original_text || x.text),
              x.reply_text ? h('div', {class: 'msg-reply'}, icon(x.responder === 'ai' ? 'bot' : 'user'), h('span', {class: 'clamp-2'}, x.reply_text))
                : x.no_reply_reason ? h('div', {class: 'msg-reply'}, icon('info'), h('span', {}, x.no_reply_reason)) : null,
              product ? h('div', {class: 'row', style: {marginTop: '5px', gap: '6px'}}, bag(product.bag_index), h('span', {class: 'fs-xs muted truncate'}, product.name)) : null));
          node.addEventListener('click', () => selectRow(row.id));
          node.addEventListener('keydown', e => { if (e.key === 'Enter') selectRow(row.id); });
          return node;
        }), items.length > 400 ? h('div', {class: 'empty'}, h('p', {}, `Đang hiện 400/${items.length}. Dùng ô tìm kiếm để thu hẹp.`)) : null);
      }
      renderBatch();
    }

    function selectRow(id) {
      selectedId = id;
      ctx.replaceSilently('inbox', filter, id);
      listBody.querySelectorAll('.msg-row').forEach(n => n.setAttribute('aria-current', String(n.dataset.id === id)));
      showDetail(id);
    }

    function move(dir) {
      const items = filtered();
      let i = items.findIndex(r => r.id === selectedId);
      if (i < 0 && selectedId) {
        // Dòng đang mở vừa rời danh sách (đã dạy/đã xoá): đi tiếp từ vị trí cũ của nó.
        const was = lastOrder.indexOf(selectedId);
        const visible = new Set(items.map(r => r.id));
        const after = lastOrder.slice(was + 1).find(id => visible.has(id));
        const before = lastOrder.slice(0, Math.max(0, was)).reverse().find(id => visible.has(id));
        const target = dir > 0 ? (after || before) : (before || after);
        if (target) { selectRow(target); listBody.querySelector(`[data-id="${CSS.escape(target)}"]`)?.scrollIntoView({block: 'nearest'}); }
        else renderDetailEmpty();
        return;
      }
      const next = items[Math.max(0, Math.min(items.length - 1, i + dir))];
      if (next) { selectRow(next.id); listBody.querySelector(`[data-id="${CSS.escape(next.id)}"]`)?.scrollIntoView({block: 'nearest'}); }
    }
    d.on(window, 'keydown', e => {
      if (e.target.closest && e.target.closest('input, textarea, select')) return;
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      if (e.key === '/') { e.preventDefault(); searchInput.focus(); }
      if (e.key === 'Delete' && selectedId) { e.preventDefault(); removeRows(checked.size ? [...checked] : [selectedId]); }
    });

    function renderDetailEmpty() {
      clear(detail, h('div', {class: 'detail-pad'}, empty('Chọn một bình luận', 'Xem khách hỏi gì, AI hoặc chủ shop đã trả lời ra sao và dạy lại AI nếu cần. Phím J/K để lên xuống.', {iconName: 'message'})));
    }

    // -------------------------------------------------------- chi tiết
    async function showDetail(id) {
      const token = ++detailToken;
      clear(detail, h('div', {class: 'detail-pad'}, skeletonRows(5, 60)));
      let data;
      try { data = await request(`/api/workspace/conversations/${encodeURIComponent(id)}`); }
      catch (e) { if (token === detailToken) clear(detail, h('div', {class: 'detail-pad'}, errorBox(friendlyError(e), () => showDetail(id)))); return; }
      if (token !== detailToken) return;
      renderDetail(data);
    }

    function renderDetail(result) {
      detailDisposers.splice(0).forEach(fn => fn());
      const token = detailToken;
      const row = result.data || {};
      const x = row.effective || row;
      const st = lookup(CONVERSATION_STATUS, x.conversation_status);
      const product = productsById[x.selected_product_id];
      const delivery = lookup(DELIVERY, x.delivery_status);
      const example = row.approved_example;
      const previous = (row.context && row.context.previous_comments) || [];

      const thread = h('div', {class: 'thread'},
        previous.map(p => h('div', {class: 'bubble customer old'}, p.original_text, h('div', {class: 'b-meta'}, relative(p.ts), p.question_type ? h('span', {}, p.question_type) : null))),
        h('div', {class: 'bubble customer'}, x.original_text,
          x.corrected_text ? h('div', {class: 'fs-xs', style: {marginTop: '6px', color: 'var(--info-text)'}}, 'Chủ shop sửa lại câu hỏi: ', h('strong', {}, x.corrected_text)) : null,
          h('div', {class: 'b-meta'}, dateTime(x.ts), x.question_type ? h('span', {}, x.question_type) : null)),
        x.reply_text ? h('div', {class: 'bubble reply ' + (x.responder === 'ai' ? 'ai' : '') + (x.delivery_status === 'failed' ? ' failed' : '')}, x.reply_text,
          h('div', {class: 'b-meta'}, x.responder === 'ai' ? 'AI Kid TE' : 'Chủ shop', x.reply_at ? relative(x.reply_at) : null, h('span', {}, delivery.label)))
          : h('div', {class: 'bubble missing'}, 'Chưa có câu trả lời', x.no_reply_reason ? h('div', {class: 'b-meta'}, x.no_reply_reason) : null));

      const contextCard = h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('h3', {}, 'Bối cảnh lúc khách hỏi')),
        h('div', {class: 'card-body col gap-3'},
          product ? h('div', {class: 'row gap-3'}, productAvatar(product, 48), h('div', {class: 'grow'}, h('strong', {}, product.name), h('div', {class: 'fs-xs muted'}, `Giỏ #${product.bag_index}${product.price ? ' · ' + product.price : ''} · sản phẩm AI dựa vào để trả lời`))) : h('div', {class: 'fs-sm muted'}, 'Không xác định được sản phẩm khách đang hỏi.'),
          kv([
            ['Khách', h('span', {}, x.nickname || '—', x.unique_id ? h('span', {class: 'muted'}, ' @' + x.unique_id) : null)],
            ['Vị trí video', x.video_position_s !== undefined && x.video_position_s !== null ? clock(x.video_position_s * 1000) : '—'],
            ['AI lúc đó', x.ai_enabled ? 'Đang bật' : 'Đang tắt'],
            ['Xử lý', x.no_reply_reason || '—'],
            ['Phiên LIVE', h('button', {class: 'btn ghost sm', type: 'button', onClick: () => { sessionFilter = x.session_id; filter = 'all'; renderRail(); renderList(); }}, 'Xem cả phiên', icon('arrowRight'))],
          ])));

      // --- Dạy lại AI
      // Mặc định lấy câu AI đã gửi để NẮN NÓT lại (đúng thói quen: xem rồi sửa
      // cho hợp giọng), thay vì bắt gõ lại từ đầu.
      const answer = textarea({rows: 3, placeholder: 'Câu trả lời đúng — viết như bạn sẽ nói với khách…'});
      answer.value = x.correct_answer || x.reply_text || '';
      const originalAnswer = answer.value;
      const corrected = h('input', {class: 'input', value: x.corrected_text || '', placeholder: 'Nếu lời khách sai chính tả / thiếu ý, viết lại cho rõ (không bắt buộc)'});
      const qtype = select(QUESTION_TYPES.map(q => [q, q]), x.question_type || 'Chưa phân loại');
      let quality = x.response_quality || 'unreviewed';
      const qualityBox = h('div');
      const renderQuality = () => clear(qualityBox, segmented(RESPONSE_QUALITY, quality, v => { quality = v; renderQuality(); }, {label: 'Đánh giá câu trả lời'}));
      renderQuality();
      const status = select(OPERATOR_STATUS_CHOICES, '');
      const noteValue = x.operator_note && x.operator_note !== 'Chủ shop dạy lại trong Hộp thư' ? x.operator_note : '';
      const note = h('input', {class: 'input', value: noteValue, placeholder: 'vd: Win Gold 20 là tên khác của Đạm Cá Amino · khách hỏi COD thì nói có'});
      const productSel = select([['', '— Không gắn sản phẩm —'], ...Object.values(productsById).sort((a, b) => a.bag_index - b.bag_index).map(p => [p.product_id, `#${p.bag_index} ${p.name}`])], x.selected_product_id || '');
      const lessonSlot = h('div');

      const collect = () => {
        const changes = {};
        if (answer.value.trim() !== (x.correct_answer || '')) changes.correct_answer = answer.value.trim();
        if (corrected.value.trim() !== (x.corrected_text || '')) changes.corrected_text = corrected.value.trim();
        if (qtype.value !== (x.question_type || '')) changes.question_type = qtype.value;
        if (quality !== (x.response_quality || 'unreviewed')) changes.response_quality = quality;
        if (note.value.trim() !== noteValue) changes.operator_note = note.value.trim();
        if (productSel.value !== (x.selected_product_id || '')) changes.selected_product_id = productSel.value;
        if (status.value) changes.conversation_status = status.value;
        return changes;
      };
      const saveBtn = btn('Chỉ lưu đánh giá', {kind: 'ghost', icon: 'save', onClick: async () => {
        const changes = collect();
        if (!Object.keys(changes).length) { toast('Chưa có gì thay đổi.', 'info'); return; }
        saveBtn.disabled = true;
        try {
          const out = await request('/api/workspace/corrections', {method: 'POST', body: {resource: 'conversations', record_id: row.id, changes, reason: note.value.trim() || 'Chủ shop duyệt trong Hộp thư'}});
          toast('Đã lưu. Dữ liệu gốc vẫn được giữ nguyên.', 'ok', {action: 'Hoàn tác', onAction: async () => { await request(`/api/workspace/corrections/${encodeURIComponent(out.correction_id)}/undo`, {method: 'POST', body: {}}); refreshRow(row.id); }});
          refreshRow(row.id);
        } catch (e) { toast(friendlyError(e), 'danger'); } finally { saveBtn.disabled = false; }
      }});
      const tryAgain = async () => {
        const slot = h('div', {class: 'loading'}, h('span', {class: 'spinner'}), 'AI đang trả lời thử…');
        clear(trySlot, slot);
        try { clear(trySlot, tryResult(await tryAI(x.corrected_text || x.original_text), productsById)); }
        catch (e) { clear(trySlot, notice('danger', null, friendlyError(e))); }
      };
      const trySlot = h('div');
      const showLesson = run => {
        const card = lessonCard(run, {onTry: tryAgain, onChanged: () => ctx.store.refresh && ctx.store.refresh()});
        clear(lessonSlot, card.root, trySlot);
        if (run && ['queued', 'running'].includes(run.status)) detailDisposers.push(watchRun(run.run_id, next => card.update(next)));
        return card;
      };
      const teach = async () => {
        const approved = answer.value.trim();
        if (!approved) { toast('Viết câu trả lời đúng trước đã.', 'warn'); answer.focus(); return; }
        // Không đổi chữ nào = xác nhận câu AI đã đúng (vẫn học được giọng & kiến thức).
        if (quality === 'unreviewed') quality = approved === originalAnswer && x.responder === 'ai' ? 'good' : x.reply_text ? 'needs_fix' : 'unreviewed';
        const changes = collect();
        teachBtn.disabled = true;
        try {
          const out = await request(`/api/workspace/conversations/${encodeURIComponent(row.id)}/approve-example`, {method: 'POST', body: {expected_revision: result.revision, changes, approved_answer: approved, question_type: qtype.value, product_id: productSel.value, reason: note.value.trim()}});
          toast('Đã dạy AI — bình luận này rời hàng chờ. Đang rút kiến thức…', 'ok', {duration: 3000, action: 'Bình luận tiếp theo', onAction: () => move(1)});
          await refreshRow(row.id, {keepLesson: out.learning_run_id});
        } catch (e) { toast(friendlyError(e), 'danger'); teachBtn.disabled = false; }
      };
      const teachBtn = btn(example && example.status === 'active' ? 'Dạy lại AI' : 'Dạy AI', {kind: 'primary', icon: 'sparkles', kbd: 'Ctrl+Enter', onClick: teach});
      [answer, note, corrected].forEach(el => el.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); teach(); } }));

      const exampleState = example ? (example.status === 'active'
        ? notice('ok', 'AI đã học từ câu này', example.approved_answer || '', btn('Ngừng dùng', {size: 'sm', onClick: async () => {
          const ok = await confirmDialog({title: 'AI ngừng dùng câu mẫu này?', text: 'Kiến thức đã rút ra (nếu có) vẫn giữ — tắt riêng ở trang Trí nhớ AI.', confirm: 'Ngừng dùng', tone: 'danger'});
          if (!ok) return;
          try { await request(`/api/workspace/conversations/${encodeURIComponent(row.id)}/disable-example`, {method: 'POST', body: {reason: 'Chủ shop ngừng dùng mẫu'}}); toast('Đã ngừng dùng câu mẫu.', 'ok'); refreshRow(row.id); } catch (e) { toast(friendlyError(e), 'danger'); }
        }}))
        : notice('warn', 'Câu mẫu đang tạm ngưng', example.review_reason || 'Thông tin sản phẩm đã đổi nên mẫu cần xem lại.')) : null;

      // Bài học gần nhất của hội thoại này (vừa dạy, hoặc lần trước).
      const pendingRun = lessonHint && lessonHint.id === row.id ? lessonHint.runId : '';
      lessonHint = null;
      if (pendingRun) showLesson({run_id: pendingRun, status: 'queued'});
      else request(`/api/learning/runs?ref=${encodeURIComponent(row.id)}`).then(out => { const last = (out.runs || [])[0]; if (last && token === detailToken) showLesson(last); }).catch(() => {});

      const similar = (row.similar_approved_examples || []).slice(0, 3);
      const corrections = row.corrections || [];

      clear(detail, h('div', {class: 'detail-pad'},
        h('div', {class: 'row gap-3'}, personAvatar(x.nickname || x.unique_id, x.user_id || x.unique_id, 44),
          h('div', {class: 'grow'}, h('h2', {class: 'fs-lg fw-7'}, x.nickname || x.unique_id || 'Khách'), h('div', {class: 'fs-xs muted'}, `@${x.unique_id || '—'} · ${dateTime(x.ts)}`)),
          chip(st.label, st.tone, {size: 'lg', dot: true}),
          btn('Tiếp', {size: 'sm', kind: 'ghost', icon: 'arrowRight', kbd: 'J', onClick: () => move(1), attrs: {title: 'Bình luận tiếp theo (J)'}}),
          btn('', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: () => removeRows([row.id]), attrs: {title: 'Xoá bình luận này (vào Thùng rác 30 ngày)', 'aria-label': 'Xoá'}})),
        thread,
        exampleState,
        h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h3', {}, 'Dạy lại AI'), h('div', {class: 'sub'}, 'Nắn lại câu trả lời cho đúng giọng. AI sẽ hiểu vì sao cần sửa và dùng cho các câu hỏi khác — lời khách gốc và câu đã gửi không bị đổi.'))),
          h('div', {class: 'card-body form-grid'},
            field('Câu trả lời đúng', answer, {hint: x.correct_answer ? 'Câu bạn đã dạy lần trước — sửa tiếp nếu cần.' : x.responder === 'ai' && x.reply_text ? 'Đang điền sẵn câu AI đã gửi — sửa chỗ chưa ổn. Giữ nguyên = xác nhận AI trả lời đúng.' : 'Viết như bạn sẽ trả lời khách.'}),
            field('Điều AI cần hiểu (không bắt buộc)', note, {hint: 'Viết điều AI cần nhớ lâu dài — AI dùng cho mọi câu liên quan, không chỉ câu này.'}),
            h('div', {class: 'form-grid two'}, field('Sản phẩm khách hỏi', productSel), field('Loại câu hỏi', qtype)),
            h('details', {class: 'teach-more'}, h('summary', {}, 'Thêm: đánh giá, câu hỏi viết lại, trạng thái'),
              h('div', {class: 'form-grid'},
                h('div', {class: 'field'}, h('span', {class: 'field-label'}, 'Câu trả lời của AI/shop'), qualityBox),
                field('Câu hỏi viết lại (nếu cần)', corrected),
                field('Đổi trạng thái', status)))),
          h('footer', {class: 'card-foot'}, saveBtn, teachBtn)),
        lessonSlot,
        similar.length ? h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('h3', {}, 'Câu mẫu tương tự AI đang dùng')),
          h('div', {class: 'card-body col gap-3'}, similar.map(s => h('div', {class: 'col', style: {gap: '2px'}}, h('span', {class: 'fs-xs muted'}, s.question_text || s.question || ''), h('span', {class: 'fs-sm'}, s.approved_answer || ''))))) : null,
        contextCard,
        corrections.length ? h('section', {class: 'card'}, h('header', {class: 'card-head'}, h('h3', {}, 'Lịch sử chỉnh sửa')),
          h('div', {class: 'list'}, corrections.slice().reverse().map(c => h('div', {class: 'list-row'}, h('div', {class: 'grow'}, h('div', {class: 'fs-sm'}, Object.entries(c.changes || {}).map(([k, v]) => `${k}: ${String(v).slice(0, 60)}`).join(' · ')), h('div', {class: 'sub'}, `${relative(c.created_at)} · ${c.reason || ''}`)),
            btn('Hoàn tác', {size: 'sm', kind: 'ghost', icon: 'undo', onClick: async () => { try { await request(`/api/workspace/corrections/${encodeURIComponent(c.correction_id)}/undo`, {method: 'POST', body: {}}); toast('Đã hoàn tác.', 'ok'); refreshRow(row.id); } catch (e) { toast(friendlyError(e), 'danger'); } }}))))) : null));
    }

    async function refreshRow(id, {keepLesson = ''} = {}) {
      if (keepLesson) lessonHint = {id, runId: keepLesson};
      try {
        const out = await request(`/api/workspace/conversations/${encodeURIComponent(id)}`);
        const i = rows.findIndex(r => r.id === id);
        if (i >= 0) {
          const data = out.data;
          const taught = (data.approved_example || {}).status === 'active';
          rows[i] = {...rows[i], ...data, taught, effective: {...(data.effective || data), taught}};
        }
        renderRail(); renderList();
        if (id === selectedId) renderDetail(out);
      } catch (_) { loadAll(); }
    }

    // -------------------------------------------------------- xoá (vào Thùng rác, hoàn tác được)
    async function removeRows(ids) {
      if (!ids.length) return;
      if (ids.length > 1) {
        const ok = await confirmDialog({title: `Xoá ${ids.length} bình luận?`, text: 'Chuyển vào Thùng rác, khôi phục được trong 30 ngày. AI không học từ bình luận đã xoá.', confirm: 'Xoá', tone: 'danger'});
        if (!ok) return;
      }
      const done = [];
      for (const id of ids) {
        try { await request(`/api/workspace/conversations/${encodeURIComponent(id)}`, {method: 'DELETE', body: {reason: 'Xoá trong Hộp thư'}}); done.push(id); } catch (_) { /* tiếp */ }
      }
      const gone = new Set(done);
      const wasSelected = gone.has(selectedId);
      rows = rows.filter(r => !gone.has(r.id));
      ids.forEach(id => checked.delete(id));
      renderRail(); renderList();
      if (wasSelected) move(1);
      toast(`Đã xoá ${done.length} bình luận.`, done.length === ids.length ? 'ok' : 'warn', {action: 'Hoàn tác', onAction: async () => {
        for (const id of done) { try { await request(`/api/workspace/conversations/${encodeURIComponent(id)}/restore`, {method: 'POST', body: {}}); } catch (_) { /* */ } }
        loadAll();
      }});
    }

    // -------------------------------------------------------- hàng loạt
    function renderBatch() {
      if (!checked.size) { clear(batchSlot); return; }
      clear(batchSlot, h('div', {class: 'batch-bar'}, h('strong', {}, `Đã chọn ${checked.size}`),
        btn('Đánh dấu đã xử lý', {kind: 'primary', size: 'sm', icon: 'check', onClick: () => applyBatch('answered')}),
        btn('Bỏ qua', {kind: 'ghost', size: 'sm', onClick: () => applyBatch('skipped')}),
        btn('Xoá', {kind: 'ghost', size: 'sm', icon: 'trash', onClick: () => removeRows([...checked])}),
        btn('Bỏ chọn', {kind: 'ghost', size: 'sm', onClick: () => { checked.clear(); renderList(); }})));
    }
    async function applyBatch(status, reason = 'Xử lý hàng loạt trong Hộp thư') {
      const ids = [...checked];
      let ok = 0;
      for (const id of ids) {
        try { await request('/api/workspace/corrections', {method: 'POST', body: {resource: 'conversations', record_id: id, changes: {conversation_status: status}, reason}}); ok += 1; } catch (_) { /* tiếp */ }
      }
      toast(`Đã cập nhật ${ok}/${ids.length} bình luận.`, ok === ids.length ? 'ok' : 'warn');
      checked.clear();
      loadAll();
    }

    renderRail();
    loadAll();
    // Bình luận mới đến trong lúc LIVE: làm mới nhẹ nhàng mỗi 15s, giữ lựa chọn & vị trí cuộn.
    d.interval(async () => {
      if (document.hidden || !rows) return;
      try {
        const first = await request('/api/workspace/conversations?view=summary&limit=100&page=1');
        const known = new Set(rows.map(r => r.id));
        const fresh = (first.items || []).filter(r => !known.has(r.id));
        const updated = new Map((first.items || []).map(r => [r.id, r]));
        rows = fresh.concat(rows.map(r => updated.get(r.id) || r));
        const scroll = listBody.scrollTop;
        renderRail(); renderList();
        listBody.scrollTop = scroll;
        if (fresh.length) toast(`${fresh.length} bình luận mới`, 'info', {duration: 2500});
      } catch (_) { /* thử lại lượt sau */ }
    }, 15000);
    return {unmount: () => d.dispose()};
  },
};
