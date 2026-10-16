// Mô hình thuần cho báo cáo thiết bị âm thanh (/api/settings → audio_report).
export const MIN_PROCESS_BUILD = 19041;

export function audioProblems(report, mode = '') {
  const r = report || {};
  const out = [];
  const code = r.error_code || 'ok';
  if (code === 'soundcard_unavailable') out.push({kind: 'danger', title: 'Thiếu thành phần nghe âm thanh', text: 'Bộ đọc âm thanh của Kid TE chưa nạp được — cài lại hoặc cập nhật Kid TE.'});
  else if (code === 'audio_service_stopped') out.push({kind: 'danger', title: 'Dịch vụ âm thanh của Windows đang tắt', text: 'Bật lại dịch vụ Windows Audio (hoặc khởi động lại máy), rồi bấm "Quét lại thiết bị".'});
  else if (code === 'no_active_output') out.push({kind: 'warn', title: 'Không có loa/tai nghe nào đang bật', text: 'Cắm loa hoặc bật thiết bị phát trong Cài đặt âm thanh Windows, rồi bấm "Quét lại thiết bị".'});
  else if (code === 'enumeration_failed') out.push({kind: 'danger', title: 'Windows báo lỗi khi liệt kê thiết bị', text: `Mã lỗi: ${r.error_detail || 'không rõ'}. Thử cập nhật driver âm thanh hoặc khởi động lại máy.`});
  const build = r.windows_build;
  if (mode === 'process' && build && build < MIN_PROCESS_BUILD) out.push({kind: 'warn', title: `Windows bản cũ (build ${build}) chưa hỗ trợ nghe riêng TikTok Studio`, text: "Chọn 'Thiết bị âm thanh riêng' hoặc cập nhật Windows."});
  if (String(r.mic_privacy || '').toLowerCase() === 'deny') out.push({kind: 'warn', title: 'Quyền micro của Windows đang tắt', text: 'Có thể chặn thu âm. Bật ở Cài đặt Windows → Quyền riêng tư → Micro.'});
  for (const item of r.missing_saved_devices || []) out.push({kind: 'warn', title: 'Thiết bị đã chọn không còn trên máy này', text: 'Thiết bị lưu từ máy cũ không tìm thấy. Chọn lại thiết bị hoặc bấm "Dùng loa mặc định".', id: item.id});
  return out;
}

export function compactReport(report) {
  const r = report || {};
  return {
    error_code: r.error_code, error_detail: r.error_detail, soundcard_ok: r.soundcard_ok,
    windows_build: r.windows_build, process_loopback_supported: r.process_loopback_supported,
    audio_service: r.audio_service, mic_privacy: r.mic_privacy,
    default_speaker_name: r.default_speaker_name,
    devices: (r.devices || []).map(d => ({name: d.name, loopback: !!d.loopback, disabled: !!d.disabled})),
    missing_saved_devices: (r.missing_saved_devices || []).length,
    suggested_device: r.suggested_device ? r.suggested_device.name : null,
  };
}
