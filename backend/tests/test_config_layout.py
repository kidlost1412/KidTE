import os
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import config


def _extension(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("{}", encoding="utf-8")
    (folder / "schedule.js").write_text("", encoding="utf-8")


def test_extension_lookup_does_not_fall_back_to_legacy_project_folder(tmp_path):
    root = tmp_path / "backend"
    root.mkdir()
    _extension(tmp_path / "legacy-extension-outside-project")

    with (
        mock.patch.object(config, "ROOT", root),
        mock.patch.dict(os.environ, {"LIVE_RE_EXTENSION_DIR": ""}, clear=False),
    ):
        assert config._find_extension_dir() is None


def test_extension_lookup_uses_current_project_extension_folder(tmp_path):
    root = tmp_path / "backend"
    root.mkdir()
    extension = tmp_path / "extension"
    _extension(extension)

    with (
        mock.patch.object(config, "ROOT", root),
        mock.patch.dict(os.environ, {"LIVE_RE_EXTENSION_DIR": ""}, clear=False),
    ):
        assert config._find_extension_dir() == extension


def test_source_data_override_does_not_move_plugins(tmp_path):
    env = dict(os.environ, KID_TE_ROOT=str(tmp_path / 'isolated-data'))
    result = subprocess.run([sys.executable, '-c',
        'import config,json; print(json.dumps([str(config.ROOT),str(config.PLUGINS_DIR)]))'],
        cwd=SRC, env=env, capture_output=True, text=True, check=True)
    root, plugins = json.loads(result.stdout)
    assert Path(root) == tmp_path / 'isolated-data'
    assert Path(plugins) == SRC.parents[1] / 'plugins'


def test_frozen_extension_is_found_next_to_executable(tmp_path):
    install = tmp_path / "installed"
    extension = install / "extension"
    _extension(extension)
    root = tmp_path / "app-data" / "KidTE"
    root.mkdir(parents=True)
    with (
        mock.patch.object(config, "IS_FROZEN", True),
        mock.patch.object(config, "INSTALL_ROOT", install),
        mock.patch.object(config, "ROOT", root),
        mock.patch.dict(os.environ, {"LIVE_RE_EXTENSION_DIR": ""}, clear=False),
    ):
        assert config._find_extension_dir() == extension


def test_frozen_runtime_prefers_portable_data_next_to_tool(tmp_path):
    install = tmp_path / "KidTE"
    install.mkdir()
    portable = tmp_path / "DuLieu"
    portable.mkdir()
    with (
        mock.patch.object(config, "IS_FROZEN", True),
        mock.patch.object(config, "INSTALL_ROOT", install),
        mock.patch.dict(os.environ, {"KID_TE_ROOT": "", "LIVE_RE_ROOT": ""}, clear=False),
    ):
        assert config._runtime_root() == portable


def test_runtime_root_override_has_priority(tmp_path):
    override = tmp_path / "custom-data"
    with mock.patch.dict(os.environ, {"KID_TE_ROOT": "", "LIVE_RE_ROOT": str(override)}, clear=False):
        assert config._runtime_root() == override


def test_frozen_runtime_creates_portable_data_root_instead_of_appdata(tmp_path):
    r"""`[Goi 14]` Lan dau chua co ``DuLieu`` thi TAO tai thu muc goc portable.

    Test nay truoc day khoa lai hanh vi nguoc lai: lui ve
    ``%LOCALAPPDATA%\KidTE``. Ke hoach nang cap chot ro "Lan dau chua co
    DuLieu thi tao tai thu muc goc portable" va "Khong fallback am tham sang
    AppData", nen y nghia nghiep vu cua ca nay da doi — day khong phai sua
    assertion de lay mau xanh.

    Ly do doi: quan sat duoc mot lan chay ma DuLieu chua kip ton tai da ghi
    ca hai database, log va cau hinh vao AppData. Du lieu that nam ngoai thu
    muc ban giao nen khong duoc backup, va lan chay sau (khi DuLieu da co)
    lai doc mot kho hoan toan khac.
    """
    install = tmp_path / "installed" / "KidTE"
    install.mkdir(parents=True)
    local = tmp_path / "local-appdata"
    with (
        mock.patch.object(config, "IS_FROZEN", True),
        mock.patch.object(config, "INSTALL_ROOT", install),
        mock.patch.dict(
            os.environ,
            {"KID_TE_ROOT": "", "LIVE_RE_ROOT": "", "LOCALAPPDATA": str(local)},
            clear=False,
        ),
    ):
        ket_qua = config._runtime_root()
    assert ket_qua == install.parent / "DuLieu"
    assert ket_qua.is_dir(), "phai tao that su, khong chi tra ve duong dan"
    assert not (local / "KidTE").exists(), "khong duoc dung toi AppData"
