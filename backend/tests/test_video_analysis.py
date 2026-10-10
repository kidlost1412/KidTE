import io
import hashlib
import json
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import video_analysis


CATALOG = {
    "synced_at": "2026-09-13T21:50:40+07:00",
    "room_id": "room-1",
    "products": [
        {"product_id": "p1", "bag_index": 1, "title": "Kích Rễ Cực Mạnh TC01 100ml",
         "profile_name": "TC01"},
        {"product_id": "p2", "bag_index": 2, "title": "Đạm Cá Amino 1L",
         "profile_name": "Đạm cá 1L"},
    ],
}


def manager(tmp_path):
    return video_analysis.VideoAnalysisManager(
        tmp_path / "uploads", catalog_provider=lambda: CATALOG,
    )


def reviewed_draft(service, job_id, events):
    """Persist the exact reviewed artifact required by the publish boundary."""
    rows = [{**row, "review_status": row.get("review_status", "approved"),
             "context_before": row.get("context_before", ""),
             "context_after": row.get("context_after", ""),
             "alternate_product_id": row.get("alternate_product_id", ""),
             "alternate_every_ms": row.get("alternate_every_ms", 0)} for row in events]
    draft = {"events": rows}
    (service._dir(job_id) / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    normalized = json.loads(json.dumps(draft))
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def test_chunked_upload_is_ordered_and_durable(tmp_path):
    service = manager(tmp_path)
    state = service.start_upload("new-live.mp4", 6)
    job_id = state["job_id"]
    first = service.append_chunk(job_id, 0, io.BytesIO(b"abc"), 3)
    assert first["uploaded_size"] == 3
    with pytest.raises(ValueError, match="offset"):
        service.append_chunk(job_id, 0, io.BytesIO(b"bad"), 3)
    service.append_chunk(job_id, 3, io.BytesIO(b"def"), 3)
    service._analyze = lambda value: service._write(value, state="review", duration_s=10)
    finished = service.finish_upload(job_id, "Video mới")
    service._threads[job_id].join(timeout=2)
    assert finished["state"] == "queued"
    assert (service._dir(job_id) / "new-live.mp4").read_bytes() == b"abcdef"
    assert service.status(job_id)["state"] == "review"


def test_preflight_uses_saved_catalog_without_a_live_page(tmp_path):
    result = manager(tmp_path).preflight()
    assert result["catalog_ready"] is True
    assert result["can_analyze_offline"] is True
    assert result["product_count"] == 2
    assert result["room_id"] == "room-1"


def test_new_analysis_is_allowed_while_live_with_single_job_guard(tmp_path):
    service = video_analysis.VideoAnalysisManager(
        tmp_path / "uploads", catalog_provider=lambda: CATALOG,
        live_provider=lambda: True,
    )
    preflight = service.preflight()
    assert preflight["can_analyze_offline"] is True
    assert preflight["live_running"] is True
    assert preflight["max_parallel_jobs"] == 1
    assert service.start_upload("new-live.mp4", 10)["state"] == "uploading"


def test_transcribe_passes_normalized_audio_array_instead_of_frozen_file_path(
        tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes((np.zeros(1600, dtype="<i2")).tobytes())
    received = {}

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            received["audio"] = audio
            info = types.SimpleNamespace(language="vi", language_probability=0.99)
            return [types.SimpleNamespace(start=0, end=0.1, text=" thử")], info

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(
        WhisperModel=FakeModel,
    ))
    monkeypatch.setattr(video_analysis, "_find_whisper_model", lambda: "fake")

    rows, _ = video_analysis._transcribe(wav)

    assert isinstance(received["audio"], np.ndarray)
    assert received["audio"].dtype == np.float32
    assert rows[0]["text"] == "thử"


def test_cuda_runtime_is_discovered_next_to_workspace_venv(tmp_path, monkeypatch):
    executable = tmp_path / "dist" / "KidTE" / "KidTE.exe"
    executable.parent.mkdir(parents=True)
    for package in ("cublas", "cudnn"):
        (tmp_path / "backend" / ".venv" / "Lib" / "site-packages" /
         "nvidia" / package / "bin").mkdir(parents=True)
    registered = []

    monkeypatch.setattr(video_analysis.os, "name", "nt")
    monkeypatch.setattr(video_analysis.sys, "executable", str(executable))
    monkeypatch.setattr(video_analysis.os, "add_dll_directory",
                        lambda path: registered.append(Path(path)) or object())
    video_analysis._CUDA_DLL_HANDLES.clear()

    assert video_analysis._register_cuda_dll_dirs() is True
    assert {path.parent.name for path in registered} == {"cublas", "cudnn"}
    assert len(video_analysis._CUDA_DLL_HANDLES) == 2


def test_upload_requires_a_saved_catalog(tmp_path):
    service = video_analysis.VideoAnalysisManager(
        tmp_path / "uploads", catalog_provider=lambda: {},
    )
    with pytest.raises(ValueError, match="saved_catalog_required"):
        service.start_upload("new-live.mp4", 10)


def test_transcript_suggestions_are_conservative_and_keep_evidence():
    products = video_analysis._catalog_rows(CATALOG)
    rows = video_analysis._propose_segments([
        {"start": 12, "end": 18, "text": "Bây giờ em giới thiệu TC01 kích rễ"},
        {"start": 19, "end": 25, "text": "TC01 dùng cho bộ rễ"},
    ], products)
    assert len(rows) == 1
    assert rows[0]["product_id"] == "p1"
    # Máy giữ đúng bằng chứng; không được tự kéo một dự đoán về đầu video.
    assert rows[0]["at_ms"] == 12000
    assert "TC01" in rows[0]["evidence"]


def test_generic_speech_does_not_match_long_seo_title():
    snapshot = {"products": [{
        "product_id": "magie", "bag_index": 14,
        "title": "Phân bón Magie Kẽm - Bổ sung trung vi lượng, GA3 cho tất cả "
                 "các loại cây trồng, mọi giai đoạn, pha 2 bình",
    }]}
    rows = video_analysis._propose_segments([{
        "start": 10, "end": 15,
        "text": "Cả nhà pha hai bình cho cây ở giai đoạn ra bông nha",
    }], video_analysis._catalog_rows(snapshot))
    assert rows == []


def test_each_product_builds_phrases_from_its_own_title():
    products = video_analysis._catalog_rows({"products": [
        {"product_id": "fish", "bag_index": 1,
         "title": "Amino Fish Premium 1L"},
        {"product_id": "humic", "bag_index": 2,
         "title": "Humic Gold Premium 5L"},
        {"product_id": "tc01", "bag_index": 3,
         "title": "Roots Master TC01 100ml"},
    ]})

    anchors = video_analysis._product_anchors(products)

    assert "amino fish" in anchors["fish"]["phrases"]
    assert "humic gold" in anchors["humic"]["phrases"]
    assert "roots master" in anchors["tc01"]["phrases"]


def test_combo_block_longer_than_two_minutes_alternates_with_humic_5l():
    snapshot = {"products": [
        {"product_id": "combo", "bag_index": 11,
         "title": "Combo Đạm Cá + Humic Nông Dược Toàn Cầu"},
        {"product_id": "humic-5l", "bag_index": 15,
         "title": "Humic Can 5 Lít Nông Dược Toàn Cầu"},
    ]}
    segments = [
        {"start": second, "end": second + 5, "text": "combo đạm cá humic"}
        for second in (0, 40, 80, 120, 160)
    ]

    rows = video_analysis._propose_segments(
        segments, video_analysis._catalog_rows(snapshot))

    assert rows[0]["product_id"] == "combo"
    assert rows[0]["alternate_product_id"] == "humic-5l"
    assert rows[0]["alternate_every_ms"] == 60000


def test_publish_uses_exact_product_ids_and_valid_timeline(tmp_path, monkeypatch):
    service = manager(tmp_path)
    state = service.start_upload("new-live.mp4", 1)
    job_id = state["job_id"]
    folder = service._dir(job_id)
    (folder / "fingerprint.npz").write_bytes(b"fingerprint")
    service._write(job_id, state="review", duration_s=120.0, title="Video mới")
    library = tmp_path / "videos"
    monkeypatch.setattr(video_analysis, "VIDEO_DIR", library)

    reviewed = [
        {"at_ms": 0, "product_id": "p1"},
        {"at_ms": 60000, "product_id": "p2"},
    ]
    revision = reviewed_draft(service, job_id, reviewed)
    result = service.publish(job_id, "Live 3", reviewed, operation_id="operation-publish-0001",
                             expected_revision=revision)

    timeline = json.loads((library / result["video_id"] / "timeline.json").read_text("utf-8"))
    assert result["ok"] is True
    assert timeline["products"]["sku_p1"]["product_id"] == "p1"
    assert timeline["events"] == [
        {"at_ms": 0, "product": "sku_p1"},
        {"at_ms": 60000, "product": "sku_p2"},
    ]
    assert video_analysis.validate_timeline(timeline) == []


def test_catalog_label_uses_real_listing_not_stale_profile():
    snapshot = {"products": [{
        "product_id": "fish-1l", "bag_index": 2,
        "title": "Đạm Cá Amino Chai 1L",
        "profile_name": "Đạm Cá Hữu Cơ Can 5 Lít",
    }]}
    row = video_analysis._catalog_rows(snapshot)[0]
    assert row["short_name"] == "Đạm Cá Amino Chai 1L"
    assert row["profile_name"] == "Đạm Cá Hữu Cơ Can 5 Lít"


def test_publish_supports_reviewed_product_alternation(tmp_path, monkeypatch):
    service = manager(tmp_path)
    state = service.start_upload("combo.mp4", 1)
    job_id = state["job_id"]
    folder = service._dir(job_id)
    (folder / "fingerprint.npz").write_bytes(b"fingerprint")
    service._write(job_id, state="review", duration_s=180.0, title="Combo")
    library = tmp_path / "videos"
    monkeypatch.setattr(video_analysis, "VIDEO_DIR", library)

    reviewed = [{
        "at_ms": 0, "product_id": "p1", "alternate_product_id": "p2",
        "alternate_every_ms": 60000,
    }]
    result = service.publish(job_id, "Combo", reviewed, operation_id="operation-publish-0002",
                             expected_revision=reviewed_draft(service, job_id, reviewed))

    timeline = json.loads((library / result["video_id"] / "timeline.json").read_text("utf-8"))
    assert timeline["events"] == [{
        "at_ms": 0, "product": "sku_p1", "alternate_with": "sku_p2",
        "alternate_every_ms": 60000,
    }]
    assert set(timeline["products"]) == {"sku_p1", "sku_p2"}
    assert video_analysis.validate_timeline(timeline) == []


def test_publish_moves_first_suggestion_to_zero_and_says_so(tmp_path, monkeypatch):
    """Moc dau duoc dua ve 0 — nhung KHONG BAO GIO am tham.

    Ban cu nem `first_event_must_start_at_zero`, nen khi doan noi dau tien bat
    dau sau giay 0 thi chu shop duyet het moc roi VAN khong phat hanh duoc:
    mot ngo cut o dung buoc cuoi.

    Dua ve 0 khong doi hanh vi ghim — `eventAt()` trong schedule.js tra
    `events[0]` cho moi vi tri nho hon moc dau, nen san pham do VON DA duoc
    ghim tu giay 0. Y nghia nghiep vu cua test nay ("khong am tham") duoc giu
    nguyen: ket qua phai noi ro da dich moc nao, tu bao nhieu ve 0.
    """
    service = manager(tmp_path)
    state = service.start_upload("late.mp4", 1)
    job_id = state["job_id"]
    folder = service._dir(job_id)
    (folder / "fingerprint.npz").write_bytes(b"fingerprint")
    service._write(job_id, state="review", duration_s=60.0)
    library = tmp_path / "videos"
    monkeypatch.setattr(video_analysis, "VIDEO_DIR", library)

    reviewed = [{
        "at_ms": 12000, "product_id": "p1",
    }]
    ket = service.publish(job_id, "Late", reviewed, operation_id="operation-publish-0003",
                          expected_revision=reviewed_draft(service, job_id, reviewed))

    bao = ket["da_chuan_hoa_moc_dau"]
    assert bao is not None, "phai bao ro da dich moc dau, khong duoc am tham"
    assert bao["tu_ms"] == 12000 and bao["ve_ms"] == 0
    assert bao["product"] == "sku_p1"

    timeline = json.loads((library / ket["video_id"] / "timeline.json").read_text("utf-8"))
    assert timeline["events"][0]["at_ms"] == 0
    assert timeline["events"][0]["product"] == "sku_p1", "san pham ghim khong doi"
    assert video_analysis.validate_timeline(timeline) == []


def test_publish_khong_bao_gi_khi_moc_dau_da_o_khong(tmp_path, monkeypatch):
    """Khong co gi bi dich thi khong duoc bao nham la da dich."""
    service = manager(tmp_path)
    state = service.start_upload("ontime.mp4", 1)
    job_id = state["job_id"]
    (service._dir(job_id) / "fingerprint.npz").write_bytes(b"fingerprint")
    service._write(job_id, state="review", duration_s=60.0)
    monkeypatch.setattr(video_analysis, "VIDEO_DIR", tmp_path / "videos")
    reviewed = [{
        "at_ms": 0, "product_id": "p1",
    }]
    ket = service.publish(job_id, "On time", reviewed, operation_id="operation-publish-0004",
                          expected_revision=reviewed_draft(service, job_id, reviewed))
    assert ket["da_chuan_hoa_moc_dau"] is None


def test_publish_supports_explicit_unpinned_sections(tmp_path, monkeypatch):
    service = manager(tmp_path)
    state = service.start_upload("gaps.mp4", 1)
    job_id = state["job_id"]
    folder = service._dir(job_id)
    (folder / "fingerprint.npz").write_bytes(b"fingerprint")
    service._write(job_id, state="review", duration_s=90.0)
    library = tmp_path / "videos"
    monkeypatch.setattr(video_analysis, "VIDEO_DIR", library)
    reviewed = [
        {"at_ms": 0, "product_id": ""},
        {"at_ms": 15000, "product_id": "p1"},
        {"at_ms": 60000, "product_id": ""},
    ]
    result = service.publish(job_id, "Gaps", reviewed, operation_id="operation-publish-0005",
                             expected_revision=reviewed_draft(service, job_id, reviewed))
    timeline = json.loads((library / result["video_id"] / "timeline.json").read_text("utf-8"))
    assert [row["product"] for row in timeline["events"]] == [None, "sku_p1", None]
    assert video_analysis.validate_timeline(timeline) == []


def test_restart_marks_running_analysis_resumable(tmp_path):
    first = manager(tmp_path)
    state = first.start_upload("recover.mp4", 3)
    job_id = state["job_id"]
    first.append_chunk(job_id, 0, io.BytesIO(b"abc"), 3)
    folder = first._dir(job_id)
    (folder / "recover.mp4.part").replace(folder / "recover.mp4")
    first._write(job_id, state="analyzing", phase="transcribing")

    restarted = manager(tmp_path)
    recovered = restarted.status(job_id)
    assert recovered["state"] == "interrupted"
    assert recovered["phase"] == "interrupted"

    restarted._analyze = lambda value: restarted._write(value, state="review")
    resumed = restarted.resume(job_id)
    restarted._threads[job_id].join(timeout=2)
    assert resumed["state"] == "queued"
    assert restarted.status(job_id)["state"] == "review"
