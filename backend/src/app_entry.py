"""Điểm vào duy nhất của Kid TE ở bản mã nguồn và bản đóng gói sau này."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import urllib.request

from config import CHAT_HOST, CHAT_PORT, INSTALL_ROOT, IS_FROZEN, LOG_DIR, PLUGINS_DIR, ROOT
from console_io import force_utf8_console
from instance_lock import InstanceAlreadyRunning, SingleInstanceLock
import auto_backup
import problems
from port_diagnostics import owners_for_port
from plugin_registry import discover_plugins
from plugin_runtime import PluginRuntimeManager
from runtime_layout import prepare_user_layout
from service_supervisor import ServiceSupervisor
from structured_log import configure_logger
from server import Handler, LoopbackHTTPServer, ReplyEngine
from update_manager import UpdateManager, select_active_gpu


def run_desktop_ui(server, url, *, webview_module=None):
    """Chạy HTTP nền rồi mở đúng một cửa sổ; trả False nếu UI không mở được."""
    loaded = [False]
    worker = None
    try:
        if webview_module is None:
            import webview as webview_module
        worker = threading.Thread(
            target=server.serve_forever, args=(0.3,), daemon=True,
            name="kid-te-http",
        )
        worker.start()
        from ui_preferences import load_window_size, save_window_size
        width, height = load_window_size(ROOT / "runtime" / "ui-window.json")
        window = webview_module.create_window(
            "Kid TE — Trợ lý vận hành LIVE", url,
            width=width, height=height, min_size=(960, 640),
        )
        events = getattr(window, "events", None)
        if events is not None and getattr(events, "loaded", None) is not None:
            events.loaded += lambda: loaded.__setitem__(0, True)
        webview_module.start()
        save_window_size(ROOT / "runtime" / "ui-window.json", window)
        server.shutdown()
        worker.join(timeout=3)
        return True
    except Exception as exc:
        if loaded[0]:
            # pywebview can raise while its native window is being destroyed.
            # The window did open; close the backend instead of falling back to
            # an invisible console server that keeps the instance lock.
            print(f"[kid-te] Giao diện đã đóng: {type(exc).__name__}: {exc}")
            server.shutdown()
            if worker is not None:
                worker.join(timeout=3)
            return True
        print(f"[kid-te] KHÔNG MỞ ĐƯỢC GIAO DIỆN: {type(exc).__name__}: {exc}")
        return False


def run_client_ui(url, *, webview_module=None):
    """Mở cửa sổ điều khiển khi backend đã chạy nhưng không có UI để focus."""
    try:
        if webview_module is None:
            import webview as webview_module
        from ui_preferences import load_window_size, save_window_size
        width, height = load_window_size(ROOT / "runtime" / "ui-window.json")
        window = webview_module.create_window(
            "Kid TE — Trợ lý vận hành LIVE", url,
            width=width, height=height, min_size=(960, 640),
        )
        webview_module.start()
        save_window_size(ROOT / "runtime" / "ui-window.json", window)
        request = urllib.request.Request(url.rsplit("/app", 1)[0] + "/api/shutdown",
                                         data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except Exception as exc:
            print(f"[kid-te] Không gửi được yêu cầu đóng backend: {type(exc).__name__}")
        return True
    except Exception as exc:
        print(f"[kid-te] KHÔNG MỞ ĐƯỢC GIAO DIỆN KẾT NỐI: {type(exc).__name__}: {exc}")
        return False


def notify_existing_instance(host, port):
    try:
        request = urllib.request.Request(
            f"http://{host}:{int(port)}/api/focus", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                return False
            try:
                result = json.loads(response.read().decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                return False
            return result.get("ok") is True
    except Exception:
        return False


def existing_backend_ready(host, port):
    """Chỉ mở cửa sổ kết nối khi tiến trình giữ lock thực sự đang phục vụ web."""
    try:
        with urllib.request.urlopen(f"http://{host}:{int(port)}/app", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> int:
    force_utf8_console()
    prepare_user_layout()
    parser = argparse.ArgumentParser(description="Kid TE")
    parser.add_argument("--host", default=CHAT_HOST)
    parser.add_argument("--port", type=int, default=CHAT_PORT)
    parser.add_argument("--safe-mode", action="store_true",
                        help="chỉ chạy core/AI, không cho bật service plugin")
    parser.add_argument("--no-ui", action="store_true",
                        help="chạy backend trong console, không mở cửa sổ")
    args = parser.parse_args()
    lock = SingleInstanceLock(ROOT / "runtime" / "live-re.lock")
    try:
        lock.acquire()
    except InstanceAlreadyRunning as exc:
        print(f"[kid-te] KHÔNG KHỞI ĐỘNG: {exc}")
        url = f"http://{args.host}:{args.port}/app"
        if notify_existing_instance(args.host, args.port):
            print("[kid-te] Đã đưa cửa sổ Kid TE đang chạy lên trước.")
            return 0
        if not args.no_ui and existing_backend_ready(args.host, args.port) and run_client_ui(url):
            print("[kid-te] Đã mở cửa sổ kết nối với Kid TE đang chạy nền.")
            return 0
        print("[kid-te] Phiên giữ khóa chưa phục vụ cổng web. Đóng phiên cũ rồi mở lại Kid TE.")
        return 2

    engine = None
    server = None
    auto_scheduler = None
    ly_do_dung = "graceful"
    logger = configure_logger("core", LOG_DIR)
    configure_logger("http", LOG_DIR, console=False)
    if lock.stale_lock:
        # Lần chạy trước kết thúc mà không qua được khối dọn dẹp (bị kill cứng,
        # mất điện, hoặc sập). Nó không ghi được dòng log nào, nên nếu không
        # ghi ở đây thì lần chạy đó biến mất hoàn toàn khỏi lịch sử.
        logger.warning(
            f"pid={lock.stale_lock.get('pid')} "
            f"started_at={lock.stale_lock.get('started_at')}",
            extra={"event": "previous_run_ended_unexpectedly"})
        print("[kid-te] Lần chạy trước không tắt bình thường "
              f"(PID {lock.stale_lock.get('pid')}). Đã dọn khoá cũ.")
    try:
        logger.info("auto_v1", extra={"event": "backup_policy"})
        problems.attach(logger)
        manifests, plugin_errors = discover_plugins(PLUGINS_DIR)
        supervisor = ServiceSupervisor(ROOT / "runtime")
        # Registry chỉ nạp/công bố ở đây; bật/tắt dịch vụ qua trang "Trợ lý tự động".
        print(f"[kid-te] Plugin hợp lệ: {', '.join(sorted(manifests)) or 'không có'}")
        logger.info(",".join(sorted(manifests)), extra={"event": "plugins_loaded"})
        for plugin_id, error in sorted(plugin_errors.items()):
            print(f"[kid-te] Plugin tắt ({plugin_id}): {error}")
            logger.error(error, extra={"event": "plugin_invalid", "component": plugin_id})
        app_root = os.environ.get("KIDTE_APP_ROOT", "") or (str(INSTALL_ROOT) if IS_FROZEN else "")
        if app_root:
            select_active_gpu(app_root)
        engine = ReplyEngine()
        engine.updates = UpdateManager(app_root or None)
        try:
            # Máy trắng: cài gói hồ sơ chính thức đi kèm (không bao giờ ghi đè).
            seeded = engine.ensure_seed_knowledge()
            if seeded.get("status") != "existing_untouched":
                print(f"[kid-te] Gói hồ sơ chính thức: {seeded.get('status')}")
            logger.info(seeded.get("status", ""), extra={"event": "knowledge_seed"})
        except Exception as exc:       # không có hồ sơ thì AI im lặng, ứng dụng vẫn mở
            print(f"[kid-te] CẢNH BÁO gói hồ sơ chính thức: {type(exc).__name__}: {exc}")
            logger.error(str(exc), extra={"event": "knowledge_seed_failed"})
        engine.plugin_errors = plugin_errors
        engine.plugin_runtime = PluginRuntimeManager(
            manifests, supervisor, safe_mode=args.safe_mode, logger=logger,
        )
        engine.safe_mode = args.safe_mode
        if getattr(engine, "learner", None) is not None:
            engine.learner.start()   # vòng học nền: rút kiến thức + tự học lại sau LIVE
        Handler.engine = engine
        try:
            server = LoopbackHTTPServer((args.host, args.port), Handler)
        except OSError as exc:
            # Lock của chính app đã có, nhưng cổng lại do tiến trình khác giữ.
            # Không taskkill: báo chính xác để người dùng quyết định.
            print(f"[kid-te] KHÔNG MỞ ĐƯỢC CỔNG {args.host}:{args.port}: {exc}")
            owners = owners_for_port(args.host, args.port)
            if owners:
                for owner in owners:
                    print("[kid-te] Cổng đang do "
                          f"{owner['process']} (PID {owner['pid']}) giữ. Không tự tắt tiến trình này.")
            else:
                print("[kid-te] Không xác định được tiến trình giữ cổng; không tự tắt tiến trình nào.")
            return 3

        def stop(signum=None, _frame=None):
            nonlocal ly_do_dung
            ly_do_dung = f"signal:{signum}" if signum is not None else "signal"
            logger.info(ly_do_dung, extra={"event": "backend_stop_requested"})
            threading.Thread(target=server.shutdown, daemon=True).start()
            webview = sys.modules.get("webview")
            for window in getattr(webview, "windows", []) if webview else []:
                try:
                    window.destroy()
                except Exception as exc:
                    logger.error(str(exc), extra={"event": "window_close_failed"})

        engine.request_shutdown = stop

        # Windows gui Ctrl+Break thanh SIGBREAK, khong phai SIGINT. Ban cu chi
        # dang ky SIGINT/SIGTERM nen Ctrl+Break rot xuong trinh xu ly mac dinh
        # cua Python: tien trinh chet voi 0xC000013A, khoi `finally` khong
        # chay, lock o lai va khong co dong log nao ve viec dung.
        for ten in ("SIGINT", "SIGTERM", "SIGBREAK"):
            so_hieu = getattr(signal, ten, None)
            if so_hieu is not None:
                try:
                    signal.signal(so_hieu, stop)
                except (ValueError, OSError):
                    pass    # khong dang ky duoc thi bo qua, khong chan khoi dong
        print(f"[kid-te] READY http://{args.host}:{args.port}")
        if args.safe_mode:
            print("[kid-te] SAFE MODE: video bị khóa")
        logger.info(f"{args.host}:{args.port}", extra={"event": "backend_ready"})
        captcha = getattr(engine, "captcha", None)
        if not args.safe_mode and captcha is not None and captcha.status()["configured"]:
            # CAPTCHA follows the app lifecycle. Start outside the UI thread
            # because Windows may show UAC.
            def _autostart_captcha():
                result = captcha.start()   # the bridge records failures in last-start.json
                if not result.get("ok"):
                    logger.warning(str(result.get("error")), extra={"event": "captcha_autostart_failed"})
            threading.Thread(target=_autostart_captcha, daemon=True,
                             name="captcha-autostart").start()
        if auto_backup.enabled():
            # Background only (delayed, cancellable); never blocks startup or the UI.
            auto_scheduler = auto_backup.AutoBackupScheduler(
                live_provider=getattr(engine, "_is_live_running", None),
                maintenance_provider=lambda: bool(getattr(getattr(engine, "maintenance", None), "blocked", lambda: False)()),
                logger=logger)
            auto_scheduler.start()
        print(f"[kid-te] Ứng dụng: http://{args.host}:{args.port}/app")
        if args.no_ui:
            server.serve_forever(0.3)
        elif not run_desktop_ui(server, f"http://{args.host}:{args.port}/app"):
            logger.error("pywebview không mở được; chạy console",
                         extra={"event": "desktop_ui_failed"})
            print("[kid-te] Backend vẫn chạy trong console. Mở đường dẫn Ứng dụng ở trên.")
            server.serve_forever(0.3)
    finally:
        # Dừng theo thứ tự và GHI LẠI. Trước đây khối này không ghi gì, nên
        # một lần tắt bình thường cũng không để lại dấu vết nào trong log.
        logger.info(ly_do_dung, extra={"event": "backend_stopping"})
        if auto_scheduler is not None:
            auto_scheduler.stop()
        if server is not None:
            try:
                server.server_close()
            except Exception as exc:
                logger.error(str(exc), extra={"event": "server_close_failed"})
        if engine is not None:
            captcha = getattr(engine, "captcha", None)
            if captcha is not None:
                try:
                    result = captcha.stop(closing=True)
                    if not result.get("ok"):
                        logger.error(str(result), extra={"event": "captcha_stop_failed"})
                except Exception as exc:
                    logger.error(str(exc), extra={"event": "captcha_stop_failed"})
            # Thoát app thì dừng luôn dịch vụ nghe âm thanh — không để chạy ngầm mãi.
            runtime = getattr(engine, "plugin_runtime", None)
            if runtime is not None:
                try:
                    result = runtime.stop_all()
                    if not result.get("ok"):
                        logger.error(str(result), extra={"event": "services_stop_failed"})
                except Exception as exc:
                    logger.error(str(exc), extra={"event": "services_stop_failed"})
            try:
                engine.close()
            except Exception as exc:
                logger.error(str(exc), extra={"event": "engine_close_failed"})
        lock.release()
        logger.info(ly_do_dung, extra={"event": "backend_stopped"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
