"""Bắt âm thanh đang phát trên máy (WASAPI loopback) để định vị video.

Dùng `soundcard` chứ không dùng `sounddevice`: bản PortAudio đi kèm
sounddevice 0.5.6 trên máy này không có loopback WASAPI
(`WasapiSettings` không nhận tham số `loopback`), nên không bắt được tiếng
đang phát ra loa/màn hình. `soundcard` nói trực tiếp với WASAPI và mở được
thiết bị PHÁT ở chế độ thu.
"""
from __future__ import annotations

import os
import sys
import threading
from collections import deque

import numpy as np
try:
    import soundcard as sc
except ImportError:
    sc = None


FORBIDDEN_DEVICE_FRAGMENTS = ("ezcap gamedock ultra",)


def _initialize_audio_thread():
    """Khoi tao COM cho chinh thread doc WASAPI tren Windows.

    ``soundcard`` khoi tao COM luc module duoc import, nhung RollingCapture
    mo recorder trong mot thread Python khac. COM la trang thai theo-thread;
    neu bo qua buoc nay moi thiet bi deu loi CO_E_NOTINITIALIZED (0x800401f0).
    Tra ve ole32 chi khi ham nay so huu mot lan khoi tao can go bo.
    """
    if os.name != "nt":
        return None
    import ctypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    initialize = ole32.CoInitializeEx
    initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    initialize.restype = ctypes.c_long
    result = int(initialize(None, 0))  # COINIT_MULTITHREADED
    if result in (0, 1):  # S_OK / S_FALSE: ca hai deu can CoUninitialize.
        return ole32
    if result & 0xFFFFFFFF == 0x80010106:  # RPC_E_CHANGED_MODE: COM da san sang.
        return None
    raise RuntimeError(f"Khong khoi tao duoc COM cho audio thread: 0x{result & 0xFFFFFFFF:08x}")


def _uninitialize_audio_thread(ole32):
    if ole32 is not None:
        ole32.CoUninitialize()


def is_forbidden_device(device):
    name = str((device or {}).get("name", "")).casefold()
    return any(fragment in name for fragment in FORBIDDEN_DEVICE_FRAGMENTS)


def list_devices():
    if sc is None:
        return []
    # BaseHTTPRequestHandler va supervisor deu goi ham nay tu worker thread.
    # WASAPI/pycaw can COM duoc khoi tao tren CHINH thread dang liet ke;
    # neu chi dua vao lan import ``soundcard`` thi Windows tra 0x800401f0 va
    # giao dien hien sai rang may khong co thiet bi am thanh.
    com = _initialize_audio_thread()
    try:
        devices = []
        for mic in sc.all_microphones(include_loopback=True):
            devices.append({
                "id": mic.id,
                "name": mic.name,
                "loopback": bool(mic.isloopback),
                "channels": int(getattr(mic, "channels", 2) or 2),
            })
        return devices
    finally:
        _uninitialize_audio_thread(com)


def resolve_device(selector):
    """Nhận số thứ tự trong danh sách, hoặc một mẩu tên (vd "VX2728")."""
    devices = list_devices()
    if not devices:
        raise RuntimeError("Không thấy thiết bị âm thanh nào")
    text = str(selector).strip()
    # Khớp theo id trước: probe_all trả về id WASAPI dạng GUID, không phải tên.
    for item in devices:
        if item["id"] == text:
            if is_forbidden_device(item):
                raise RuntimeError(f"Thiết bị bị cấm để bảo vệ dock USB: {item['name']}")
            return item
    if text.isdigit():
        index = int(text)
        if 0 <= index < len(devices):
            item = devices[index]
            if is_forbidden_device(item):
                raise RuntimeError(f"Thiết bị bị cấm để bảo vệ dock USB: {item['name']}")
            return item
        raise RuntimeError(f"Số thiết bị {index} không có trong danh sách")
    folded = text.casefold()
    forbidden_matches = [item for item in devices
                         if folded in item["name"].casefold()
                         and is_forbidden_device(item)]
    if forbidden_matches:
        raise RuntimeError(
            f"Thiết bị bị cấm để bảo vệ dock USB: {forbidden_matches[0]['name']}"
        )
    matches = [item for item in devices if folded in item["name"].casefold()
               and not is_forbidden_device(item)]
    if not matches:
        raise RuntimeError(f"Không thiết bị nào khớp {text!r}")
    # Ưu tiên loopback: tên màn hình/loa xuất hiện ở cả hai nhóm.
    matches.sort(key=lambda item: not item["loopback"])
    return matches[0]


def probe_all(seconds=6.0, rate=48000, loopback_only=True):
    """Thu cung luc tu cac thiet bi co the nghe duoc, tra ve am thanh tung cai.

    Dung cho viec tu tim thiet bi dang phat video: khong doan theo ten thiet bi
    (ten doi theo tung may) ma nghe thu roi so voi dau van tay.

    loopback_only=True la MAC DINH va rat quan trong: tieng video luon di ra
    qua duong PHAT (loopback). Mo luong thu tren ca micro/thiet bi THU that —
    tai nghe USB, webcam, micro roi — lam Windows danh thuc va do lai tung
    thiet bi USB, sinh ra thong bao "USB device not recognized" giua phien
    live. Nhung thiet bi do khong bao gio la nguon tieng video, nen khong co
    ly do gi de dung toi chung.
    """
    devices = [item for item in list_devices()
               if (item["loopback"] or not loopback_only)
               and not is_forbidden_device(item)]
    results = []
    captures = []
    for device in devices:
        try:
            capture = RollingCapture(device["id"], seconds, rate=rate)
            capture.__enter__()
            captures.append((device, capture))
        except Exception as exc:
            results.append({**device, "samples": None,
                            "error": f"{type(exc).__name__}: {exc}"})
    import time as _time
    _time.sleep(seconds + 0.6)
    for device, capture in captures:
        block = capture.latest()
        error = capture.error or ("" if block is not None else "khong du du lieu")
        capture.__exit__()
        results.append({**device, "samples": block, "error": error})
    order = {item["id"]: index for index, item in enumerate(devices)}
    results.sort(key=lambda item: order.get(item["id"], 99))
    return results


class RollingCapture:
    """Giữ lại N giây âm thanh mới nhất từ thiết bị đã chọn."""

    def __init__(self, selector, window_s, rate=48000):
        if sc is None:
            raise RuntimeError("soundcard_unavailable")
        # ``get_microphone`` cung dung WASAPI ngay trong luong khoi tao.
        # resolve_device() chi giu COM trong luc liet ke, nen phai giu them
        # mot reference COM cho den khi tao xong wrapper microphone.
        com = _initialize_audio_thread()
        try:
            self.device = resolve_device(selector)
            self.name = self.device["name"]
            self.loopback = self.device["loopback"]
            self._microphone = sc.get_microphone(
                self.device["id"], include_loopback=True,
            )
        finally:
            _uninitialize_audio_thread(com)
        self.rate = int(rate)
        self.window_s = float(window_s)
        self._need = int(self.rate * self.window_s)
        self._buffer = deque(maxlen=self._need + self.rate)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._pump, daemon=True)

    def _pump(self):
        com = None
        try:
            com = _initialize_audio_thread()
            with self._microphone.recorder(
                samplerate=self.rate, channels=1, blocksize=2048,
            ) as recorder:
                while not self._stop.is_set():
                    block = recorder.record(numframes=4096)
                    mono = np.asarray(block, dtype=np.float32).reshape(-1)
                    with self._lock:
                        self._buffer.extend(mono)
        except Exception as exc:  # thiết bị bị chiếm, rút dây, đổi tần số...
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            _uninitialize_audio_thread(com)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=3)

    @property
    def error(self):
        return self._error

    def latest(self):
        with self._lock:
            if len(self._buffer) < self._need:
                return None
            data = np.fromiter(self._buffer, dtype=np.float32, count=len(self._buffer))
        return data[-self._need:]


# ---------------------------------------------------------------- Chẩn đoán
# Microsoft hỗ trợ process loopback (AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK)
# từ Windows 10 build 19041 (20H1). process_audio.py ghi 20348 trong docstring vì
# đó là máy thử đầu tiên; mốc tối thiểu chính thức là 19041.
PROCESS_LOOPBACK_MIN_BUILD = 19041
AUDIO_SERVICES = ("Audiosrv", "AudioEndpointBuilder")
_MIC_CONSENT_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                    r"\CapabilityAccessManager\ConsentStore\microphone")


def _windows_build():
    try:
        return int(sys.getwindowsversion().build) if os.name == "nt" else None
    except (AttributeError, OSError, ValueError):
        return None


def _service_state(name):
    try:
        import psutil
        return str(psutil.win_service_get(name).status())
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            if "STATE" in line:
                return "running" if "RUNNING" in line else "stopped" if "STOPPED" in line else line.split()[-1].lower()
    except Exception:
        pass
    return None


def _audio_services():
    return {name: _service_state(name) for name in AUDIO_SERVICES}


def _mic_privacy():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_CONSENT_KEY) as key:
            return str(winreg.QueryValueEx(key, "Value")[0])
    except Exception:
        return None


def _default_speaker_name():
    if sc is None:
        return None
    try:
        speaker = sc.default_speaker()
        return getattr(speaker, "name", None) if speaker is not None else None
    except Exception:
        return None


def _error_detail(exc):
    code = getattr(exc, "hresult", None) or getattr(exc, "winerror", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    detail = type(exc).__name__
    if isinstance(code, int):
        detail += f" 0x{code & 0xFFFFFFFF:08x}"
    return detail


def enumerate_report():
    """Liệt kê thiết bị kèm lý do cụ thể khi không thấy gì (không bao giờ ném lỗi)."""
    build = _windows_build()
    report = {"devices": [], "error_code": "ok", "error_detail": "",
              "soundcard_ok": sc is not None, "windows_build": build,
              "process_loopback_min_build": PROCESS_LOOPBACK_MIN_BUILD,
              "process_loopback_supported": build is None or build >= PROCESS_LOOPBACK_MIN_BUILD,
              "audio_service": _audio_services(), "default_speaker_name": None,
              "mic_privacy": _mic_privacy()}
    if sc is None:
        report["error_code"] = "soundcard_unavailable"
        report["error_detail"] = "soundcard import failed"
        return report
    report["default_speaker_name"] = _default_speaker_name()
    try:
        report["devices"] = list_devices()
    except Exception as exc:
        report["error_code"] = "enumeration_failed"
        report["error_detail"] = _error_detail(exc)
    stopped = [name for name, state in report["audio_service"].items()
               if state is not None and state != "running"]
    if stopped and not report["devices"]:
        report["error_code"] = "audio_service_stopped"
        report["error_detail"] = ",".join(stopped)
    elif report["error_code"] == "ok" and not any(item.get("loopback") for item in report["devices"]):
        report["error_code"] = "no_active_output"
    return report
