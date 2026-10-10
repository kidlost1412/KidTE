"""Giao thức tải video lên để phân tích: chunk, tiếp nối, chống nhân đôi byte.

Tách từ bộ test_e2e_modernization cũ — chỉ giữ các test gọi THẬT vào
VideoAnalysisManager. Phần còn lại của bộ cũ chỉ grep chuỗi trong mã giao diện
(khẳng định cả những hành vi sai như ô kéo-thả giả) nên đã bỏ cùng giao diện cũ.
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from video_analysis import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    MAX_CHUNK_BYTES,
    VideoAnalysisManager,
)


def mock_catalog():
    return {
        "synced_at": "2026-09-20T00:00:00Z",
        "products": [
            {"product_id": "sku-tc01", "bag_index": 1, "title": "Phân bón rễ TC01", "profile_id": "p-01"},
            {"product_id": "sku-tc02", "bag_index": 2, "title": "Thuốc trừ sâu sinh học", "profile_id": ""},
            {"product_id": "sku-tc03", "bag_index": 3, "title": "Kích chồi lá mầm", "profile_id": ""},
        ]
    }


class TestVideoUploadProtocol:
    def test_tier1_f12_video_file_extensions_and_validation(self):
            """F12: Verifies backend video analysis accepts standard video extensions and rejects others."""
            assert ".mp4" in ALLOWED_EXTENSIONS
            assert ".mov" in ALLOWED_EXTENSIONS
            assert ".webm" in ALLOWED_EXTENSIONS
            assert ".exe" not in ALLOWED_EXTENSIONS
            assert ".pdf" not in ALLOWED_EXTENSIONS

    def test_tier1_f12_chunked_upload_start_protocol(self):
            """F12: Verifies VideoAnalysisManager.start_upload initializes job metadata."""
            with tempfile.TemporaryDirectory() as tmp:
                mgr = VideoAnalysisManager(root=Path(tmp), catalog_provider=mock_catalog)
                state = mgr.start_upload("demo.mp4", 1024 * 1024)
                assert state["ok"] is True
                assert state["state"] == "uploading"
                assert state["total_size"] == 1024 * 1024
                assert state["uploaded_size"] == 0

    def test_tier1_f13_upload_chunk_append_and_progress(self):
            """F13: Verifies append_chunk writes chunk to disk and updates progress."""
            with tempfile.TemporaryDirectory() as tmp:
                mgr = VideoAnalysisManager(root=Path(tmp), catalog_provider=mock_catalog)
                state = mgr.start_upload("demo.mp4", 4096)
                job_id = state["job_id"]
                data = b"x" * 2048
                res = mgr.append_chunk(job_id, 0, io.BytesIO(data), len(data))
                assert res["uploaded_size"] == 2048
                assert res["progress"] == 50

    def test_tier1_f13_upload_pause_and_resumable_verification(self):
            """F13: Verifies resumable_job verifies content fingerprint to allow resuming."""
            with tempfile.TemporaryDirectory() as tmp:
                mgr = VideoAnalysisManager(root=Path(tmp), catalog_provider=mock_catalog)
                probe = "a" * 64
                state = mgr.start_upload("demo.mp4", 4096, probe)
                job_id = state["job_id"]
                mgr.append_chunk(job_id, 0, io.BytesIO(b"x" * 2048), 2048)

                # Query resumable job
                resumable = mgr.resumable_job("demo.mp4", 4096, probe)
                assert resumable is not None
                assert resumable["job_id"] == job_id
                assert resumable["uploaded_size"] == 2048

    def test_tier2_b13_upload_chunk_size_exceeding_limit(self):
            """B13: Verifies VideoAnalysisManager rejects chunk exceeding MAX_CHUNK_BYTES."""
            with tempfile.TemporaryDirectory() as tmp:
                mgr = VideoAnalysisManager(root=Path(tmp), catalog_provider=mock_catalog)
                state = mgr.start_upload("demo.mp4", 100 * 1024 * 1024)
                job_id = state["job_id"]
                large_size = MAX_CHUNK_BYTES + 1024
                with pytest.raises(ValueError, match="(chunk_too_large|invalid_chunk_size)"):
                    mgr.append_chunk(job_id, 0, io.BytesIO(b""), large_size)

    def test_tier2_b14_upload_unsupported_file_extension(self):
            """B14: Verifies VideoAnalysisManager rejects unsupported file extension."""
            with tempfile.TemporaryDirectory() as tmp:
                mgr = VideoAnalysisManager(root=Path(tmp), catalog_provider=mock_catalog)
                with pytest.raises(ValueError, match="video_extension_not_supported"):
                    mgr.start_upload("virus.exe", 1024)
