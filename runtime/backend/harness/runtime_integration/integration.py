from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.harness.runtime_integration.models import HarnessBundleResult, HarnessIntegrationConfig
from backend.harness.runtime_integration.post_run import PostRunHarnessRunner


def build_default_post_run_config() -> HarnessIntegrationConfig:
    return HarnessIntegrationConfig(
        enable_episode_memory=True,
        enable_repair_planning=True,
        enable_replan_decision=True,
        enable_one_run_benchmark=False,
        execute_repair=False,
        apply_replan_patches=False,
        fail_soft=True,
    )


def run_post_generation_harness(
    *,
    run_id: str,
    run_dir: str | Path,
    output_root: str | Path,
    memory: Any | None = None,
    trace: Any | None = None,
    config: HarnessIntegrationConfig | None = None,
) -> HarnessBundleResult:
    runner = PostRunHarnessRunner(
        output_root=output_root,
        memory=memory,
        trace=trace,
        config=config or build_default_post_run_config(),
    )
    return runner.run(run_id=run_id, run_dir=run_dir)
