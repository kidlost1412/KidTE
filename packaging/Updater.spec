# -*- mode: python ; coding: utf-8 -*-
"""Standalone one-file launcher; kept separate from the legacy core bundle."""
from pathlib import Path
import sys

PROJECT = Path(SPECPATH).parent
sys.path.insert(0, str(PROJECT / "backend" / "src"))

a = Analysis(
    [str(PROJECT / "packaging" / "updater_entry.py")],
    pathex=[str(PROJECT / "backend" / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "component_store",
        "update_catalog",
        "extension_sync",
        "instance_lock",
        "port_diagnostics",
        "user_dialogs",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
        "cryptography.hazmat.primitives.asymmetric",
        "cryptography.hazmat.primitives.ciphers",
        "cryptography.hazmat.primitives.kdf",
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="KidTELauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(PROJECT / "assets" / "kid-te.ico"),
)
