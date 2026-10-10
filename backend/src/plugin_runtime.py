"""Điều phối lifecycle plugin đã được registry xác thực."""
from __future__ import annotations

import sys
from pathlib import Path

from service_supervisor import ServiceSpec


RESOURCE_LABELS = {
    "thiet-bi-am-thanh": "Thiết bị âm thanh",
    "mang": "Mạng",
    "du-lieu-cuc-bo": "Dữ liệu trên máy",
}

# Lý do từ supervisor → câu người vận hành hiểu được.
REASON_TEXT = {
    "restart_limit": "Dịch vụ bị lỗi liên tục nên tạm khoá 5 phút — xem Nhật ký.",
    "restart_backoff": "Vừa khởi động lại, chờ vài giây rồi thử lại.",
    "start_failed": "Không chạy được dịch vụ (thiếu file hoặc Python).",
    "stop_timeout": "Dịch vụ không chịu dừng trong 10 giây.",
    "pid_identity_mismatch": "Tiến trình đang giữ PID không phải dịch vụ này — không động tới.",
    "unmanaged_running": "Dịch vụ đang chạy nhưng không dừng được từ đây.",
}


class PluginRuntimeManager:
    def __init__(self, manifests, supervisor, *, safe_mode=False, logger=None):
        self.manifests = dict(manifests or {})
        self.supervisor = supervisor
        self.safe_mode = bool(safe_mode)
        self.states = {plugin_id: "nap" for plugin_id in self.manifests}
        # `[T9]` logger tuỳ chọn — không log thì mọi hành vi y hệt trước đây
        # (test cũ không cần đổi gì). Ghi log CHỈ ở hành động chủ động
        # (start/stop/safe_mode) và khi trạng thái dịch vụ THAY ĐỔI lúc
        # snapshot — không log mỗi lần UI poll, tránh spam log.
        self.logger = logger
        self._last_runtime_state = {}

    def _log(self, event, plugin_id, **fields):
        """`JsonFormatter` (structured_log.py) chỉ ghi đúng 5 khoá cố định
        (timestamp/component/severity/event/correlation_id/message) — mọi
        thứ trong `extra` ngoài `event`/`component` bị BỎ QUA khi ghi ra
        file, không phải một khoá JSON riêng. Vì vậy chi tiết (pid, reason,
        ...) phải nhét vào chuỗi `message`, giống đúng cách
        `app_entry.py` đã làm với `logger.info(str(backup_result), ...)`."""
        if not self.logger:
            return
        detail = ", ".join(f"{key}={value}" for key, value in fields.items()
                          if value is not None)
        message = f"{event} ({detail})" if detail else event
        try:
            self.logger.info(
                message, extra={"event": event, "component": plugin_id, **fields},
            )
        except Exception:
            pass  # logging không bao giờ được làm hỏng thao tác plugin thật.

    def _note_runtime_transition(self, plugin_id, runtime):
        """Ghi log CHỈ khi trạng thái dịch vụ thật sự đổi so với lần quan
        sát trước — bằng chứng cho 'service chết -> lõi mở lại -> attach
        đúng PID' và 'service chết giữa chừng bị phát hiện', không phải
        nhật ký của từng lượt UI hỏi trạng thái."""
        current = runtime.get("state")
        previous = self._last_runtime_state.get(plugin_id)
        self._last_runtime_state[plugin_id] = current
        if previous == current:
            return
        if current == "running" and runtime.get("attached"):
            self._log("service_reattached_after_restart", plugin_id,
                      pid=runtime.get("pid"), uptime_s=runtime.get("uptime_s"))
        elif previous == "running" and runtime.get("reason") in (
            "stale_pid", "pid_identity_mismatch",
        ):
            self._log("service_crash_detected", plugin_id,
                      reason=runtime.get("reason"))

    @staticmethod
    def _spec(manifest):
        if getattr(sys, "frozen", False) and manifest.kind == "dich-vu" and manifest.plugin_id == "dong-bo-video":
            from component_runtime import component_path
            executable = component_path("video-sync", "KidTEVideo.exe")
            if executable is None:
                executable = Path(sys.executable).resolve().parent / "KidTEVideo.exe"
            command = (str(executable), *manifest.command[1:])
        else:
            command = (sys.executable, str(manifest.command_path), *manifest.command[1:])
        return ServiceSpec(
            manifest.plugin_id,
            command,
            manifest.directory,
        )

    def snapshot(self):
        plugins = []
        for plugin_id, manifest in sorted(self.manifests.items()):
            if manifest.kind == "bo-xu-ly":
                runtime = {"state": "in_process"}
                state = "dang_chay"
            else:
                try:
                    spec = self._spec(manifest)
                    if getattr(sys, "frozen", False) and not Path(spec.command[0]).is_file():
                        runtime = {"state": "not_installed", "reason": "Chưa cài Đồng bộ video LIVE — vào Cài đặt → Cập nhật để tải."}
                    else:
                        runtime = self.supervisor.status(spec)
                except (OSError, RuntimeError, ValueError):
                    runtime = {"state": "invalid_package", "reason": "Gói đồng bộ video bị lỗi — cài lại ở Cài đặt → Cập nhật."}
                state = "dang_chay" if runtime.get("state") == "running" else self.states[plugin_id]
                self._note_runtime_transition(plugin_id, runtime)
            plugins.append({
                "id": plugin_id, "name": manifest.name, "kind": manifest.kind,
                "version": manifest.version, "state": state,
                "runtime": runtime,
                "resources": [RESOURCE_LABELS.get(item, item) for item in manifest.resources],
                "description": manifest.description,
            })
        return {"ok": True, "safe_mode": self.safe_mode, "plugins": plugins}

    def start(self, plugin_id):
        if self.safe_mode:
            return {"ok": False, "error": "safe_mode_blocks_plugins"}
        manifest = self.manifests.get(str(plugin_id))
        if not manifest:
            return {"ok": False, "error": "plugin_not_available"}
        if manifest.kind != "dich-vu":
            return {"ok": False, "error": "processor_managed_by_core"}
        try:
            spec = self._spec(manifest)
        except (OSError, RuntimeError, ValueError):
            return {"ok": False, "error": "Gói đồng bộ video bị lỗi — cài lại ở Cài đặt → Cập nhật."}
        if getattr(sys, "frozen", False) and not Path(spec.command[0]).is_file():
            return {"ok": False, "error": "Chưa cài Đồng bộ video LIVE — vào Cài đặt → Cập nhật để tải."}
        result = self.supervisor.start(spec, manual=True)
        self._last_runtime_state[plugin_id] = result.get("state")
        self.states[plugin_id] = (
            "dang_chay" if result.get("state") == "running" else "can_nguoi_xu_ly"
        )
        if result.get("reason") == "restart_limit":
            self._log("service_restart_limit_reached", plugin_id,
                      max_restarts=getattr(self.supervisor, "max_restarts", None))
        elif result.get("state") == "running":
            self._log("service_start_ok", plugin_id, pid=result.get("pid"),
                      attached=bool(result.get("attached")))
        else:
            self._log("service_start_failed", plugin_id, reason=result.get("reason"))
        ok = result.get("state") == "running"
        return {"ok": ok, "plugin_id": plugin_id, "state": self.states[plugin_id], "runtime": result,
                **({} if ok else {"error": REASON_TEXT.get(result.get("reason"), result.get("reason") or "start_failed")})}

    def stop(self, plugin_id):
        manifest = self.manifests.get(str(plugin_id))
        if not manifest:
            return {"ok": False, "error": "plugin_not_available"}
        if manifest.kind != "dich-vu":
            return {"ok": False, "error": "processor_managed_by_core"}
        result = self.supervisor.stop(self._spec(manifest), timeout_s=10)
        self._last_runtime_state[plugin_id] = result.get("state")
        self.states[plugin_id] = (
            "tat" if result.get("state") == "stopped" else "can_nguoi_xu_ly"
        )
        self._log("service_stop_result", plugin_id, state=result.get("state"),
                  reason=result.get("reason"))
        ok = result.get("state") == "stopped"
        return {"ok": ok, "plugin_id": plugin_id, "state": self.states[plugin_id], "runtime": result,
                **({} if ok else {"error": REASON_TEXT.get(result.get("reason"), result.get("reason") or "stop_failed")})}

    def restart(self, plugin_id):
        stopped = self.stop(plugin_id)
        if not stopped["ok"]:
            return stopped
        return self.start(plugin_id)

    def stop_all(self):
        """Dừng mọi service (bật chế độ an toàn, hoặc khi thoát ứng dụng)."""
        results = []
        for plugin_id, manifest in sorted(self.manifests.items()):
            if manifest.kind != "dich-vu":
                continue
            try:
                results.append(self.stop(plugin_id))
            except Exception as exc:
                self._log("service_stop_failed", plugin_id, error=type(exc).__name__)
                results.append({"ok": False, "plugin_id": plugin_id, "error": "stop_failed"})
        return {"ok": all(item["ok"] for item in results), "results": results}

    def set_safe_mode(self, enabled):
        enabled = bool(enabled)
        stopped = self.stop_all() if enabled else {"ok": True, "results": []}
        self.safe_mode = enabled
        self._log("safe_mode_changed", "core", enabled=enabled, ok=stopped["ok"])
        return {"ok": stopped["ok"], "safe_mode": enabled,
                "stop_results": stopped["results"]}
