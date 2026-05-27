from __future__ import annotations

import json
from pathlib import Path

from backend.harness.runtime_integration.models import HarnessBundleResult, HarnessManifest
from backend.harness.runtime_integration.safety import sanitize_runtime_artifacts


def write_harness_manifest(
    manifest: HarnessManifest,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    output_path = path / "harness_manifest.json"
    output_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return sanitize_runtime_artifacts({"harness_manifest_json": str(output_path)})


def load_harness_manifest(path: str | Path) -> HarnessManifest | None:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return HarnessManifest.model_validate(payload)
    except Exception:
        return None


def write_harness_bundle_result(
    result: HarnessBundleResult,
    output_dir: str | Path,
) -> dict[str, str]:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    output_path = path / "harness_bundle.json"
    output_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return sanitize_runtime_artifacts({"harness_bundle_json": str(output_path)})
