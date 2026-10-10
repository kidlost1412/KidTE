"""Cô lập test khỏi cài đặt thật của máy (bộ xử lý, lời nhắc tự sửa)."""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
_TMP = Path(tempfile.mkdtemp(prefix="kid-te-test-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["KID_TE_ROOT"] = str(_TMP / "runtime")
os.environ.pop("LIVE_RE_ROOT", None)
for prefix in ("KID_TE", "LIVE_RE"):
    os.environ.setdefault(f"{prefix}_PROCESSOR_SETTINGS", str(_TMP / "cai_dat_bo_xu_ly.json"))
    os.environ.setdefault(f"{prefix}_PROMPT_OVERRIDE", str(_TMP / "loi_nhac_he_thong.txt"))

import pytest


@pytest.fixture(autouse=True)
def _detach_problem_log():
    """Bộ ghi sự cố không được dính vào logger thật của máy sau một test."""
    import logging
    logger = logging.getLogger('kid-te.core')
    saved = (list(logger.handlers), logger.level, logger.propagate, logger.disabled,
             getattr(logger, '_kid_te_configured', None))
    yield
    import problems
    problems.attach(None)
    for handler in logger.handlers:
        if handler not in saved[0]:
            handler.close()
    logger.handlers = saved[0]
    logger.setLevel(saved[1])
    logger.propagate, logger.disabled = saved[2:4]
    if saved[4] is None:
        logger.__dict__.pop('_kid_te_configured', None)
    else:
        logger._kid_te_configured = saved[4]
