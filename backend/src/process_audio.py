"""WASAPI process-tree loopback. Captures one app, never the desktop mix.

Uses ActivateAudioInterfaceAsync with AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK.
Requires Windows build 20348+. No additional driver or compiled helper.
Reference: Microsoft Windows-classic-samples/Samples/ApplicationLoopback.
"""
from __future__ import annotations

import ctypes as ct
import os
import threading
import uuid
from collections import deque

import numpy as np

_ACTIVATIONS = {}  # Keep callbacks alive even if activation times out.


def studio_process(name="TikTok LIVE Studio.exe"):
    import psutil
    aliases = {name.casefold()}
    if name.casefold() == "tiktok live studio.exe":
        aliases.update({"tiktok live studio.exe", "tiktok live studio", "tiktokstudio.exe"})
    matches = []
    for process in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        if (process.info.get("name") or "").casefold() in aliases:
            matches.append(process.info)
    roots = [item for item in matches if item["ppid"] not in {other["pid"] for other in matches}]
    if len(roots) != 1:
        return None
    return roots[0]


class ProcessCapture:
    def __init__(self, process_id, seconds=8):
        self.process_id = int(process_id)
        self.seconds = float(seconds)
        self.rate = 44100
        self.name = f"TikTok Studio · PID {process_id}"
        self.loopback = True
        self.error = ""
        self._chunks = deque()
        self._frames = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("process_audio_windows_only")
        if self.process_id <= 0:
            raise ValueError("invalid_process_id")
        self._thread = threading.Thread(target=self._run, daemon=True, name="tiktok-process-audio")
        self._thread.start()
        if not self._ready.wait(12):
            self.close()
            raise RuntimeError("process_audio_activation_timeout")
        if self.error:
            self.close()
            raise RuntimeError(self.error)
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def latest(self):
        with self._lock:
            if self._frames < self.seconds * self.rate:
                return None
            return np.concatenate(list(self._chunks))[-int(self.seconds * self.rate):].copy()

    def _run(self):
        from audio_capture import _initialize_audio_thread, _uninitialize_audio_thread
        com = None
        client = ct.c_void_p()
        capture = ct.c_void_p()
        operation = ct.c_void_p()
        callback_refs = []
        token = id(self)
        try:
            com = _initialize_audio_thread()
            HRESULT = ct.c_int32
            ULONG = ct.c_uint32
            PTR = ct.c_void_p
            CALL = ct.WINFUNCTYPE
            def guid(text):
                return (ct.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)
            def method(pointer, slot, restype, *types):
                table = ct.cast(pointer, ct.POINTER(ct.POINTER(PTR))).contents
                return CALL(restype, PTR, *types)(table[slot])
            def checked(result):
                if result < 0:
                    raise RuntimeError(f"process_audio_hresult:0x{result & 0xffffffff:08x}")
            audio_iid = guid("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
            capture_iid = guid("c8adbd64-e71e-48a0-a4de-185c395cd317")
            supported = {uuid.UUID(value).bytes_le for value in (
                "00000000-0000-0000-c000-000000000046",
                "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90",
                "41d949ab-9862-444a-80f6-c261334da5eb")}
            activated = threading.Event()
            activation_error = []
            instance = (PTR * 1)()
            @CALL(HRESULT, PTR, PTR, ct.POINTER(PTR))
            def query(this, iid, output):
                if ct.string_at(iid, 16) in supported:
                    output[0] = ct.addressof(instance)
                    return 0
                output[0] = None
                return -2147467262
            @CALL(ULONG, PTR)
            def addref(this):
                return 2
            @CALL(ULONG, PTR)
            def release(this):
                return 1
            @CALL(HRESULT, PTR, PTR)
            def completed(this, op):
                unknown = PTR()
                try:
                    status = HRESULT()
                    checked(method(op, 3, HRESULT, ct.POINTER(HRESULT), ct.POINTER(PTR))(op, ct.byref(status), ct.byref(unknown)))
                    checked(status.value)
                    checked(method(unknown, 0, HRESULT, PTR, ct.POINTER(PTR))(unknown, ct.byref(audio_iid), ct.byref(client)))
                except Exception as exc:
                    activation_error.append(str(exc))
                finally:
                    if unknown:
                        method(unknown, 2, ULONG)(unknown)
                    activated.set()
                return 0
            vtable = (PTR * 4)(*[ct.cast(fn, PTR).value for fn in (query, addref, release, completed)])
            instance[0] = ct.addressof(vtable)
            callback_refs.extend([query, addref, release, completed, vtable, instance])
            params = (ULONG * 3)(1, self.process_id, 0)  # INCLUDE_TARGET_PROCESS_TREE
            class Blob(ct.Structure):
                _fields_ = [("size", ULONG), ("data", PTR)]
            class Variant(ct.Structure):
                _fields_ = [("vt", ct.c_uint16), ("reserved", ct.c_uint16 * 3), ("blob", Blob)]
            variant = Variant(65, (ct.c_uint16 * 3)(), Blob(ct.sizeof(params), ct.addressof(params)))
            callback_refs.extend([params, variant])
            _ACTIVATIONS[token] = callback_refs
            activate = ct.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
            activate.argtypes = [ct.c_wchar_p, PTR, PTR, PTR, ct.POINTER(PTR)]
            activate.restype = HRESULT
            checked(activate("VAD\\Process_Loopback", ct.byref(audio_iid), ct.byref(variant),
                             ct.addressof(instance), ct.byref(operation)))
            if not activated.wait(10):
                raise RuntimeError("process_audio_activation_timeout")
            if activation_error:
                raise RuntimeError(activation_error[0])
            class WaveFormat(ct.Structure):
                _pack_ = 2
                _fields_ = [("tag", ct.c_uint16), ("channels", ct.c_uint16), ("rate", ULONG),
                            ("bytes_per_second", ULONG), ("align", ct.c_uint16),
                            ("bits", ct.c_uint16), ("extra", ct.c_uint16)]
            fmt = WaveFormat(1, 2, self.rate, self.rate * 4, 4, 16, 0)
            checked(method(client, 3, HRESULT, ct.c_int, ULONG, ct.c_int64, ct.c_int64, PTR, PTR)(
                client, 0, 0x80020000, 0, 0, ct.byref(fmt), None))
            checked(method(client, 14, HRESULT, PTR, ct.POINTER(PTR))(client, ct.byref(capture_iid), ct.byref(capture)))
            checked(method(client, 10, HRESULT)(client))
            self._ready.set()
            while not self._stop.wait(0.02):
                available = ULONG()
                checked(method(capture, 5, HRESULT, ct.POINTER(ULONG))(capture, ct.byref(available)))
                while available.value:
                    data, frames, flags = PTR(), ULONG(), ULONG()
                    checked(method(capture, 3, HRESULT, ct.POINTER(PTR), ct.POINTER(ULONG), ct.POINTER(ULONG), PTR, PTR)(
                        capture, ct.byref(data), ct.byref(frames), ct.byref(flags), None, None))
                    try:
                        samples = np.zeros(frames.value, dtype=np.float32) if flags.value & 2 else (
                            np.frombuffer(ct.string_at(data, frames.value * 4), dtype="<i2")
                            .reshape(-1, 2).astype(np.float32).mean(axis=1) / 32768)
                        with self._lock:
                            self._chunks.append(samples)
                            self._frames += len(samples)
                            while self._chunks and self._frames - len(self._chunks[0]) >= self.seconds * self.rate:
                                self._frames -= len(self._chunks.popleft())
                    finally:
                        checked(method(capture, 4, HRESULT, ULONG)(capture, frames.value))
                    checked(method(capture, 5, HRESULT, ct.POINTER(ULONG))(capture, ct.byref(available)))
            checked(method(client, 11, HRESULT)(client))
        except Exception as exc:
            self.error = str(exc)
            self._ready.set()
        finally:
            for pointer in (capture, client, operation):
                if pointer:
                    method(pointer, 2, ct.c_uint32)(pointer)
            if 'activated' in locals() and activated.is_set():
                _ACTIVATIONS.pop(token, None)
            _uninitialize_audio_thread(com)
