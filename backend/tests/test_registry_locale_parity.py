"""todo 26 (d): errors.json <-> backend ERROR_REGISTRY parity, wired into pytest.

Wraps backend/tests/tools/compare_registry_locales.py — the acceptance script
from todo 15 — as a first-class pytest case so any backend test run (and any
future backend CI) re-proves that:
  1. every registry code (minus email.*) has a zh AND en errors.json key,
  2. every locale key maps back to a registry code or a documented
     frontend-only key,
  3. zh values are byte-exact against the registry default details,
  4. the common 422 pydantic-type keys exist in both locales.

The tool is CWD-independent (it resolves paths from its own __file__), so the
pytest process can run from anywhere.
"""

import importlib.util
import sys
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parent / "tools" / "compare_registry_locales.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("compare_registry_locales", TOOL_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {TOOL_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("compare_registry_locales", module)
    spec.loader.exec_module(module)
    return module


def test_errors_json_matches_backend_registry():
    tool = _load_tool()
    report = tool.main()
    assert "RESULT: PASS" in report, f"registry/locale parity FAILED:\n{report}"
