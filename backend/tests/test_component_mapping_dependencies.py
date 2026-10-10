"""Dependency closure checks for complete persisted component generations."""
from __future__ import annotations

import json
import uuid

import pytest
from component_store import ComponentStore, ComponentStoreError
from test_optional_components import _package


def _stage_records(tmp_path, component_ids, *, core_version="1.14.255"):
    app_root = tmp_path / "App"
    app_root.mkdir()
    store = ComponentStore(app_root)
    records = {}
    for component_id in component_ids:
        version = core_version if component_id == "core" else "1.0.0"
        archive, record = _package(tmp_path, component_id, version)
        store.stage_package(record, archive)
        records[component_id] = record
    return store, records


@pytest.mark.parametrize("components", [
    ["core", "gpu-cuda12"],
    ["core", "media-tools", "captcha-solver"],
    ["core", "gpu-cuda12", "media-tools", "captcha-solver", "video-analysis-cpu"],
])
def test_prepare_accepts_dependency_complete_optional_mappings(tmp_path, components):
    store, records = _stage_records(tmp_path, components)

    pending = store.prepare(records, catalog_sequence=1)

    assert set(pending["components"]) == set(components)


def test_prepare_rejects_mapping_with_missing_dependency(tmp_path):
    store, records = _stage_records(tmp_path, ["captcha-solver"])

    with pytest.raises(ComponentStoreError, match="dependency is missing"):
        store.prepare(records, catalog_sequence=1)

    assert store.load_pending() is None


def test_prepare_rejects_dependency_version_outside_range(tmp_path):
    store, records = _stage_records(
        tmp_path, ["core", "media-tools", "captcha-solver"], core_version="1.14.254"
    )

    with pytest.raises(ComponentStoreError, match="dependency version mismatch"):
        store.prepare(records, catalog_sequence=1)

    assert store.load_pending() is None


def test_activate_pending_rejects_tampered_mapping_missing_dependency(tmp_path):
    store, records = _stage_records(tmp_path, ["core", "media-tools", "captcha-solver"])
    pending = store.prepare(records, catalog_sequence=1)
    tampered = json.loads(store.pending_path.read_text(encoding="utf-8"))
    del tampered["components"]["media-tools"]
    store.pending_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(ComponentStoreError, match="dependency is missing"):
        store.activate_pending()

    assert store.load_active() is None
    assert store.load_pending() is None
    assert pending["generation"] == tampered["generation"]


def test_empty_mapping_remains_valid_for_rollback_baseline(tmp_path):
    store = ComponentStore(tmp_path / "App")
    mapping = {"schema_version": 1, "generation": str(uuid.uuid4()),
               "catalog_sequence": 1, "components": {}}

    store._validate_mapping(mapping)
