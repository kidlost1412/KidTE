import {request} from './api.js';

export async function waitJob(jobId, {signal, onProgress = () => {}, intervalMs = 1000} = {}) {
  while (!signal?.aborted) {
    const job = await request('/api/system/jobs/' + encodeURIComponent(jobId), {signal});
    onProgress(job);
    if (job.state === 'completed') return job.result;
    if (['failed', 'interrupted'].includes(job.state)) {
      throw new Error(job.state === 'interrupted'
        ? 'Tác vụ bị gián đoạn. Kiểm tra trạng thái trước khi thực hiện lại.'
        : job.error === 'backup_busy' ? 'Có thao tác sao lưu hoặc xóa đang chạy. Chờ hoàn tất rồi thử lại.'
        : 'Tác vụ thất bại: ' + (job.error || 'unknown'));
    }
    await new Promise((resolve, reject) => {
      const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(new DOMException('Aborted', 'AbortError')); };
      const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, intervalMs);
      signal?.addEventListener('abort', abort, {once: true});
      if (signal?.aborted) abort();
    });
  }
  throw new DOMException('Aborted', 'AbortError');
}
