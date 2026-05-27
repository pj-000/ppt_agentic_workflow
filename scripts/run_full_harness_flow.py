from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from backend.harness.memory.integration import create_default_agent_memory  # noqa: E402
from backend.harness.runtime_integration import (  # noqa: E402
    HarnessIntegrationConfig,
    run_post_generation_harness,
)


MISSING_CORE_ARTIFACTS_MESSAGE = (
    "Missing quality_report.json or trace_summary.json.\n"
    "Please run PPT generation first, or use --mode synthetic to inspect the Harness flow "
    "without external dependencies."
)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def find_latest_run_dir(output_root: Path) -> Path | None:
    runs_root = output_root / "runs"
    if not runs_root.exists():
        return None
    candidates = [
        path
        for path in runs_root.iterdir()
        if path.is_dir() and ((path / "quality_report.json").exists() or (path / "trace_summary.json").exists())
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_synthetic_run_artifacts(*, run_id: str, output_root: Path) -> Path:
    run_dir = output_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    quality_report = _synthetic_quality_report(run_id)
    trace_summary = _synthetic_trace_summary(run_id)

    (run_dir / "quality_report.json").write_text(
        json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "quality_report.md").write_text(_quality_report_markdown(quality_report), encoding="utf-8")
    (run_dir / "trace_summary.json").write_text(
        json.dumps(trace_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "trace_summary.md").write_text(_trace_summary_markdown(trace_summary), encoding="utf-8")
    (run_dir / "trace.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_id": run_id,
                        "stage": "run.started",
                        "status": "success",
                        "payload": {"synthetic": True},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "run_id": run_id,
                        "stage": "quality.reported",
                        "status": "success",
                        "payload": {"quality_report": "quality_report.json", "synthetic": True},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def print_harness_metrics(*, run_dir: Path, bundle_result: Any | None = None) -> None:
    quality = load_json(run_dir / "quality_report.json")
    trace = load_json(run_dir / "trace_summary.json")
    manifest = load_json(run_dir / "harness_manifest.json")
    repair_plan = load_json(run_dir / "repair_plan.json")
    replan_decision = load_json(run_dir / "replan_decision.json")
    benchmark = _load_benchmark_report(run_dir=run_dir, bundle_result=bundle_result)

    run = _mapping(quality.get("run"))
    quality_summary = _mapping(quality.get("summary"))
    repair_metadata = _mapping(repair_plan.get("metadata"))
    replan_metadata = _mapping(replan_decision.get("metadata"))
    benchmark_path = _benchmark_report_path(run_dir=run_dir, bundle_result=bundle_result)

    print("=== PPT Generation Agent Harness Demo ===")
    print(f"Run ID: {run_dir.name}")
    print(f"Run Dir: {run_dir}")
    print("")
    print("Core Artifacts:")
    print(f"- quality_report.json: {_exists_label(run_dir / 'quality_report.json')}")
    print(f"- trace_summary.json: {_exists_label(run_dir / 'trace_summary.json')}")
    print(f"- harness_summary.md: {_exists_label(run_dir / 'harness_summary.md')}")
    print(f"- benchmark_report.md: {_exists_label(benchmark_path) if benchmark_path else 'N/A'}")
    print("")
    print("Quality:")
    print(f"- quality_status: {_value(quality_summary.get('status'))}")
    print(f"- pptx_exists: {_value(run.get('pptx_exists'))}")
    print(f"- preview_success: {_value(run.get('preview_success'))}")
    print(f"- slide_count: {_value(run.get('slide_count'))}")
    print(f"- visual_score_avg: {_value(run.get('visual_score_avg'))}")
    print(f"- visual_score_min: {_value(run.get('visual_score_min'))}")
    print(f"- content_issue_count: {_value(run.get('content_issue_count'))}")
    print("")
    print("Trace:")
    print(f"- trace_status: {_value(trace.get('status'))}")
    print(f"- tool_call_count: {_value(trace.get('tool_call_count'))}")
    print(f"- tool_attempt_count: {_value(trace.get('tool_attempt_count'))}")
    print(f"- failed_tool_count: {_value(trace.get('failed_tool_count'))}")
    print(f"- skipped_tool_count: {_value(trace.get('skipped_tool_count'))}")
    print(f"- timeout_tool_count: {_value(trace.get('timeout_tool_count'))}")
    print(f"- top_error_signatures: {_value(trace.get('error_signatures') or [])}")
    print("")
    print("Benchmark:")
    print(f"- status: {_value(benchmark.get('status'))}")
    print(f"- strict_success_rate: {_value(benchmark.get('end_to_end_success_rate'))}")
    print(f"- acceptable_success_rate: {_value(benchmark.get('acceptable_success_rate'))}")
    print(f"- pptx_exists_rate: {_value(benchmark.get('pptx_exists_rate'))}")
    print(f"- preview_success_rate: {_value(benchmark.get('preview_success_rate'))}")
    print(f"- tool_call_success_rate: {_value(benchmark.get('tool_call_success_rate'))}")
    print("")
    print("Repair:")
    print(f"- repair_plan_status: {_value(repair_plan.get('status'))}")
    print(f"- issue_count: {_value(len(repair_plan.get('issues', [])) if repair_plan else None)}")
    print(f"- action_count: {_value(len(repair_plan.get('actions', [])) if repair_plan else None)}")
    print(f"- repair_action_count: {_value(len(repair_plan.get('actions', [])) if repair_plan else None)}")
    print(f"- repair_context: {_value(repair_metadata.get('context') or repair_metadata.get('missing_artifacts'))}")
    print("")
    print("Replan:")
    print(f"- replan_status: {_value(replan_decision.get('status'))}")
    print(f"- patch_count: {_value(replan_metadata.get('patch_count'))}")
    print(f"- replan_patch_count: {_value(replan_metadata.get('patch_count'))}")
    print(f"- high_risk_patch_count: {_value(replan_metadata.get('high_risk_patch_count'))}")
    print(f"- auto_apply_patch_count: {_value(replan_metadata.get('auto_apply_patch_count'))}")
    print("")
    print("Memory:")
    memory_ids = getattr(bundle_result, "memory_write_ids", None)
    if memory_ids is None:
        memory_ids = manifest.get("memory_writes", [])
    print(f"- memory_write_ids: {_value(memory_ids)}")


def run_synthetic_mode(*, args: argparse.Namespace) -> int:
    output_root = args.output_root
    run_id = args.run_id or "demo_synthetic_001"
    run_dir = write_synthetic_run_artifacts(run_id=run_id, output_root=output_root)
    bundle_result = _run_post_harness(run_id=run_id, run_dir=run_dir, args=args)
    print_harness_metrics(run_dir=run_dir, bundle_result=bundle_result)
    print("")
    print("Note: synthetic metrics are demo data for understanding the pipeline, not resume evidence.")
    return 0


def run_existing_run_mode(*, args: argparse.Namespace) -> int:
    run_dir = args.run_dir
    if run_dir is None:
        print("--run-dir is required for existing-run mode.")
        return 2
    if not _has_core_artifacts(run_dir):
        print(MISSING_CORE_ARTIFACTS_MESSAGE)
        return 1
    bundle_result = _run_post_harness(run_id=run_dir.name, run_dir=run_dir, args=args)
    print_harness_metrics(run_dir=run_dir, bundle_result=bundle_result)
    return 0


def run_latest_run_mode(*, args: argparse.Namespace) -> int:
    run_dir = find_latest_run_dir(args.output_root)
    if run_dir is None:
        print("No run with quality_report.json or trace_summary.json was found under outputs/runs.")
        print("Run PPT generation first, or use --mode synthetic to inspect the Harness flow.")
        return 1
    if not _has_core_artifacts(run_dir):
        print(MISSING_CORE_ARTIFACTS_MESSAGE)
        return 1
    bundle_result = _run_post_harness(run_id=run_dir.name, run_dir=run_dir, args=args)
    print_harness_metrics(run_dir=run_dir, bundle_result=bundle_result)
    return 0


def run_real_generate_mode() -> int:
    print(
        "real-generate mode is not implemented yet. Please run the existing ppt-backend generate "
        "command manually, then use --mode latest-run or --mode existing-run."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local PPT Generation Agent Harness demo flow.")
    parser.add_argument(
        "--mode",
        choices=["synthetic", "existing-run", "latest-run", "real-generate"],
        default="synthetic",
    )
    parser.add_argument("--run-id", default="demo_synthetic_001")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("./outputs"))
    parser.add_argument("--enable-benchmark", action="store_true")
    parser.add_argument("--topic", default="")
    parser.add_argument("--min-slides", type=int, default=6)
    parser.add_argument("--max-slides", type=int, default=8)
    parser.add_argument("--image-mode", default="off")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "synthetic":
        return run_synthetic_mode(args=args)
    if args.mode == "existing-run":
        return run_existing_run_mode(args=args)
    if args.mode == "latest-run":
        return run_latest_run_mode(args=args)
    return run_real_generate_mode()


def _run_post_harness(*, run_id: str, run_dir: Path, args: argparse.Namespace) -> Any:
    memory = create_default_agent_memory(output_root=args.output_root)
    config = HarnessIntegrationConfig(
        enable_episode_memory=True,
        enable_repair_planning=True,
        enable_replan_decision=True,
        enable_one_run_benchmark=bool(args.enable_benchmark),
        execute_repair=False,
        apply_replan_patches=False,
        fail_soft=True,
        benchmark_suite_id="single_run_smoke",
        metadata={"demo_runner": True, "mode": args.mode},
    )
    return run_post_generation_harness(
        run_id=run_id,
        run_dir=run_dir,
        output_root=args.output_root,
        memory=memory,
        config=config,
    )


def _has_core_artifacts(run_dir: Path) -> bool:
    return (run_dir / "quality_report.json").exists() and (run_dir / "trace_summary.json").exists()


def _synthetic_quality_report(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run": {
            "topic": "Synthetic Harness Demo",
            "slide_count": 6,
            "pptx_exists": True,
            "preview_success": True,
            "visual_score_avg": 4.1,
            "visual_score_min": 3.2,
            "content_issue_count": 2,
            "repaired_slide_count": 0,
            "repair_attempt_count": 0,
            "tool_error_count": 0,
        },
        "summary": {
            "status": "success",
            "issue_count": 1,
            "critical_issue_count": 0,
            "synthetic": True,
        },
        "slides": [
            {"slide_index": 0, "title": "Harness overview", "visual_score": 4.4},
            {"slide_index": 1, "title": "Quality and trace", "visual_score": 4.2},
            {"slide_index": 2, "title": "Repair and replan", "visual_score": 3.2},
        ],
        "issues": [
            {
                "issue_type": "visual_density_warning",
                "severity": "warning",
                "scope": "visual",
                "slide_index": 2,
                "message": "Synthetic slide 2 is intentionally near the visual threshold for demo planning.",
            }
        ],
        "missing_reasons": {},
        "artifacts": {
            "pptx": f"runs/{run_id}/synthetic_demo.pptx",
            "preview": f"runs/{run_id}/preview/slide_001.png",
        },
        "metadata": {"synthetic": True, "description": "Synthetic quality report for local harness demo."},
    }


def _synthetic_trace_summary(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "success",
        "tool_call_count": 5,
        "tool_attempt_count": 5,
        "failed_tool_count": 0,
        "skipped_tool_count": 0,
        "timeout_tool_count": 0,
        "error_signatures": [],
        "artifact_refs": {
            "quality_report": f"runs/{run_id}/quality_report.json",
            "trace_summary": f"runs/{run_id}/trace_summary.json",
        },
        "quality_report_paths": [f"runs/{run_id}/quality_report.json"],
        "metadata": {"synthetic": True, "description": "Synthetic trace summary for local harness demo."},
    }


def _quality_report_markdown(report: dict[str, Any]) -> str:
    run = _mapping(report.get("run"))
    return "\n".join(
        [
            "# Synthetic Quality Report",
            "",
            "This report is generated by `scripts/run_full_harness_flow.py --mode synthetic`.",
            "It is demo data only and must not be used as real benchmark evidence.",
            "",
            f"- PPTX Exists: {_value(run.get('pptx_exists'))}",
            f"- Preview Success: {_value(run.get('preview_success'))}",
            f"- Slide Count: {_value(run.get('slide_count'))}",
            f"- Visual Score Avg: {_value(run.get('visual_score_avg'))}",
            f"- Visual Score Min: {_value(run.get('visual_score_min'))}",
            f"- Content Issue Count: {_value(run.get('content_issue_count'))}",
            "",
        ]
    )


def _trace_summary_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Synthetic Trace Summary",
            "",
            "This trace summary is synthetic demo data.",
            "",
            f"- Status: {_value(summary.get('status'))}",
            f"- Tool Call Count: {_value(summary.get('tool_call_count'))}",
            f"- Tool Attempt Count: {_value(summary.get('tool_attempt_count'))}",
            f"- Failed Tool Count: {_value(summary.get('failed_tool_count'))}",
            f"- Skipped Tool Count: {_value(summary.get('skipped_tool_count'))}",
            f"- Timeout Tool Count: {_value(summary.get('timeout_tool_count'))}",
            "",
        ]
    )


def _load_benchmark_report(*, run_dir: Path, bundle_result: Any | None) -> dict[str, Any]:
    path = _benchmark_report_path(run_dir=run_dir, bundle_result=bundle_result, prefer_json=True)
    return load_json(path) if path else {}


def _benchmark_report_path(
    *,
    run_dir: Path,
    bundle_result: Any | None,
    prefer_json: bool = False,
) -> Path | None:
    benchmark_id = getattr(bundle_result, "benchmark_id", None)
    if not benchmark_id:
        manifest = load_json(run_dir / "harness_bundle.json")
        benchmark_id = manifest.get("benchmark_id")
    if not benchmark_id:
        return None
    benchmarks_root = run_dir.parent.parent / "benchmarks" / str(benchmark_id)
    filename = "benchmark_report.json" if prefer_json else "benchmark_report.md"
    return benchmarks_root / filename


def _exists_label(path: Path | None) -> str:
    if path is None:
        return "N/A"
    return str(path) if path.exists() else "missing"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _value(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value) if value else "None"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
