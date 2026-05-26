from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class BenchmarkExpected(BaseModel):
    min_slides: int | None = None
    max_slides: int | None = None
    required_sections: list[str] = Field(default_factory=list)
    expected_keywords: list[str] = Field(default_factory=list)
    min_visual_score: float | None = None
    max_content_issue_count: int | None = None
    require_pptx: bool = True
    require_preview: bool = False
    require_quality_report: bool = True
    require_trace_summary: bool = True


class BenchmarkCase(BaseModel):
    case_id: str
    name: str = ""
    topic: str = ""
    language: str = "zh-CN"
    audience: str = ""
    input_document_path: str | None = None
    run_id: str | None = None
    expected: BenchmarkExpected = Field(default_factory=BenchmarkExpected)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkSuite(BaseModel):
    suite_id: str
    name: str = ""
    cases: list[BenchmarkCase] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def load_benchmark_suite(path: str | Path) -> BenchmarkSuite:
    suite_path = Path(path)
    if not suite_path.exists():
        raise FileNotFoundError(f"Benchmark suite file not found: {suite_path}")
    if suite_path.suffix.lower() != ".json":
        raise ValueError(f"Unsupported benchmark suite format: {suite_path.suffix or '[none]'}")

    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid benchmark suite JSON: {suite_path}") from exc

    suite = BenchmarkSuite.model_validate(payload)
    seen: set[str] = set()
    duplicates: list[str] = []
    for case in suite.cases:
        if case.case_id in seen:
            duplicates.append(case.case_id)
        seen.add(case.case_id)
    if duplicates:
        duplicate_list = ", ".join(sorted(set(duplicates)))
        raise ValueError(f"Duplicate benchmark case_id(s): {duplicate_list}")
    return suite
