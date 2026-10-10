import importlib.util
from pathlib import Path
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "tools" / "run_tests.py"
SPEC = importlib.util.spec_from_file_location("run_tests_tool", PATH)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def test_extension_suite_requires_explicit_developer_node(monkeypatch):
    monkeypatch.setattr(TOOL.sys, "argv", ["run_tests.py", "--suite", "extension"])
    with mock.patch.object(TOOL.shutil, "which", return_value=None):
        assert TOOL.main() == 2
