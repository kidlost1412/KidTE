// Tải video lên để phân tích. Chunk 8 MB, tiếp nối đúng byte server đã
// commit, xác minh cùng một file bằng vân tay (đầu + cuối 1 MB + cỡ + tên + ngày sửa).
import {request, ApiError} from '../../core/api.js';

const CHUNK = 8 * 1024 * 1024;
const PROBE = 1024 * 1024;
export const ACCEPT = '.mp4,.mov,.mkv,.webm,.avi,.m4v';
export const MAX_VIDEO_BYTES = 100 * 1024 ** 3;
const EXT = /\.(mp4|mov|mkv|webm|avi|m4v)$/i;

// Video LIVE xuất lại có thể trùng đầu/cuối 1 MB và cùng cỡ; thêm tên và
// lastModified để không nối nhầm vào phần đã tải của một file khác.
export async function contentProbe(file) {
  const head = file.slice(0, Math.min(PROBE, file.size));
  const tail = file.slice(Math.max(0, file.size - PROBE));
  const identity = `
${file.size}
${file.name}
${Number(file.lastModified) || 0}`;
  const bytes = new Uint8Array(await new Blob([head, tail, identity]).arrayBuffer());
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// Các lượt tải đang chạy — sống qua việc chuyển trang trong ứng dụng.
const active = new Map();
const listeners = new Set();
function emit() { listeners.forEach(fn => { try { fn([...active.values()]); } catch (_) { /* */ } }); }
export function onUploadsChange(fn) { listeners.add(fn); fn([...active.values()]); return () => listeners.delete(fn); }

export function validateFile(file) {
  if (!file) return 'Chưa chọn file.';
  if (!EXT.test(file.name)) return 'Định dạng chưa hỗ trợ. Dùng MP4, MOV, MKV, WebM, AVI hoặc M4V.';
  if (file.size < 1024 * 1024) return 'File quá nhỏ, có thể không phải video.';
  if (!Number.isSafeInteger(file.size) || file.size > MAX_VIDEO_BYTES) return 'Video phải có kích thước tối đa 100 GB.';
  return '';
}

/** Bắt đầu (hoặc tiếp tục) tải một file. Trả về entry theo dõi được. */
export function startUpload(file, {title} = {}) {
  const key = `${file.name}|${file.size}|${file.lastModified}`;
  if (active.has(key) && ['uploading', 'checking'].includes(active.get(key).state)) return active.get(key);
  const entry = {
    key, file, name: file.name, size: file.size, title: title || file.name.replace(/\.[^.]+$/, ''),
    state: 'checking', uploaded: 0, progress: 0, speed: 0, eta: 0, error: '', jobId: '', resumed: false,
    controller: new AbortController(),
  };
  entry.cancel = () => { entry.controller.abort(); };
  entry.retry = () => { active.delete(key); return startUpload(file, {title: entry.title}); };
  entry.dismiss = () => { active.delete(key); emit(); };
  active.set(key, entry);
  emit();
  run(entry).catch(() => { /* đã ghi vào entry */ });
  return entry;
}

async function run(entry) {
  const {file} = entry;
  const signal = entry.controller.signal;
  try {
    const probe = await contentProbe(file);
    const resume = await request('/api/video/upload/resume', {method: 'POST', body: {filename: file.name, total_size: file.size, source_probe: probe}, signal});
    let job = resume.job;
    if (job) entry.resumed = true;
    else job = await request('/api/video/upload/start', {method: 'POST', body: {filename: file.name, total_size: file.size, source_probe: probe}, signal});
    entry.jobId = job.job_id;
    entry.state = 'uploading';
    let offset = Number(job.uploaded_size || 0);
    entry.uploaded = offset; entry.progress = offset / file.size * 100; emit();
    let failures = 0;
    const samples = [];
    while (offset < file.size) {
      if (signal.aborted) throw new ApiError('aborted');
      const end = Math.min(file.size, offset + CHUNK);
      const t0 = performance.now();
      try {
        const out = await request(`/api/video/upload/chunk?job_id=${encodeURIComponent(job.job_id)}&offset=${offset}`, {
          method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file.slice(offset, end), signal,
        });
        const sent = Number(out.uploaded_size) - offset;
        offset = Number(out.uploaded_size);
        failures = 0;
        samples.push({bytes: sent, ms: performance.now() - t0});
        if (samples.length > 6) samples.shift();
        const totalBytes = samples.reduce((a, s) => a + s.bytes, 0), totalMs = samples.reduce((a, s) => a + s.ms, 0);
        entry.speed = totalMs ? totalBytes / (totalMs / 1000) : 0;
        entry.eta = entry.speed ? (file.size - offset) / entry.speed : 0;
        entry.uploaded = offset; entry.progress = offset / file.size * 100; entry.error = '';
        emit();
      } catch (error) {
        if (error.code === 'aborted') throw error;
        if (error.status === 409 && error.payload && Number.isFinite(Number(error.payload.committed_offset))) {
          offset = Number(error.payload.committed_offset); continue;
        }
        if (error.code === 'upload_part_truncated_on_disk') throw error;
        failures += 1;
        if (failures >= 6) throw error;
        entry.error = `Kết nối chập chờn — đang thử lại (${failures}/5). Phần đã tải vẫn được giữ.`;
        emit();
        await new Promise(resolve => setTimeout(resolve, 600 * 2 ** (failures - 1)));
        try {
          const saved = await request(`/api/video/analysis/status?job_id=${encodeURIComponent(job.job_id)}`, {signal});
          if (Number(saved.uploaded_size) > offset) offset = Number(saved.uploaded_size);
        } catch (_) { /* thử chunk lại */ }
      }
    }
    entry.state = 'finishing'; emit();
    await request('/api/video/upload/finish', {method: 'POST', body: {job_id: job.job_id, title: entry.title}, signal});
    entry.state = 'done'; entry.progress = 100; emit();
    setTimeout(() => { if (active.get(entry.key) === entry) { active.delete(entry.key); emit(); } }, 4000);
  } catch (error) {
    entry.state = error.code === 'aborted' ? 'paused' : 'failed';
    entry.error = error.code === 'aborted' ? 'Đã tạm dừng. Bấm “Tiếp tục” — phần đã tải không mất.' : error;
    emit();
    throw error;
  }
}
