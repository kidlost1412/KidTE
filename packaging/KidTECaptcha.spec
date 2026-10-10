# -*- mode: python ; coding: utf-8 -*-
"""Independent one-folder CAPTCHA executable; no Kid TE backend is bundled."""
import os
import json
import hashlib
from pathlib import Path


PROJECT = Path(SPECPATH).parent
SOLVER = Path(os.environ.get("KIDTE_CAPTCHA_SOLVER_ROOT", "").strip()
              or PROJECT.parent.parent / "TikTokCaptchaSolver-clean-20260926")
PACKAGE = SOLVER / "src" / "tiktok_live_studio_captcha_solver"
if not (PACKAGE / "solver.py").is_file():
    raise SystemExit(f"Missing audited CAPTCHA solver source: {PACKAGE}")
audited = json.loads((PROJECT / 'packaging' / 'solver-patches' / 'manifest.json').read_text(encoding='utf-8'))
for filename, expected in (('solver.py', audited['solver_after_sha256']),
                           ('legacy_guard.py', audited['legacy_guard_sha256']),
                           ('episode_guard.py', audited['episode_guard_sha256'])):
    source = PACKAGE / filename
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise SystemExit('Unaudited solver source. Run packaging/solver-patches/install_guard.py --solver-root <path> before building.')

analysis = Analysis(
    [str(PROJECT / "packaging" / "captcha_entry.py")],
    pathex=[str(PROJECT / "backend" / "src"), str(SOLVER / "src")],
    binaries=[],
    datas=([(str(png), "tiktok_live_studio_captcha_solver/resources")
            for png in sorted((PACKAGE / "resources").glob("*.png"))] +
           [(str(script), "captcha-monitor")
            for script in sorted((PROJECT / "packaging" / "captcha-monitor").glob("*.ps1"))]),
    hiddenimports=['captcha_upload', 'tiktok_live_studio_captcha_solver.legacy_guard',
                   'tiktok_live_studio_captcha_solver.episode_guard'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="KidTECaptcha",
    console=False, debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, disable_windowed_traceback=False,
    icon=str(PROJECT / "assets" / "kid-te.ico"),
)
dist = COLLECT(exe, analysis.binaries, analysis.datas,
               strip=False, upx=False, name="KidTECaptcha")
