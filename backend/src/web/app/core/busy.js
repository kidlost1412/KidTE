// Tự khoá nút khi trình xử lý bấm trả về Promise (chống bấm đúp). Không phụ thuộc DOM thật.

/** Bọc onClick: nếu trả Promise thì khoá nút tới khi xong/lỗi. */
export function withBusyLock(handler) {
  if (typeof handler !== 'function') return handler;
  return function busyLocked(event) {
    const button = event && event.currentTarget;
    if (button && button.dataset && button.dataset.busy === '1') return undefined;
    const result = handler.call(this, event);
    if (!button || !result || typeof result.then !== 'function') return result;
    const wasDisabled = Boolean(button.disabled);
    button.dataset.busy = '1';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.classList.add('busy');
    const unlock = () => {
      delete button.dataset.busy;
      button.removeAttribute('aria-busy');
      button.classList.remove('busy');
      button.disabled = wasDisabled;
    };
    return Promise.resolve(result).finally(unlock);
  };
}
