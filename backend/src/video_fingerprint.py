"""Dấu vân tay âm thanh để biết video phát lại đang ở giây thứ mấy.

Đặc trưng phổ đã được đo trên chính `live1.mp4` (lịch sử: benchmark 09/2026):
sai số 0 ms ở mọi điều kiện sạch/nén/nhiễu/mất gói.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

HOP_S = 0.10
WINDOW_S = 0.20
BANDS = 18
TARGET_RATE = 16000


def read_pcm16(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("cần WAV PCM16")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    if channels > 1:
        raw = raw.reshape(-1, channels).mean(axis=1)
    return raw.astype(np.float32) / 32768.0, rate


def resample_mono(samples, rate, target=TARGET_RATE):
    """Đổi tần số lấy mẫu bằng nội suy tuyến tính.

    Thiết bị WASAPI thường chạy 48kHz còn bản tham chiếu là 16kHz; đặc trưng
    phổ chỉ so được khi hai bên cùng một tần số.
    """
    samples = np.asarray(samples, dtype=np.float32).ravel()
    if rate == target or len(samples) == 0:
        return samples, target
    count = int(round(len(samples) * target / rate))
    if count <= 1:
        return samples, target
    source_x = np.arange(len(samples), dtype=np.float64)
    target_x = np.linspace(0, len(samples) - 1, count)
    return np.interp(target_x, source_x, samples).astype(np.float32), target


def _feature_plan(rate, hop_s, window_s, bands):
    hop = max(1, round(rate * hop_s))
    size = max(hop, round(rate * window_s))
    window = np.hanning(size).astype(np.float32)
    frequencies = np.fft.rfftfreq(size, 1.0 / rate)
    edges = np.geomspace(90.0, min(7200.0, rate * 0.47), bands + 1)
    masks = [(frequencies >= edges[i]) & (frequencies < edges[i + 1])
             for i in range(bands)]
    return hop, size, window, masks


def _raw_frames(samples, hop, size, window, masks, output, start=0):
    """Ghi dac trung tho (chua chuan hoa) cua moi khung day du trong `samples`."""
    count = max(0, 1 + (len(samples) - size) // hop) if len(samples) >= size else 0
    for index in range(count):
        frame = samples[index * hop:index * hop + size] * window
        power = np.abs(np.fft.rfft(frame)) ** 2
        output[start + index] = [math.log1p(float(power[mask].sum())) for mask in masks]
    return count


def _normalize(output):
    # Chuan hoa TUNG khung doc lap -> tinh theo khoi cho ket qua y het.
    output -= output.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(output, axis=1, keepdims=True)
    output /= np.maximum(norms, 1e-7)
    return output


def spectral_features(samples, rate, hop_s=HOP_S, window_s=WINDOW_S, bands=BANDS):
    hop, size, window, masks = _feature_plan(rate, hop_s, window_s, bands)
    if len(samples) < size:
        return np.empty((0, bands), dtype=np.float32)
    count = max(0, 1 + (len(samples) - size) // hop)
    output = np.empty((count, bands), dtype=np.float32)
    _raw_frames(samples, hop, size, window, masks, output)
    return _normalize(output)


def spectral_features_wav(path, block_frames=60 * TARGET_RATE):
    """Nhu read_pcm16 + resample_mono + spectral_features nhung doc WAV theo khoi.

    Ket qua giong het (tung khung chi phu thuoc mau cua chinh no; doi kieu va
    tron kenh la phep tinh tung mau). RAM ~ mot khoi (mac dinh 60 giay) thay vi
    ca file. WAV khong phai TARGET_RATE can noi suy toan cuc -> giu duong cu.
    Tra ve (features, duration_s).
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("cần WAV PCM16")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        total = handle.getnframes()
        if rate != TARGET_RATE:
            handle.close()
            samples, rate = read_pcm16(path)
            samples, rate = resample_mono(samples, rate)
            return spectral_features(samples, rate), len(samples) / rate
        hop, size, window, masks = _feature_plan(rate, HOP_S, WINDOW_S, BANDS)
        count = max(0, 1 + (total - size) // hop) if total >= size else 0
        output = np.empty((count, BANDS), dtype=np.float32)
        block_frames = max(int(block_frames), size)
        pending = np.empty(0, dtype=np.float32)
        consumed = 0  # chi so mau (toan cuc) cua pending[0]
        done = 0
        while True:
            raw = np.frombuffer(handle.readframes(block_frames), dtype="<i2")
            if raw.size == 0:
                break
            if channels > 1:
                raw = raw.reshape(-1, channels).mean(axis=1)
            chunk = raw.astype(np.float32) / 32768.0
            pending = np.concatenate([pending, chunk])
            # pending[0] luon la mau dau cua khung chua tinh tiep theo.
            made = _raw_frames(pending, hop, size, window, masks, output, done)
            done += made
            drop = done * hop - consumed
            pending = pending[drop:]
            consumed += drop
    return _normalize(output[:done]), total / rate


def locate(reference, query, hop_s=HOP_S):
    """Trả về (vị trí bắt đầu của cửa sổ, điểm khớp, khoảng cách với đỉnh nhì).

    `margin` là thứ đáng tin hơn `score`: một đoạn nhạc nền lặp lại có thể cho
    điểm cao ở nhiều chỗ, và khi đó margin sẽ nhỏ — đúng lúc không nên tin.
    """
    if len(query) == 0 or len(query) > len(reference):
        return None
    scores = np.zeros(len(reference) - len(query) + 1, dtype=np.float32)
    for column in range(reference.shape[1]):
        scores += np.correlate(reference[:, column], query[:, column], mode="valid")
    scores /= max(1, len(query))
    best = int(np.argmax(scores))
    exclusion = max(1, round(3.0 / hop_s))
    masked = scores.copy()
    masked[max(0, best - exclusion):best + exclusion + 1] = -2.0
    second = float(masked.max()) if masked.size else -1.0
    return best * hop_s, float(scores[best]), float(scores[best] - second)


def save_reference(path, features, duration_s, source):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path, features=features, hop_s=HOP_S, window_s=WINDOW_S,
        bands=BANDS, rate=TARGET_RATE, duration_s=float(duration_s),
        source=str(source),
    )


def load_reference(path):
    data = np.load(Path(path), allow_pickle=False)
    return {
        "features": data["features"],
        "hop_s": float(data["hop_s"]),
        "duration_s": float(data["duration_s"]),
        "rate": int(data["rate"]),
        "source": str(data["source"]),
    }
