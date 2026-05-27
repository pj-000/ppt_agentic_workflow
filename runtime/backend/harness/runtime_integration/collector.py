from __future__ import annotations

from pathlib import Path

from backend.harness.runtime_integration.models import HarnessArtifactKind, HarnessArtifactRef
from backend.harness.runtime_integration.safety import sanitize_runtime_path


_KNOWN_ARTIFACTS: tuple[tuple[str, HarnessArtifactKind, bool, str], ...] = (
    ("quality_report.json", HarnessArtifactKind.QUALITY, True, "Structured quality report"),
    ("trace_summary.json", HarnessArtifactKind.TRACE, True, "Trace summary"),
    ("quality_report.md", HarnessArtifactKind.QUALITY, False, "Readable quality report"),
    ("trace.jsonl", HarnessArtifactKind.TRACE, False, "Trace event stream"),
    ("trace_summary.md", HarnessArtifactKind.TRACE, False, "Readable trace summary"),
    ("repair_plan.json", HarnessArtifactKind.REPAIR, False, "Repair plan"),
    ("repair_result.json", HarnessArtifactKind.REPAIR, False, "Repair result"),
    ("repair_report.md", HarnessArtifactKind.REPAIR, False, "Readable repair report"),
    ("plan_graph.json", HarnessArtifactKind.REPLAN, False, "Plan graph"),
    ("replan_decision.json", HarnessArtifactKind.REPLAN, False, "Replan decision"),
    ("replan_report.md", HarnessArtifactKind.REPLAN, False, "Readable replan report"),
    ("harness_manifest.json", HarnessArtifactKind.OTHER, False, "Harness manifest"),
    ("harness_bundle.json", HarnessArtifactKind.OTHER, False, "Harness bundle"),
    ("harness_summary.md", HarnessArtifactKind.OTHER, False, "Readable harness summary"),
)


def collect_run_artifacts(
    *,
    run_id: str,
    run_dir: str | Path,
    max_preview_artifacts: int = 20,
) -> list[HarnessArtifactRef]:
    del run_id
    path = Path(run_dir)
    artifacts = [
        HarnessArtifactRef(
            name=name,
            kind=kind,
            path=sanitize_runtime_path(path / name),
            exists=(path / name).exists(),
            required=required,
            description=description,
        )
        for name, kind, required, description in _KNOWN_ARTIFACTS
    ]
    artifacts.extend(_discover_pptx_artifacts(path))
    artifacts.extend(_discover_preview_artifacts(path, max_preview_artifacts=max_preview_artifacts))
    return artifacts


def _discover_pptx_artifacts(path: Path) -> list[HarnessArtifactRef]:
    return [
        HarnessArtifactRef(
            name=item.name,
            kind=HarnessArtifactKind.PPTX,
            path=sanitize_runtime_path(item),
            exists=True,
            required=False,
            description="Generated PPTX artifact",
        )
        for item in sorted(path.glob("*.pptx"))
        if item.is_file()
    ]


def _discover_preview_artifacts(path: Path, *, max_preview_artifacts: int = 20) -> list[HarnessArtifactRef]:
    preview_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    preview_files = []
    for child in path.rglob("*"):
        if child.is_file() and child.suffix.lower() in preview_suffixes:
            preview_files.append(child)
    sorted_files = sorted(preview_files)
    returned_files = sorted_files[: max(max_preview_artifacts, 0)]
    artifacts = [
        HarnessArtifactRef(
            name=item.name,
            kind=HarnessArtifactKind.PREVIEW,
            path=sanitize_runtime_path(item),
            exists=True,
            required=False,
            description="Preview image artifact",
        )
        for item in returned_files
    ]
    omitted_count = max(len(sorted_files) - len(returned_files), 0)
    if omitted_count:
        artifacts.append(
            HarnessArtifactRef(
                name="preview_images_truncated",
                kind=HarnessArtifactKind.PREVIEW,
                path=f"{omitted_count} preview image(s) omitted from manifest",
                exists=True,
                required=False,
                description="Preview image artifact list truncated to keep manifest compact",
            )
        )
    return artifacts
