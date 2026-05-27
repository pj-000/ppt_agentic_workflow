from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_full_harness_flow.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("run_full_harness_flow", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_mode_creates_artifacts(tmp_path: Path, capsys) -> None:
    runner = _load_runner_module()
    output_root = tmp_path / "outputs"
    run_id = "demo_synthetic_test"

    exit_code = runner.main(
        [
            "--mode",
            "synthetic",
            "--run-id",
            run_id,
            "--output-root",
            str(output_root),
            "--enable-benchmark",
        ]
    )

    assert exit_code == 0
    run_dir = output_root / "runs" / run_id
    assert (run_dir / "quality_report.json").exists()
    assert (run_dir / "trace_summary.json").exists()
    assert (run_dir / "harness_manifest.json").exists()
    assert (run_dir / "harness_bundle.json").exists()
    assert (run_dir / "harness_summary.md").exists()
    assert (run_dir / "repair_plan.json").exists()
    assert (run_dir / "replan_decision.json").exists()
    assert list((output_root / "benchmarks").glob("*/benchmark_report.json"))

    output = capsys.readouterr().out
    assert "strict_success_rate" in output
    assert "acceptable_success_rate" in output


def test_existing_run_mode_works_with_synthetic_artifacts(tmp_path: Path, capsys) -> None:
    runner = _load_runner_module()
    output_root = tmp_path / "outputs"
    run_id = "existing_synthetic"
    run_dir = runner.write_synthetic_run_artifacts(run_id=run_id, output_root=output_root)

    exit_code = runner.main(
        [
            "--mode",
            "existing-run",
            "--run-dir",
            str(run_dir),
            "--output-root",
            str(output_root),
            "--enable-benchmark",
        ]
    )

    assert exit_code == 0
    assert (run_dir / "harness_summary.md").exists()
    assert (run_dir / "repair_plan.json").exists()
    assert "Harness Demo" in capsys.readouterr().out


def test_latest_run_mode_finds_latest_run(tmp_path: Path) -> None:
    runner = _load_runner_module()
    output_root = tmp_path / "outputs"
    older = runner.write_synthetic_run_artifacts(run_id="run_old", output_root=output_root)
    newer = runner.write_synthetic_run_artifacts(run_id="run_new", output_root=output_root)
    older.touch()
    newer.touch()

    selected = runner.find_latest_run_dir(output_root)

    assert selected == newer


def test_missing_artifacts_prints_actionable_message(tmp_path: Path, capsys) -> None:
    runner = _load_runner_module()
    output_root = tmp_path / "outputs"
    run_dir = output_root / "runs" / "empty_run"
    run_dir.mkdir(parents=True)

    exit_code = runner.main(
        [
            "--mode",
            "existing-run",
            "--run-dir",
            str(run_dir),
            "--output-root",
            str(output_root),
        ]
    )

    assert exit_code == 1
    assert "Missing quality_report.json or trace_summary.json" in capsys.readouterr().out


def test_printed_metrics_include_key_names(tmp_path: Path, capsys) -> None:
    runner = _load_runner_module()
    output_root = tmp_path / "outputs"
    run_id = "metrics_synthetic"

    exit_code = runner.main(
        [
            "--mode",
            "synthetic",
            "--run-id",
            run_id,
            "--output-root",
            str(output_root),
            "--enable-benchmark",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "strict_success_rate" in output
    assert "acceptable_success_rate" in output
    assert "tool_call_success_rate" in output
    assert "visual_score_avg" in output
    assert "repair_plan_status" in output
    assert "replan_status" in output
