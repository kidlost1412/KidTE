"""Đọc và kiểm tra hợp đồng plugin trước khi supervisor được phép chạy."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


PLUGIN_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_FIELDS = {
    "id": str, "ten": str, "loai": str, "phien_ban": str,
    "can_loi_toi_thieu": str, "mo_ta": str, "dung_toi": list,
    "lenh": list, "tu_khoi_dong_lai": bool, "kiem_tra_song": str,
    "phu_thuoc_plugin": list,
}


class PluginManifestError(ValueError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    name: str
    kind: str
    version: str
    core_min_version: str
    description: str
    resources: tuple[str, ...]
    command: tuple[str, ...]
    auto_restart: bool
    health_check: str
    dependencies: tuple[str, ...]
    directory: Path

    @property
    def command_path(self) -> Path:
        return self.directory / self.command[0]


def _safe_command(directory: Path, command: list) -> tuple[str, ...]:
    if not command or not all(isinstance(item, str) and item.strip() for item in command):
        raise PluginManifestError("lenh phải là danh sách chuỗi không rỗng")
    first = Path(command[0])
    if first.is_absolute() or ".." in first.parts:
        raise PluginManifestError("lenh[0] không được thoát thư mục plugin")
    resolved = (directory / first).resolve()
    try:
        resolved.relative_to(directory.resolve())
    except ValueError as exc:
        raise PluginManifestError("lenh[0] không được thoát thư mục plugin") from exc
    if not resolved.is_file():
        raise PluginManifestError(f"Không thấy entry point plugin: {first}")
    return tuple(command)


def load_manifest(path: Path) -> PluginManifest:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"Không đọc được plugin.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginManifestError("plugin.json phải là object")
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in raw:
            raise PluginManifestError(f"Thiếu trường bắt buộc: {field}")
        # bool là subclass int: dùng type chính xác cho schema rõ ràng.
        if type(raw[field]) is not expected_type:
            raise PluginManifestError(f"Trường {field} sai kiểu")
    plugin_id = raw["id"]
    if not PLUGIN_ID.fullmatch(plugin_id):
        raise PluginManifestError("id plugin chỉ dùng chữ thường, số và dấu gạch ngang")
    if raw["loai"] not in {"dich-vu", "bo-xu-ly"}:
        raise PluginManifestError("loai chỉ nhận dich-vu hoặc bo-xu-ly")
    if raw["kiem_tra_song"] not in {"pid"}:
        raise PluginManifestError("kiem_tra_song chưa được hỗ trợ")
    if not all(isinstance(item, str) and item for item in raw["dung_toi"]):
        raise PluginManifestError("dung_toi phải là danh sách chuỗi")
    if not all(PLUGIN_ID.fullmatch(item or "") for item in raw["phu_thuoc_plugin"]):
        raise PluginManifestError("phu_thuoc_plugin có id không hợp lệ")
    command = _safe_command(path.parent, raw["lenh"])
    return PluginManifest(
        plugin_id=plugin_id, name=raw["ten"], kind=raw["loai"],
        version=raw["phien_ban"], core_min_version=raw["can_loi_toi_thieu"],
        description=raw["mo_ta"], resources=tuple(raw["dung_toi"]), command=command,
        auto_restart=raw["tu_khoi_dong_lai"], health_check=raw["kiem_tra_song"],
        dependencies=tuple(raw["phu_thuoc_plugin"]), directory=path.parent,
    )


def discover_plugins(plugin_root: Path) -> tuple[dict[str, PluginManifest], dict[str, str]]:
    """Manifest lỗi chỉ cô lập plugin đó, không làm lõi sập."""
    manifests, errors, owners, duplicate_ids = {}, {}, {}, set()
    for manifest_path in sorted(Path(plugin_root).glob("*/plugin.json")):
        directory_name = manifest_path.parent.name
        try:
            manifest = load_manifest(manifest_path)
            if manifest.plugin_id in duplicate_ids:
                errors[directory_name] = f"ID trùng: {manifest.plugin_id}"
                continue
            if manifest.plugin_id in manifests:
                duplicate_ids.add(manifest.plugin_id)
                errors[owners[manifest.plugin_id]] = f"ID trùng: {manifest.plugin_id}"
                errors[directory_name] = f"ID trùng: {manifest.plugin_id}"
                manifests.pop(manifest.plugin_id)
                continue
            manifests[manifest.plugin_id] = manifest
            owners[manifest.plugin_id] = directory_name
        except PluginManifestError as exc:
            errors[directory_name] = str(exc)
    for plugin_id, manifest in list(manifests.items()):
        missing = sorted(set(manifest.dependencies) - set(manifests))
        if missing:
            errors[plugin_id] = "Thiếu plugin phụ thuộc: " + ", ".join(missing)
            manifests.pop(plugin_id)
    return manifests, errors
