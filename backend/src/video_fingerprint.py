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


def spectral_features(samples, rate, hop_s=HOP_S, window_s=WINDOW_S, bands=BANDS):
    hop = max(1, round(rate * hop_s))
    size = max(hop, round(rate * window_s))
    if len(samples) < size:
        return np.empty((0, bands), dtype=np.float32)
    window = np.hanning(size).astype(np.float32)
    frequencies = np.fft.rfftfreq(size, 1.0 / rate)
    edges = np.geomspace(90.0, min(7200.0, rate * 0.47), bands + 1)
    masks = [(frequencies >= edges[i]) & (frequencies < edges[i + 1])
             for i in range(bands)]
    count = max(0, 1 + (len(samples) - size) // hop)
    output = np.empty((count, bands), dtype=np.float32)
    for index in range(count):
        frame = samples[index * hop:index * hop + size] * window
        power = np.abs(np.fft.rfft(frame)) ** 2
        output[index] = [math.log1p(float(power[mask].sum())) for mask in masks]
    output -= output.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(output, axis=1, keepdims=True)
    output /= np.maximum(norms, 1e-7)
    return output


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
