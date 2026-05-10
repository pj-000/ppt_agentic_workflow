"""Load the standalone PPT runtime without the DeerFlow gateway stack."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
RUNTIME_API_MODULE_NAME = "standalone_ppt_generation_runtime_api"


def ensure_runtime_root() -> Path:
    if not RUNTIME_ROOT.exists():
        raise FileNotFoundError(f"PPT runtime not found: {RUNTIME_ROOT}")

    runtime_root = str(RUNTIME_ROOT)
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)
        importlib.invalidate_caches()
    return RUNTIME_ROOT


@lru_cache(maxsize=1)
def get_runtime_api_module() -> ModuleType:
    ensure_runtime_root()
    if RUNTIME_API_MODULE_NAME in sys.modules:
        return sys.modules[RUNTIME_API_MODULE_NAME]

    module_path = RUNTIME_ROOT / "api.py"
    spec = importlib.util.spec_from_file_location(RUNTIME_API_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load PPT runtime API from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_API_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module

