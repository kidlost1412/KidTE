// Trang "Trí nhớ AI": AI đang nhớ gì, thử AI, phong cách từng người trực, học lại toàn bộ.
import {h, clear, disposer, debounce} from '../core/dom.js';
import {mountDocuments} from './documents.js';
import {icon} from '../core/icons.js';
import {request, friendlyError} from '../core/api.js';
import {relative, dateTime, fold, number} from '../core/format.js';
import {btn, chip, empty, errorBox, skeletonRows, pageHead, tabs, notice, field, select, textarea, toggle, stat, bag} from '../ui/kit.js';
import {toast, confirmDialog, promptDialog} from '../ui/overlays.js';
import {loadCatalog} from './video/data.js';
import {KIND, STATUS, itemRow, lessonCard, watchRun, setItemStatus, tryResult, tryAI} from './learning-ui.js';
import {openPromptViewer} from './prompt-view-ui.js';
import {LIMITS as PERSONA_LIMITS, PROFILE_HINTS, PROFILE_EXAMPLE, PROFILE_AGE_HINT, proposePersona, savePersona, styleCounter} from './persona-model.js';

const TABS = [
  {id: 'knowledge', label: 'Kiến thức', icon: 'layers'},
  {id: 'docs', label: 'Tài liệu', icon: 'fileText'},
  {id: 'try', label: 'Thử AI', icon: 'play'},
  {id: 'style', label: 'Phong cách người trực', icon: 'user'},
  {id: 'relearn', label: 'Học lại toàn bộ', icon: 'refresh'},
];

export default {
  mount(ctx) {
    const d = disposer();
    const tab = TABS.some(t => t.id === ctx.route.sub) ? ctx.route.sub : 'knowledge';
    let state = null;
    let error = null;
    let productsById = {};
    let filter = 'suggested';
    let query = '';
    const body = h('div', {}, skeletonRows(6, 56));
    const statsSlot = h('div');
    clear(ctx.shell.main, h('div', {class: 'page'},
      pageHead('Trí nhớ AI', 'Mỗi lần bạn sửa câu trả lời, AI rút ra điều cần nhớ — tên gọi khác của sản phẩm, dữ kiện, cách xử lý — và dùng cho MỌI câu hỏi liên quan, theo đúng giọng của người đang trực. Không còn chép lại nguyên câu mẫu.'),
      statsSlot,
      tabs(TABS, tab, id => ctx.navigate('brain', id)),
      h('div', {style: {marginTop: '16px'}}, body)));

    async function load() {
      error = null;
      try {
        const [s, cat] = await Promise.all([request('/api/learning'), loadCatalog().catch(() => ({byId: {}}))]);
        state = s; productsById = cat.byId || {};
        const counts = s.stats.by_status;
        if (filter === 'suggested' && !counts.suggested) filter = 'active';
      } catch (e) { error = e; }
      render();
    }

    function renderStats() {
      if (!state) { clear(statsSlot); return; }
      const st = state.stats;
      const persona = state.personas.find(p => p.persona_id === state.active_persona);
      clear(statsSlot, h('div', {class: 'stat-grid', style: {marginBottom: '14px'}},
        stat('Đang nhớ', number(st.by_status.active), `${st.active_by_kind.alias} tên gọi · ${st.active_by_kind.fact} dữ kiện · ${st.active_by_kind.rule} cách xử lý`),
        stat('Chờ bạn duyệt', number(st.by_status.suggested), st.by_status.suggested ? 'AI đề xuất từ các bản sửa gần đây' : 'Không có đề xuất mới', {tone: st.by_status.suggested ? 'warn' : ''}),
        stat('Lượt AI đã dùng', number(st.used_total), 'số lần kiến thức được đưa vào câu trả lời'),
        stat('Người đang trực', persona ? persona.name : '—', st.last_relearn_at ? `Học lại lần cuối ${relative(st.last_relearn_at)}` : 'Chưa học lại toàn bộ lần nào')));
    }

    function render() {
      if (error) { clear(body, errorBox(friendlyError(error), load)); return; }
      if (!state) return;
      renderStats();
      if (tab === 'knowledge') renderKnowledge();
      else if (tab === 'docs') { if (!body.dataset.docs) { body.dataset.docs = '1'; mountDocuments(ctx, body, d); } }
      else if (tab === 'try') renderTry();
      else if (tab === 'style') renderStyle();
      else renderRelearn();
    }

    // ---------------------------------------------------------------- Kiến thức
    function productOptions(value = '') {
      return select([['', '— Chung của shop —'], ...Object.values(productsById).sort((a, b) => a.bag_index - b.bag_index).map(p => [p.product_id, `#${p.bag_index} ${p.name}`])], value);
    }

    // Ô "Dạy AI một điều" giữ nguyên qua các lần vẽ lại danh sách (không mất chữ đang gõ / thẻ kết quả).
    let teachUI = null;
    function getTeachUI() {
      if (teachUI) return teachUI;
      // Dạy nhanh: gõ một điều, AI tự phân loại và đề xuất.
      const note = textarea({rows: 2, placeholder: 'Dạy AI một điều mới, viết tự nhiên — vd: “Win Gold 20 là tên khác của Đạm Cá Amino”, “khách hỏi ship COD thì nói có, 2–3 ngày tới”'});
      const product = productOptions();
      const lessonSlot = h('div');
      const teach = async () => {
        const text = note.value.trim();
        if (text.length < 4) { note.focus(); return; }
        try {
          const out = await request('/api/learning/distill', {method: 'POST', body: {note: text, product_id: product.value}});
          note.value = '';
          const card = lessonCard(null, {onChanged: load});
          clear(lessonSlot, card.root);
          d.add(watchRun(out.run_id, run => card.update(run)));
        } catch (e) { toast(friendlyError(e), 'danger'); }
      };
      note.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); teach(); } });

      teachUI = {note, product, lessonSlot, teach};
      return teachUI;
    }

    function renderKnowledge() {
      const {note, product, lessonSlot, teach} = getTeachUI();
      const counts = state.stats.by_status;
      const filters = [['suggested', `Chờ duyệt (${counts.suggested})`], ['active', `Đang dùng (${counts.active})`], ['off', `Đã tắt (${counts.retired + counts.rejected})`], ['all', 'Tất cả']];
      const search = h('input', {class: 'input', type: 'search', placeholder: 'Tìm trong kiến thức…', value: query});
      search.addEventListener('input', debounce(() => { query = fold(search.value); renderList(); }, 150, d));
      const listSlot = h('div');

      function renderList() {
        const items = state.items.filter(i => (filter === 'all' || (filter === 'off' ? ['retired', 'rejected'].includes(i.status) : i.status === filter))
          && (!query || fold(`${i.statement} ${(i.terms || []).join(' ')} ${i.product_name}`).includes(query)));
        if (!items.length) {
          clear(listSlot, empty(filter === 'suggested' ? 'Không có đề xuất chờ duyệt' : 'Chưa có kiến thức nào', filter === 'suggested' ? 'Sửa câu trả lời trong Hộp thư và bấm “Dạy AI” — đề xuất sẽ hiện ở đây.' : 'Dạy AI ở ô phía trên, hoặc sửa câu trả lời trong Hộp thư.', {iconName: 'sparkles'}));
          return;
        }
        // Gom theo sản phẩm: dễ thấy AI đang biết gì về từng món.
        const groups = new Map();
        items.forEach(i => { const k = i.product_id || ''; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(i); });
        const ordered = [...groups.entries()].sort((a, b) => (a[0] ? (productsById[a[0]]?.bag_index || 999) : -1) - (b[0] ? (productsById[b[0]]?.bag_index || 999) : -1));
        const bulk = filter === 'off' ? h('div', {class: 'row', style: {gap: '8px', marginBottom: '10px'}},
          btn(`Xoá hẳn tất cả mục đã tắt (${items.length})`, {size: 'sm', kind: 'ghost', icon: 'trash', onClick: async () => { const ok = await confirmDialog({title: `Xoá hẳn ${items.length} mục đã tắt?`, confirm: 'Xoá hẳn', tone: 'danger'}); if (ok) { await request('/api/learning/delete', {method: 'POST', body: {item_ids: items.map(i => i.item_id)}}); load(); } }}))
          : filter === 'suggested' ? h('div', {class: 'row', style: {gap: '8px', marginBottom: '10px'}},
          btn(`Ghi nhớ tất cả (${items.length})`, {kind: 'primary', size: 'sm', icon: 'check', onClick: async () => { await setItemStatus(items.map(i => i.item_id), 'active'); toast(`AI đã ghi nhớ ${items.length} điều.`, 'ok'); load(); }}),
          btn('Bỏ tất cả', {size: 'sm', kind: 'ghost', onClick: async () => { const ok = await confirmDialog({title: `Bỏ ${items.length} đề xuất?`, confirm: 'Bỏ', tone: 'danger'}); if (ok) { await setItemStatus(items.map(i => i.item_id), 'rejected'); load(); } }})) : null;
        clear(listSlot, bulk, ordered.map(([pid, group]) => {
          const p = productsById[pid];
          return h('section', {class: 'card k-group'},
            h('header', {class: 'card-head'}, h('div', {class: 'row', style: {gap: '8px'}}, p ? bag(p.bag_index) : icon('home', {size: 16}), h('h3', {}, p ? p.name : pid ? (group[0].product_name || 'Sản phẩm không còn trong giỏ hôm nay') : 'Chung của shop')), h('span', {class: 'fs-xs muted'}, `${group.length} mục`)),
            h('div', {}, group.map(item => itemRow(item, {actions: itemActions(item)}))));
        }));
      }

      function itemActions(item) {
        const set = status => async () => { try { await setItemStatus([item.item_id], status); load(); } catch (e) { toast(friendlyError(e), 'danger'); } };
        const edit = async () => {
          const value = await promptDialog({title: 'Sửa điều AI cần nhớ', label: 'Nội dung', value: item.statement, multiline: true, confirm: 'Lưu'});
          if (value === null || value.trim() === item.statement) return;
          const terms = await promptDialog({title: 'Từ khoá kích hoạt', text: 'Khi câu khách có một trong các từ này (cách nhau bằng dấu phẩy), AI chắc chắn nhớ tới mục này. Để trống nếu không cần.', label: 'Từ khoá', value: (item.terms || []).join(', '), confirm: 'Lưu'});
          try {
            await request('/api/learning/items', {method: 'POST', body: {item_id: item.item_id, changes: {statement: value.trim(), ...(terms !== null ? {terms: terms.split(',').map(t => t.trim()).filter(Boolean)} : {})}}});
            toast('Đã sửa.', 'ok'); load();
          } catch (e) { toast(friendlyError(e), 'danger'); }
        };
        const hardDelete = btn('', {size: 'sm', kind: 'ghost', icon: 'trash', attrs: {title: 'Xoá hẳn mục này', 'aria-label': 'Xoá'}, onClick: async () => {
          const ok = await confirmDialog({title: 'Xoá hẳn mục kiến thức này?', text: 'Khác với “Tắt”: xoá hẳn thì không bật lại được.', confirm: 'Xoá hẳn', tone: 'danger'});
          if (!ok) return;
          try { await request('/api/learning/delete', {method: 'POST', body: {item_ids: [item.item_id]}}); toast('Đã xoá.', 'ok'); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
        }});
        if (item.status === 'suggested') return [btn('Ghi nhớ', {size: 'sm', kind: 'primary', icon: 'check', onClick: set('active')}), btn('Sửa', {size: 'sm', kind: 'ghost', onClick: edit}), btn('Bỏ', {size: 'sm', kind: 'ghost', onClick: set('rejected')}), hardDelete];
        if (item.status === 'active') return [btn('Sửa', {size: 'sm', kind: 'ghost', icon: 'edit', onClick: edit}), btn('Tắt', {size: 'sm', kind: 'ghost', onClick: set('retired')}), hardDelete];
        return [btn('Bật lại', {size: 'sm', kind: 'ghost', icon: 'refresh', onClick: set('active')}), hardDelete];
      }

      clear(body,
        h('section', {class: 'card', style: {marginBottom: '16px'}},
          h('header', {class: 'card-head'}, h('div', {}, h('h3', {}, 'Dạy AI một điều'), h('div', {class: 'sub'}, 'AI tự phân loại (tên gọi khác / dữ kiện / cách xử lý) và gắn đúng sản phẩm. Ctrl+Enter để gửi.'))),
          h('div', {class: 'card-body col gap-3'}, note, h('div', {class: 'row wrap', style: {gap: '10px'}}, h('div', {style: {minWidth: '260px', flex: '1'}}, product), btn('Dạy AI', {kind: 'primary', icon: 'sparkles', onClick: teach})), lessonSlot)),
        h('div', {class: 'toolbar'},
          h('div', {class: 'segmented', role: 'group'}, filters.map(([id, label]) => h('button', {type: 'button', 'aria-pressed': String(filter === id), onClick: () => { filter = id; renderKnowledge(); }}, label))),
          h('div', {class: 'search-input'}, icon('search'), search)),
        listSlot);
      renderList();
    }

    // ---------------------------------------------------------------- Thử AI
    function renderTry() {
      const input = h('input', {class: 'input', placeholder: 'Gõ câu khách có thể hỏi — vd “win gold 20 giá bao nhiêu”', style: {fontSize: '15px'}});
      const pin = select([['', '— Sản phẩm đang ghim thật —'], ...Object.values(productsById).sort((a, b) => a.bag_index - b.bag_index).map(p => [p.product_id, `Giả sử đang ghim #${p.bag_index} ${p.name}`])], '');
      const out = h('div');
      const history = [];
      const run = async () => {
        const q = input.value.trim();
        if (q.length < 2) return;
        clear(out, h('div', {class: 'loading'}, h('span', {class: 'spinner'}), 'AI đang soạn câu trả lời…'));
        try {
          const res = await tryAI(q, pin.value ? {pin_id: pin.value} : {});
          history.unshift({q, res});
          clear(out, history.slice(0, 6).map(({q: question, res: r}) => h('section', {class: 'card', style: {marginBottom: '12px'}},
            h('div', {class: 'card-body col gap-3'}, h('div', {class: 'bubble customer'}, question), tryResult(r, productsById)))));
        } catch (e) { clear(out, errorBox(friendlyError(e))); }
      };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); run(); } });
      clear(body,
        notice('info', 'Chạy đúng như lúc LIVE — nhưng không gửi, không ghi vào Hộp thư', 'Dùng để kiểm tra AI đã hiểu điều bạn dạy chưa. Mỗi lần thử tốn một lượt gọi DeepSeek (rất rẻ).'),
        h('div', {class: 'row wrap', style: {gap: '10px', margin: '14px 0'}}, h('div', {class: 'grow', style: {minWidth: '320px'}}, input), h('div', {style: {minWidth: '260px'}}, pin), btn('Thử', {kind: 'primary', icon: 'play', onClick: run})),
        out);
      requestAnimationFrame(() => input.focus());
    }

    // ---------------------------------------------------------------- Phong cách
    function renderStyle() {
      const people = state.personas;
      clear(body,
        notice('info', 'Mỗi người trực LIVE một giọng', 'Luật bán hàng (giá, quà, độ dài, chống bịa) là chung và luôn được cập nhật cùng ứng dụng. Giọng nói, tính cách, xưng hô nằm HẾT ở đây: chọn người đang trực — AI nói theo giọng người đó (nam hay nữ đều được). Hướng dẫn giọng được AI tự viết lại từ các câu người đó sửa mỗi lần “Học lại toàn bộ”; bạn sửa tay được.'),
        personaCreator(),
        h('div', {class: 'settings-grid', style: {marginTop: '14px'}}, people.map(p => personaCard(p)),
          h('section', {class: 'card persona-add'}, h('div', {class: 'card-body col gap-3', style: {alignItems: 'center', textAlign: 'center', padding: '28px'}},
            icon('plus', {size: 22}), h('strong', {}, 'Thêm người trực'), h('p', {class: 'fs-xs muted'}, 'Mỗi người trực LIVE nên có một phong cách riêng để AI không nói lẫn giọng.'),
            btn('Thêm', {kind: 'primary', onClick: async () => {
              const name = await promptDialog({title: 'Tên người trực', label: 'Tên', placeholder: 'vd: Chị Hoa (xưng em, giọng miền Tây)'});
              if (!name) return;
              try { await request('/api/learning/persona', {method: 'POST', body: {name}}); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
            }})))));
    }

    // Tạo phong cách bằng AI: tả mình bằng lời -> AI đề xuất hướng dẫn giọng + 3 câu mẫu -> sửa -> lưu / chọn trực.
    function personaCreator() {
      const profile = textarea({rows: 4, placeholder: PROFILE_EXAMPLE, maxlength: PERSONA_LIMITS.profile});
      const result = h('div', {class: 'col gap-3'});
      const go = btn('Tạo bằng AI', {kind: 'primary', icon: 'play', onClick: generate});
      async function generate() {
        go.disabled = true;
        clear(result, h('div', {class: 'muted fs-sm'}, 'Đang nhờ DeepSeek viết phong cách…'));
        try { showProposal(await proposePersona(request, profile.value)); }
        catch (e) { clear(result, notice('danger', null, friendlyError(e))); }
        go.disabled = false;
      }
      function showProposal(form) {
        const name = h('input', {class: 'input', value: form.name, maxlength: PERSONA_LIMITS.name});
        const desc = h('input', {class: 'input', value: form.description, maxlength: PERSONA_LIMITS.description});
        const guide = textarea({rows: 8});
        guide.value = form.guide;
        const counter = h('div', {class: 'fs-xs muted'});
        const update = () => { const c = styleCounter(guide.value); counter.textContent = `${c.used}/${PERSONA_LIMITS.style} ký tự`; counter.style.color = c.over ? 'var(--danger, #c0392b)' : ''; };
        guide.addEventListener('input', update); update();
        const save = async activate => {
          try {
            await savePersona(request, {name: name.value, description: desc.value, guide: guide.value}, activate);
            toast(activate ? 'Đã lưu và chọn người này trực.' : 'Đã lưu phong cách.', 'ok');
            load();
          } catch (e) { toast(friendlyError(e), 'danger'); }
        };
        clear(result,
          field('Tên phong cách', name), field('Mô tả ngắn', desc),
          field('Hướng dẫn giọng (sửa thoải mái)', guide, {hint: 'Mỗi dòng một ý: xưng hô, từ hay dùng, độ dài/nhịp, cách đùa, cách chốt, điều tránh.'}), counter,
          h('div', {class: 'col gap-2'}, h('strong', {class: 'fs-sm'}, 'AI trả lời thử theo giọng này'),
            ...form.samples.map(x => h('div', {class: 'fs-sm'}, h('div', {class: 'muted fs-xs'}, x.comment ? `Khách: “${x.comment}”` : 'Khách:'), h('div', {}, x.reply || '—')))),
          h('div', {class: 'row', style: {gap: '8px', justifyContent: 'flex-end'}},
            btn('Tạo lại', {kind: 'ghost', icon: 'refresh', onClick: generate}),
            btn('Lưu', {icon: 'save', onClick: () => save(false)}),
            btn('Lưu và chọn người này trực', {kind: 'primary', icon: 'user', onClick: () => save(true)})));
      }
      return h('section', {class: 'card', style: {marginTop: '14px'}},
        h('header', {class: 'card-head'}, h('div', {class: 'row', style: {gap: '8px'}}, icon('user', {size: 16}), h('h3', {}, 'Tạo phong cách bằng AI'))),
        h('div', {class: 'card-body col gap-3'},
          h('p', {class: 'fs-sm muted'}, `Tả về mình như đang kể cho bạn nghe: ${PROFILE_HINTS.join(', ')}. AI sẽ viết hướng dẫn giọng và trả lời thử 3 câu để bạn xem trước.`),
          h('p', {class: 'fs-sm', 'data-persona-age-hint': ''}, PROFILE_AGE_HINT),
          profile, h('div', {class: 'row', style: {justifyContent: 'flex-end'}}, go), result));
    }

    function personaCard(p) {
      const active = p.persona_id === state.active_persona;
      const desc = h('input', {class: 'input', value: p.description || '', placeholder: 'Tự mô tả ngắn: xưng hô, vùng miền, nam/nữ…'});
      const guide = textarea({rows: 7, placeholder: 'Chưa có — AI sẽ tự viết sau khi người này sửa vài câu và bấm “Học lại toàn bộ”. Có thể tự viết: mỗi dòng một ý.'});
      guide.value = p.style_guide || '';
      const save = async () => {
        try { await request('/api/learning/persona', {method: 'POST', body: {persona_id: p.persona_id, description: desc.value.trim(), style_guide: guide.value}}); toast('Đã lưu phong cách.', 'ok'); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
      };
      return h('section', {class: 'card' + (active ? ' persona-active' : '')},
        h('header', {class: 'card-head'}, h('div', {class: 'row', style: {gap: '8px'}}, icon('user', {size: 16}), h('h3', {}, p.name)),
          active ? chip('Đang trực', 'ok', {dot: true}) : btn('Chọn người này trực', {size: 'sm', onClick: async () => { await request('/api/learning/persona', {method: 'POST', body: {persona_id: p.persona_id, activate: true}}); toast(`AI sẽ nói theo giọng ${p.name}.`, 'ok'); load(); }})),
        h('div', {class: 'card-body col gap-3'}, field('Mô tả', desc),
          p.builtin_voice ? h('details', {}, h('summary', {class: 'fs-sm'}, 'Giọng dựng sẵn của ứng dụng (luôn áp dụng, không sửa được)'), h('pre', {class: 'style-pre'}, p.builtin_voice)) : null,
          field(p.builtin_voice ? 'Bổ sung thêm cho giọng này (AI tự học hoặc bạn viết)' : 'Hướng dẫn giọng AI đang dùng', guide, {hint: p.style_updated_at ? `Cập nhật ${relative(p.style_updated_at)}` : ''})),
        h('footer', {class: 'card-foot'},
          p.persona_id !== 'default' ? btn('Xoá', {kind: 'ghost', size: 'sm', onClick: async () => { const ok = await confirmDialog({title: `Xoá phong cách “${p.name}”?`, text: 'Kiến thức đã học không bị xoá.', confirm: 'Xoá', tone: 'danger'}); if (ok) { await request('/api/learning/persona', {method: 'POST', body: {persona_id: p.persona_id, delete: true}}); load(); } }}) : null,
          h('span', {class: 'grow'}), btn('Xem toàn bộ lời nhắc', {size: 'sm', icon: 'fileText', onClick: () => openPromptViewer(p, {onChange: load})}),
          btn('Lưu', {kind: 'primary', size: 'sm', icon: 'save', onClick: save})));
    }

    // ---------------------------------------------------------------- Học lại toàn bộ
    function renderRelearn() {
      const runs = state.runs || [];
      const s = state.settings;
      const running = runs.find(r => ['queued', 'running'].includes(r.status));
      const latest = runs.find(r => r.status === 'done');
      const modelSel = select([['', `Giống model trả lời (${state.reply_model})`], ...state.models.map(m => [m.id, m.label])], s.learning_model);
      const effortSel = select([['low', 'Suy luận nhẹ — nhanh'], ['high', 'Suy luận kỹ (khuyên dùng)'], ['max', 'Suy luận tối đa — chậm, kỹ nhất']], s.learning_effort);
      const saveSettings = async patch => { try { state.settings = (await request('/api/learning/settings', {method: 'POST', body: patch})).settings; toast('Đã lưu.', 'ok', {duration: 1500}); } catch (e) { toast(friendlyError(e), 'danger'); } };
      modelSel.addEventListener('change', () => saveSettings({learning_model: modelSel.value}));
      effortSel.addEventListener('change', () => saveSettings({learning_effort: effortSel.value}));
      const proposalSlot = h('div');
      clear(body,
        h('div', {class: 'settings-grid'},
          h('section', {class: 'card'},
            h('header', {class: 'card-head'}, h('div', {}, h('h3', {}, 'Học lại toàn bộ'), h('div', {class: 'sub'}, 'Gom mọi kiến thức + mọi bản sửa: gộp trùng, bỏ điều đã lỗi thời, chỉ ra chỗ mâu thuẫn, viết lại giọng từng người trực.'))),
            h('div', {class: 'card-body col gap-3'},
              running ? h('div', {class: 'loading'}, h('span', {class: 'spinner'}), 'Đang học lại… (thường 20–60 giây)') : null,
              h('p', {class: 'fs-sm soft'}, 'Kết quả là một ĐỀ XUẤT để bạn xem trước. Chỉ khi bấm “Áp dụng” thì trí nhớ mới đổi — và luôn hoàn tác được.'),
              btn(running ? 'Đang chạy…' : 'Học lại ngay', {kind: 'primary', icon: 'sparkles', disabled: Boolean(running) || !state.model_ready, onClick: async () => {
                try { const out = await request('/api/learning/relearn', {method: 'POST', body: {}}); toast('Bắt đầu học lại…', 'info'); d.add(watchRun(out.run_id, run => { if (!['queued', 'running'].includes(run.status)) load(); }, {interval: 3000})); load(); } catch (e) { toast(friendlyError(e), 'danger'); }
              }}),
              !state.model_ready ? notice('warn', null, 'Cần khoá DeepSeek (Cài đặt) để học lại.') : null)),
          h('section', {class: 'card'},
            h('header', {class: 'card-head'}, h('h3', {}, 'Cách AI học')),
            h('div', {class: 'card-body col gap-3'},
              toggle('Tự học lại sau mỗi phiên LIVE (khi có từ 3 bản sửa mới, 30 phút không có bình luận)', s.auto_relearn, v => saveSettings({auto_relearn: v})),
              toggle('Tự ghi nhớ đề xuất AI rất chắc chắn (không cần duyệt)', s.auto_activate, v => saveSettings({auto_activate: v})),
              field('Model dùng để học', modelSel, {hint: 'Việc học chạy nền, không ảnh hưởng tốc độ trả lời LIVE.'}),
              field('Mức suy luận khi học', effortSel)))),
        h('div', {class: 'row between', style: {margin: '22px 0 10px'}}, h('div', {class: 'section-title'}, 'Các lượt học lại'),
          runs.some(r => ['error', 'discarded', 'reverted'].includes(r.status)) ? btn('Dọn lượt lỗi / đã bỏ', {size: 'sm', kind: 'ghost', icon: 'trash', onClick: async () => { await request('/api/learning/delete-runs', {method: 'POST', body: {}}); load(); }}) : null),
        runs.length ? h('div', {class: 'card'}, h('div', {class: 'list'}, runs.map(r => h('button', {class: 'list-row', type: 'button', onClick: () => showProposal(r, proposalSlot)},
          icon(r.status === 'applied' ? 'checkCircle' : r.status === 'error' ? 'xCircle' : r.status === 'done' ? 'sparkles' : 'clock', {size: 16}),
          h('div', {class: 'grow'}, h('div', {class: 'title'}, r.output && r.output.summary ? r.output.summary : r.status === 'error' ? 'Lỗi: ' + r.error : 'Đang chạy…'),
            h('div', {class: 'sub'}, `${dateTime(r.created_at)} · ${r.trigger === 'auto' ? 'tự động sau LIVE' : 'bấm tay'}${r.model ? ' · ' + r.model : ''}`)),
          chip({done: 'Chờ áp dụng', applied: 'Đã áp dụng', discarded: 'Đã bỏ', reverted: 'Đã hoàn tác', error: 'Lỗi', running: 'Đang chạy', queued: 'Đang chờ'}[r.status] || r.status, r.status === 'done' ? 'warn' : r.status === 'applied' ? 'ok' : ''))))) : empty('Chưa học lại lần nào', 'Bấm “Học lại ngay” sau khi đã dạy AI vài câu.', {iconName: 'refresh'}),
        proposalSlot);
      if (latest) showProposal(latest, proposalSlot);
    }

    function showProposal(run, slot) {
      const p = run.output || {};
      if (!p.items && !p.retire_ids) { clear(slot); return; }
      const byId = new Map(state.items.map(i => [i.item_id, i]));
      const section = (title, rows, tone) => rows.length ? h('div', {class: 'col gap-2'}, h('div', {class: 'section-title'}, `${title} · ${rows.length}`), rows.map(r => h('div', {class: 'k-diff ' + tone}, r))) : null;
      const describe = entry => h('div', {}, h('div', {}, entry.statement), h('div', {class: 'fs-xs muted'}, (KIND[entry.kind] || KIND.fact).label, entry.product_id && productsById[entry.product_id] ? ` · #${productsById[entry.product_id].bag_index} ${productsById[entry.product_id].name}` : ' · chung',
        (entry.terms || []).length ? ` · từ khoá: ${entry.terms.join(', ')}` : '',
        entry.from_ids && entry.from_ids.length > 1 ? ` · gộp từ ${entry.from_ids.length} mục` : ''));
      const items = p.items || [];
      clear(slot, h('section', {class: 'card', style: {marginTop: '16px'}},
        h('header', {class: 'card-head'}, h('div', {}, h('h3', {}, 'Đề xuất ' + dateTime(run.created_at)), h('div', {class: 'sub'}, p.summary || '')),
          run.status === 'done' ? h('div', {class: 'row', style: {gap: '8px'}},
            btn('Bỏ đề xuất', {kind: 'ghost', size: 'sm', onClick: async () => { await request('/api/learning/discard', {method: 'POST', body: {run_id: run.run_id}}); load(); }}),
            btn('Áp dụng', {kind: 'primary', size: 'sm', icon: 'check', onClick: async () => { try { const out = await request('/api/learning/apply', {method: 'POST', body: {run_id: run.run_id}}); toast(`Đã áp dụng: ${out.created} mục mới/gộp, ${out.retired} mục tắt.`, 'ok', {action: 'Hoàn tác', onAction: async () => { await request('/api/learning/undo', {method: 'POST', body: {run_id: run.run_id}}); load(); }}); load(); } catch (e) { toast(friendlyError(e), 'danger'); } }}))
            : run.status === 'applied' ? btn('Hoàn tác lượt này', {size: 'sm', icon: 'undo', onClick: async () => { await request('/api/learning/undo', {method: 'POST', body: {run_id: run.run_id}}); toast('Đã trả trí nhớ về như trước lượt này.', 'ok'); load(); }}) : null),
        h('div', {class: 'card-body col gap-4'},
          section('Kiến thức mới', items.filter(i => i.action === 'new').map(describe), 'add'),
          section('Gộp mục trùng', items.filter(i => i.action === 'merge').map(describe), 'merge'),
          section('Viết lại cho rõ', items.filter(i => i.action === 'revise').map(e => h('div', {}, h('div', {class: 'fs-xs muted', style: {textDecoration: 'line-through'}}, (byId.get(e.from_ids[0]) || {}).statement || ''), describe(e))), 'merge'),
          section('Tắt vì sai / lỗi thời', (p.retire_ids || []).map(id => h('div', {}, (byId.get(id) || {}).statement || id)), 'remove'),
          section('Mâu thuẫn — bạn quyết', (p.conflicts || []).map(c => h('div', {}, h('div', {}, c.explain), c.ids.map(id => h('div', {class: 'fs-xs muted'}, '• ' + ((byId.get(id) || {}).statement || id))))), 'conflict'),
          section('Giọng người trực', (p.personas || []).map(s => h('div', {}, h('strong', {}, s.name), h('pre', {class: 'style-pre'}, s.after))), 'merge'),
          items.every(i => i.action === 'keep') && !(p.retire_ids || []).length && !(p.personas || []).length ? h('div', {class: 'fs-sm muted'}, 'Trí nhớ đã gọn gàng — không có gì cần đổi.') : null)));
    }

    load();
    return {unmount: () => d.dispose()};
  },
};
