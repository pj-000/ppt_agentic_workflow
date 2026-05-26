"""Runtime entrypoint for the bundled PPT generation skill."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

SKILL_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = SKILL_ROOT / "assets" / "runtime"
RUNTIME_API_MODULE_NAME = "directionai_ppt_generation_skill_api"


def ensure_runtime_root() -> Path:
    """Expose the embedded PPT runtime on ``sys.path`` for dynamic imports."""
    if not RUNTIME_ROOT.exists():
        raise FileNotFoundError(f"PPT skill runtime not found: {RUNTIME_ROOT}")

    runtime_root = str(RUNTIME_ROOT)
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)
        importlib.invalidate_caches()
    return RUNTIME_ROOT


@lru_cache(maxsize=1)
def get_runtime_api_module() -> ModuleType:
    """Import and cache the bundled PPT runtime ``api.py`` module."""
    ensure_runtime_root()
    if RUNTIME_API_MODULE_NAME in sys.modules:
        return sys.modules[RUNTIME_API_MODULE_NAME]

    module_path = RUNTIME_ROOT / "api.py"
    spec = importlib.util.spec_from_file_location(RUNTIME_API_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load PPT skill api module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_API_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def upload_document_route(*args, **kwargs):
    return get_runtime_api_module().upload_document_route(*args, **kwargs)


def stream_ppt_outline_route(*args, **kwargs):
    return get_runtime_api_module().stream_ppt_outline_route(*args, **kwargs)


def stream_ppt_from_outline_route(*args, **kwargs):
    return get_runtime_api_module().stream_ppt_from_outline_route(*args, **kwargs)


def stream_evaluate_ppt_route(*args, **kwargs):
    return get_runtime_api_module().stream_evaluate_ppt_route(*args, **kwargs)


def download_ppt_route(*args, **kwargs):
    return get_runtime_api_module().download_ppt_route(*args, **kwargs)


def preview_ppt_image_route(*args, **kwargs):
    return get_runtime_api_module().preview_ppt_image_route(*args, **kwargs)


def __getattr__(name: str):
    """Proxy unknown attributes to the bundled runtime API module."""
    return getattr(get_runtime_api_module(), name)


__all__ = [
    "ensure_runtime_root",
    "get_runtime_api_module",
    "upload_document_route",
    "stream_ppt_outline_route",
    "stream_ppt_from_outline_route",
    "stream_evaluate_ppt_route",
    "download_ppt_route",
    "preview_ppt_image_route",
]
