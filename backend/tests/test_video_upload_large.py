import io
from collections import namedtuple

import pytest
import video_analysis

shutil_usage = namedtuple("usage", "free total used", defaults=(0, 0))


@pytest.mark.parametrize("size", [50 * 1024**3, 100 * 1024**3])
def test_large_video_metadata_and_resume_do_not_allocate_large_file(tmp_path, monkeypatch, size):
    monkeypatch.setattr(video_analysis, "_require_analysis_runtime", lambda: None)
    monkeypatch.setattr(video_analysis.shutil, "disk_usage",
                        lambda _p: shutil_usage(free=200 * 1024**3))
    manager = video_analysis.VideoAnalysisManager(root=tmp_path, catalog_provider=lambda: {
        "products": [{"product_id": "p", "title": "Product"}]})
    state = manager.start_upload("large.mp4", size, "a" * 64)
    assert state["total_size"] == size
    assert state["uploaded_size"] == 0
    manager.append_chunk(state["job_id"], 0, io.BytesIO(b"x" * 16), 16)
    resumed = manager.resumable_job("large.mp4", size, "a" * 64)
    assert resumed["job_id"] == state["job_id"]
    assert resumed["uploaded_size"] == 16
    assert sum(p.stat().st_size for p in tmp_path.rglob('*') if p.is_file()) < 16384


def test_over_100gb_is_rejected_before_creating_job(tmp_path, monkeypatch):
    monkeypatch.setattr(video_analysis, "_require_analysis_runtime", lambda: None)
    manager = video_analysis.VideoAnalysisManager(root=tmp_path, catalog_provider=lambda: {
        "products": [{"product_id": "p", "title": "Product"}]})
    with pytest.raises(ValueError, match="invalid_video_size"):
        manager.start_upload("large.mp4", 100 * 1024**3 + 1)
    assert not list(tmp_path.rglob('job.json'))
