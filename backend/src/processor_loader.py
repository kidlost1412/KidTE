"""Nạp bộ xử lý từ đường dẫn manifest mà không biến tên thư mục thành import."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def load_processor(path: Path, plugin_id: str):
    path = Path(path).resolve()
    name = "kid_te_processor_" + plugin_id.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Không nạp được bộ xử lý {plugin_id}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
