import {h, clear, disposer} from '../../core/dom.js';
import {icon} from '../../core/icons.js';
import {request, friendlyError} from '../../core/api.js';
import {clock, durationText, relative, bytes, number} from '../../core/format.js';
import {btn, chip, bag, bagColor, productAvatar, empty, errorBox, skeletonRows, notice, pageHead, progress, iconBtn} from '../../ui/kit.js';
import {toast, confirmDialog, openMenu} from '../../ui/overlays.js';
import {JOB_STATE, JOB_PHASE, JOB_PHASE_ORDER, lookup} from '../../labels.js';
import {fromLibrary, withEnds, checks} from './model.js';
import {loadCatalog, resolveProduct} from './data.js';
import {startUpload, onUploadsChange, validateFile, ACCEPT} from './upload.js';

function uploadCard(entry) {
  const stateText = {
    checking: 'Đang kiểm tra file…', uploading: entry.resumed ? 'Đang tải tiếp' : 'Đang tải lên',
    finishing: 'Đang hoàn tất…', done: 'Đã tải xong — chuyển sang phân tích', paused: 'Đã tạm dừng', failed: 'Tải thất bại',
  }[entry.state] || entry.state;
  const tone = entry.state === 'failed' ? 'danger' : entry.state === 'paused' ? 'warn' : 'info';
  const detail = entry.state === 'uploading'
    ? `${bytes(entry.uploaded)} / ${bytes(entry.size)}${entry.speed ? ` · ${bytes(entry.speed)}/s · còn khoảng ${Math.ceil(entry.eta / 60)} phút` : ''}`
    : bytes(entry.size);
  const actions = [];
  if (entry.state === 'uploading' || entry.state === 'checking') actions.push(btn('Tạm dừng', {size: 'sm', icon: 'pause', onClick: () => entry.cancel()}));
  if (entry.state === 'paused' || entry.state === 'failed') actions.push(btn('Tiếp tục', {size: 'sm', kind: 'primary', icon: 'play', onClick: () => entry.retry()}), iconBtn('x', 'Ẩn', () => entry.dismiss(), {size: 'sm'}));
  return h('div', {class: 'job-card'},
    h('div', {class: 'job-icon'}, icon('upload')),
    h('div', {class: 'grow'},
      h('div', {class: 'row'}, h('strong', {class: 'truncate'}, entry.name), chip(stateText, tone)),
      h('div', {class: 'fs-xs muted', style: {margin: '4px 0 8px'}}, detail),
      progress(entry.progress, {indeterminate: entry.state === 'checking' || entry.state === 'finishing'}),
      entry.error ? h('div', {class: 'fs-xs', style: {marginTop: '6px', color: entry.state === 'failed' ? 'var(--danger-text)' : 'var(--warn-text)'}}, typeof entry.error === 'string' ? entry.error : friendlyError(entry.error)) : null),
    h('div', {class: 'row'}, actions));
}

function jobCard(job, ctx, reload) {
  const st = lookup(JOB_STATE, job.state);
  const phaseIndex = JOB_PHASE_ORDER.indexOf(job.phase);
  const phases = h('div', {class: 'job-phases', title: JOB_PHASE[job.phase] || ''}, JOB_PHASE_ORDER.map((p, i) => h('span', {
    class: job.state === 'error' && i === Math.max(0, phaseIndex) ? 'fail' : (['review', 'published'].includes(job.state) || i < phaseIndex) ? 'done' : (i === phaseIndex && job.state === 'analyzing') ? 'active' : '',
    title: JOB_PHASE[p],
  })));
  const actions = [];
  if (job.state === 'review') actions.push(btn(`Duyệt ${job.suggestions || 0} mốc`, {kind: 'primary', icon: 'checkCircle', onClick: () => ctx.navigate('video', 'review', job.job_id)}));
  if (job.state === 'error' || job.state === 'interrupted') actions.push(btn('Chạy lại phân tích', {icon: 'refresh', onClick: async () => {
    try { await request('/api/video/analysis/resume', {method: 'POST', body: {job_id: job.job_id}}); toast('Đã xếp hàng phân tích lại.', 'ok'); reload(); } catch (e) { toast(friendlyError(e), 'danger'); }
  }}));
  if (job.state === 'published' && job.video_id) actions.push(btn('Mở Studio', {icon: 'film', onClick: () => ctx.navigate('video', 'studio', job.video_id)}));
  const more = iconBtn('more', 'Thêm', e => openMenu(e.currentTarget, [
    {label: 'Đưa vào Thùng rác', icon: 'trash', danger: true, onClick: async () => {
      const ok = await confirmDialog({title: 'Ẩn tác vụ này?', text: 'Tác vụ phân tích được đưa vào Thùng rác 30 ngày. File video trên máy không bị xoá.', confirm: 'Đưa vào Thùng rác', tone: 'danger'});
      if (!ok) return;
      try { await request(`/api/workspace/video-jobs/${encodeURIComponent(job.job_id)}`, {method: 'DELETE', body: {reason: 'Ẩn tác vụ phân tích'}}); toast('Đã đưa vào Thùng rác.', 'ok'); reload(); } catch (err) { toast(friendlyError(err), 'danger'); }
    }},
  ]));
  const sub = job.state === 'analyzing' ? (JOB_PHASE[job.phase] || 'Đang phân tích') + '…'
    : job.state === 'review' ? `AI tìm thấy ${number(job.suggestions)} mốc · ${number(job.transcript_segments)} câu thoại`
    : job.state === 'error' ? String(job.error || 'Lỗi không rõ').slice(0, 160)
    : job.state === 'uploading' ? `Mới tải ${Math.round(job.progress || 0)}% — chọn lại đúng file để tải tiếp`
    : job.state === 'published' ? `Đã phát hành ${relative(job.published_at)}` : (JOB_PHASE[job.phase] || '');
  return h('div', {class: 'job-card'},
    h('div', {class: 'job-icon', dataset: {job: job.job_id}}, icon(job.state === 'review' ? 'checkCircle' : job.state === 'error' ? 'alert' : 'film')),
    h('div', {class: 'grow'},
      h('div', {class: 'row wrap'}, h('strong', {class: 'truncate'}, job.title || job.filename || 'Video'), chip(st.label, st.tone, {dot: true}),
        job.duration_s ? chip(clock(job.duration_s * 1000), '', {iconName: 'clock'}) : null),
      h('div', {class: 'fs-xs muted', style: {marginTop: '3px'}}, sub),
      ['uploading', 'queued', 'analyzing', 'error', 'interrupted'].includes(job.state) ? phases : null),
    h('div', {class: 'row'}, actions, more));
}

function miniTimeline(doc, catalog) {
  const bar = h('div', {class: 'mini-timeline', title: 'Lịch ghim theo thời gian'});
  const total = doc.durationMs || 1;
  withEnds(doc).forEach(seg => {
    const product = resolveProduct(doc, seg.product, catalog);
    bar.append(h('i', {style: {width: ((seg.end - seg.start) / total * 100) + '%', background: product ? bagColor(product.bag_index) : '#3a4554'}, title: `${clock(seg.start)} · ${product ? product.name : 'Không ghim'}`}));
  });
  return bar;
}

function videoCard(row, ctx, catalog, liveVideoId, reload) {
  const timeline = row.timeline || {};
  const doc = fromLibrary({...timeline, video_id: row.id});
  const issues = checks(doc, catalog.byId);
  // Mỗi mục kiểm tra là MỘT sản phẩm (thông điệp gắn tên SP) — đếm số SP, không đếm lượt.
  const bagIssues = issues.filter(i => i.code === 'bag_changed').length;
  const missingIssues = issues.filter(i => i.code === 'not_in_catalog').length;
  const keys = [...new Set(doc.segments.map(s => s.product).filter(Boolean))];
  const poster = h('div', {class: 'vcard-poster'}, h('div', {class: 'vcard-noposter'}, icon('film')), h('span', {class: 'vcard-duration'}, clock(row.cycle_ms)));
  request('/api/video/assets?video_id=' + encodeURIComponent(row.id)).then(state => {
    if (state.poster_url) { poster.style.backgroundImage = `url("${state.poster_url}")`; poster.querySelector('.vcard-noposter')?.remove(); }
    else if (state.state === 'missing_media') poster.querySelector('.vcard-noposter').replaceChildren(h('div', {class: 'col', style: {alignItems: 'center', gap: '4px', fontSize: '11px', textAlign: 'center', padding: '8px'}}, icon('eyeOff'), 'Chưa có file video trên máy'));
  }).catch(() => {});
  const live = liveVideoId && liveVideoId === row.id;
  const open = () => ctx.navigate('video', 'studio', row.id);
  return h('article', {class: 'card vcard'},
    h('div', {class: 'vcard-media', onClick: open, style: {cursor: 'pointer'}},
      poster,
      h('div', {class: 'vcard-strip'},
        h('div', {class: 'row between'}, h('span', {class: 'fs-xs', style: {color: '#9fb0c3', fontWeight: 700}}, `${doc.segments.length} đoạn · ${keys.length} sản phẩm`),
          live ? chip('Đang phát khi LIVE', 'live', {dot: true}) : null),
        miniTimeline(doc, catalog),
        h('div', {class: 'vcard-products'}, keys.slice(0, 10).map(key => { const p = resolveProduct(doc, key, catalog); return h('span', {title: `${p.name}${p.bagChanged ? ` — giỏ đã đổi #${p.timelineBag}→#${p.bag_index}` : ''}`}, productAvatar(p, 36)); }),
          keys.length > 10 ? h('span', {class: 'fs-xs', style: {color: '#9fb0c3', alignSelf: 'center'}}, `+${keys.length - 10}`) : null))),
    h('div', {class: 'vcard-body'},
      h('h3', {class: 'clamp-2'}, row.title || row.id),
      h('div', {class: 'fs-xs muted'}, row.source_file || row.id),
      h('div', {class: 'vcard-meta'},
        row.transcript_segments ? chip(`${number(row.transcript_segments)} câu thoại`, 'info', {iconName: 'message'}) : chip('Chưa có lời thoại', '', {iconName: 'message'}),
        row.draft_saved_at ? chip('Có bản nháp chưa áp dụng', 'warn', {dot: true, title: 'Lưu lúc ' + relative(row.draft_saved_at)}) : null,
        bagIssues ? chip(`${bagIssues} SP lệch số giỏ`, 'danger', {iconName: 'alert', title: 'Thứ tự giỏ hàng hôm nay đã khác lúc làm kịch bản — mở Studio để cập nhật'}) : null,
        missingIssues ? chip(`${missingIssues} SP không còn bán`, 'warn', {iconName: 'alert'}) : null,
        (row.errors || []).length ? chip('Kịch bản lỗi', 'danger', {title: row.errors.join('; ')}) : null,
        (row.duplicates || []).length ? chip('Có bản trùng', 'warn', {title: 'Trùng với: ' + row.duplicates.join(', ')}) : null)),
    h('div', {class: 'vcard-foot'},
      btn('Mở Studio', {kind: 'primary', icon: 'film', onClick: open}),
      h('span', {class: 'grow'}),
      iconBtn('more', 'Thêm', e => openMenu(e.currentTarget, [
        {label: 'Mở Studio', icon: 'film', onClick: open},
        '-',
        {label: 'Đưa video vào Thùng rác', icon: 'trash', danger: true, onClick: async () => {
          const ok = await confirmDialog({title: `Đưa “${row.title}” vào Thùng rác?`, text: 'Lịch ghim của video sẽ không còn được tiện ích dùng khi LIVE. Có thể khôi phục trong 30 ngày.', confirm: 'Đưa vào Thùng rác', tone: 'danger'});
          if (!ok) return;
          try {
            await request(`/api/workspace/videos/${encodeURIComponent(row.id)}`, {method: 'DELETE', body: {expected_revision: row.revision, reason: 'Xoá video khỏi thư viện'}});
            toast('Đã đưa video vào Thùng rác.', 'ok', {action: 'Hoàn tác', onAction: async () => { await request(`/api/workspace/videos/${encodeURIComponent(row.id)}/restore`, {method: 'POST', body: {}}); reload(); }});
            reload();
          } catch (err) { toast(friendlyError(err), 'danger'); }
        }},
      ]))));
}

export function mountLibrary(ctx, host, {autoUpload = false} = {}) {
  const d = disposer();
  let jobs = [];
  let videos = null;
  let catalog = {byId: {}, products: []};
  let loadError = null;
  let uploads = [];
  const fileInput = h('input', {type: 'file', accept: ACCEPT, hidden: true, onChange: () => { pick(fileInput.files[0]); fileInput.value = ''; }});
  const choose = () => fileInput.click();

  function pick(file) {
    const problem = validateFile(file);
    if (problem) { toast(problem, 'danger'); return; }
    startUpload(file);
    toast(`Bắt đầu tải “${file.name}”. Có thể tiếp tục làm việc khác trong lúc chờ.`, 'info');
  }

  const dropzone = h('div', {class: 'dropzone'},
    h('span', {class: 'dz-icon'}, icon('upload')),
    h('div', {class: 'grow'}, h('strong', {}, 'Thêm video phát lại mới'),
      h('p', {class: 'fs-sm muted'}, 'Kéo thả file vào đây hoặc bấm chọn (tối đa 100 GB/file). AI sẽ nghe lời nói, tìm lúc host nhắc tới từng sản phẩm và đề xuất mốc ghim để bạn duyệt.')),
    btn('Chọn video…', {kind: 'primary', icon: 'upload', onClick: choose}), fileInput);
  ['dragenter', 'dragover'].forEach(t => dropzone.addEventListener(t, e => { e.preventDefault(); dropzone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(t => dropzone.addEventListener(t, e => { e.preventDefault(); dropzone.classList.remove('drag'); }));
  dropzone.addEventListener('drop', e => { const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) pick(f); });

  const reviewSlot = h('div');
  const modelSlot = h('div');
  const queueSlot = h('div');
  // Chưa có model chép lời thì tải video lên cũng không phân tích được — báo TRƯỚC.
  request('/api/system/whisper').then(state => {
    if (!state.active_path) {
      clear(modelSlot, notice('warn', 'Máy này chưa có model chép lời',
        'Video tải lên sẽ không được tự chép lời và gợi ý mốc ghim. Tải model hợp với máy (một lần, cần mạng).',
        btn('Tải model chép lời', {kind: 'primary', icon: 'download', onClick: () => ctx.navigate('system', 'whisper')})));
    }
  }).catch(() => {});
  const gridSlot = h('div', {}, skeletonRows(3, 180));
  const page = h('div', {class: 'page'},
    pageHead('Video & lịch ghim', 'Mỗi video phát lại có một lịch ghim sản phẩm. Khi LIVE, tiện ích nghe âm thanh để biết video đang ở giây nào và tự ghim đúng giỏ hàng.',
      [btn('Tải video mới', {kind: 'primary', icon: 'upload', onClick: choose})]),
    h('div', {class: 'col gap-4'}, reviewSlot, modelSlot, dropzone, queueSlot, gridSlot));
  clear(host, page);

  function renderTop() {
    const reviewJobs = jobs.filter(j => j.state === 'review');
    clear(reviewSlot, reviewJobs.length ? notice('warn', `${reviewJobs.length} video đang chờ bạn duyệt lịch ghim`,
      'AI đã nghe xong và đề xuất các mốc ghim. Duyệt xong mới phát hành được thành video dùng khi LIVE.',
      btn('Duyệt ngay', {kind: 'primary', icon: 'checkCircle', onClick: () => ctx.navigate('video', 'review', reviewJobs[0].job_id)})) : null);
    const inProgress = jobs.filter(j => j.state !== 'published');
    if (uploads.length || inProgress.length) {
      clear(queueSlot, h('section', {class: 'card'},
        h('header', {class: 'card-head'}, h('div', {class: 'grow'}, h('h2', {}, 'Đang tải & phân tích'), h('div', {class: 'sub'}, 'Mỗi lần phân tích một video. Phân tích 1 giờ video mất khoảng 5–15 phút tuỳ máy.'))),
        h('div', {}, uploads.map(uploadCard), inProgress.map(job => jobCard(job, ctx, loadJobs)))));
    } else clear(queueSlot);
  }

  let gridKey = '';
  function renderGrid(force = false) {
    if (loadError) { clear(gridSlot, errorBox(friendlyError(loadError), loadAll)); return; }
    if (!videos) return;
    const liveId = ctx.store.get().dashboard?.video?.video_id || '';
    const published = jobs.filter(j => j.state === 'published').length;
    const key = JSON.stringify([videos.map(v => v.id + v.revision + v.draft_saved_at), liveId, published, catalog.products.length]);
    if (!force && key === gridKey) return;
    gridKey = key;
    clear(gridSlot,
      h('div', {class: 'row between', style: {margin: '6px 0 12px'}},
        h('h2', {class: 'fs-lg fw-7'}, `Thư viện · ${videos.length} video`),
        published ? h('span', {class: 'fs-xs muted'}, `${published} video phát hành từ phân tích AI`) : null),
      videos.length ? h('div', {class: 'vlib-grid'}, videos.map(row => videoCard(row, ctx, catalog, liveId, loadAll)))
        : empty('Thư viện chưa có video', 'Tải video phát lại đầu tiên để AI tạo lịch ghim.', {iconName: 'film', action: btn('Tải video mới', {kind: 'primary', icon: 'upload', onClick: choose})}));
  }

  let jobTimer = 0;
  d.add(() => clearTimeout(jobTimer));
  async function loadJobs() {
    clearTimeout(jobTimer);
    try {
      const out = await request('/api/workspace/video-jobs?limit=100');
      const hadPublished = jobs.filter(j => j.state === 'published').length;
      jobs = out.items || [];
      renderTop();
      if (jobs.filter(j => j.state === 'published').length !== hadPublished) loadAll();
    } catch (e) { /* hàng chờ không quan trọng bằng thư viện */ }
    const busy = jobs.some(j => ['uploading', 'queued', 'analyzing'].includes(j.state)) || uploads.some(u => u.state !== 'failed' && u.state !== 'paused');
    jobTimer = setTimeout(loadJobs, busy ? 3000 : 15000);
  }

  async function loadAll() {
    loadError = null;
    try {
      const [list, cat] = await Promise.all([request('/api/workspace/videos?limit=100'), loadCatalog().catch(() => ({byId: {}, products: []}))]);
      videos = list.items || [];
      catalog = cat;
    } catch (e) { loadError = e; }
    renderGrid(true);
  }

  d.add(onUploadsChange(list => { uploads = list; renderTop(); }));
  d.add(ctx.store.subscribe((state, prev) => { if (state.dashboard?.video?.video_id !== prev.dashboard?.video?.video_id) renderGrid(); }));
  loadAll();
  loadJobs();
  if (autoUpload) d.timeout(choose, 150);
  return {unmount: () => d.dispose()};
}
