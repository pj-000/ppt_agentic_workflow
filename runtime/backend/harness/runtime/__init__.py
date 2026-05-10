from .benchmark_gate import BenchmarkGateStore, BenchmarkVerdict
from .benchmark_runner import (
    BenchmarkCaseComparison,
    BenchmarkCaseObservation,
    BenchmarkCaseSpec,
    BenchmarkObservations,
    BenchmarkRunReport,
    BenchmarkRunner,
    BenchmarkTarget,
    BenchmarkTargetResult,
    GoldenBenchmarkManifest,
)
from .catalog import (
    get_audience_aliases,
    get_audience_profiles,
    get_default_principle_descriptions,
    get_evaluation_metric_aliases,
    get_generated_image_hints,
    get_js_diagram_hints,
    get_skill_asset_registry,
    get_learned_skill_registry,
    get_skill_policy_map,
    get_shape_value_map,
    get_supported_audiences,
    get_supported_styles,
)
from .context import SkillContext
from .prompt_composer import PromptComposer
from .progressive_loading import LoadedSkillRecord, PromptBundle, PromptSection, merge_prompt_sections
from .learned_skills import LearnedSkillStore
from .promoted_lessons import PromotedLessonStore, PromotedRepairLesson
from .repair_orchestrator import RepairOrchestrator
from .runtime_memory import RepairMemoryRecord, RuntimeMemoryStore
from .skill_policy import SkillPolicyEntry, SkillPolicyStore
from .skill_runtime import SkillRuntime
from .trace import HarnessTrace
from .skill_loader import SkillLoader
from .state_machine import HarnessRunState, PhaseExecution

def __getattr__(name: str):
    if name in {"BenchmarkCaseExecution", "BenchmarkObservationGenerator"}:
        from .benchmark_observer import BenchmarkCaseExecution, BenchmarkObservationGenerator

        return {
            "BenchmarkCaseExecution": BenchmarkCaseExecution,
            "BenchmarkObservationGenerator": BenchmarkObservationGenerator,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_audience_aliases",
    "get_audience_profiles",
    "get_default_principle_descriptions",
    "get_evaluation_metric_aliases",
    "get_generated_image_hints",
    "get_js_diagram_hints",
    "get_skill_asset_registry",
    "get_learned_skill_registry",
    "get_skill_policy_map",
    "get_shape_value_map",
    "get_supported_audiences",
    "get_supported_styles",
    "BenchmarkGateStore",
    "BenchmarkVerdict",
    "BenchmarkCaseObservation",
    "BenchmarkCaseExecution",
    "BenchmarkCaseComparison",
    "BenchmarkCaseSpec",
    "BenchmarkObservationGenerator",
    "BenchmarkObservations",
    "BenchmarkRunReport",
    "BenchmarkRunner",
    "BenchmarkTarget",
    "BenchmarkTargetResult",
    "GoldenBenchmarkManifest",
    "PromptComposer",
    "PromptBundle",
    "PromptSection",
    "merge_prompt_sections",
    "LoadedSkillRecord",
    "LearnedSkillStore",
    "PromotedLessonStore",
    "PromotedRepairLesson",
    "RepairMemoryRecord",
    "RepairOrchestrator",
    "HarnessTrace",
    "SkillContext",
    "SkillPolicyEntry",
    "SkillPolicyStore",
    "SkillLoader",
    "SkillRuntime",
    "RuntimeMemoryStore",
    "HarnessRunState",
    "PhaseExecution",
]
