from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

DOC_FILES = [
    ROOT / "docs/harness_engineering_overview.md",
    ROOT / "docs/module_map.md",
    ROOT / "docs/artifact_walkthrough.md",
    ROOT / "docs/benchmark_guide.md",
    ROOT / "docs/demo_runbook.md",
    ROOT / "docs/resume_alignment.md",
    ROOT / "docs/interview_playbook.md",
    ROOT / "docs/limitations_and_next_steps.md",
    ROOT / "docs/architecture_overview.md",
    ROOT / "docs/agent_harness_positioning.md",
    ROOT / "docs/refactor_guardrails.md",
]

README_DOC_LINKS = [
    "docs/harness_engineering_overview.md",
    "docs/architecture_overview.md",
    "docs/module_map.md",
    "docs/artifact_walkthrough.md",
    "docs/benchmark_guide.md",
    "docs/demo_runbook.md",
    "docs/resume_alignment.md",
    "docs/interview_playbook.md",
    "docs/limitations_and_next_steps.md",
]

LOCAL_PATH_MARKERS = [
    "/Users/sss",
    "/private/tmp",
    "/home/user",
    "C:\\Users\\",
]

FAKE_BENCHMARK_PATTERNS = [
    re.compile(r"提升\s*\d+%"),
    re.compile(r"success rate from\s*\d+%\s*to\s*\d+%", re.IGNORECASE),
    re.compile(r"\d+%\s*->\s*\d+%"),
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_docs() -> list[Path]:
    return [README, *DOC_FILES]


def test_readme_title_is_agent_harness() -> None:
    readme = _read(README)

    assert readme.startswith("# PPT Generation Agent Harness")
    assert "# Standalone PPT Generation Backend" not in readme


def test_readme_required_sections_are_present() -> None:
    readme = _read(README)

    required_sections = [
        "## Project Positioning",
        "## Why Agent Harness Engineering",
        "## Architecture",
        "## Capability Map",
        "## Install",
        "## CLI Usage",
        "## Backend Service Mode",
        "## Post-run Harness Artifacts",
        "## Benchmark",
        "## Boundaries and Limitations",
        "## Resume Summary",
        "## Further Reading",
    ]

    for section in required_sections:
        assert section in readme


def test_readme_preserves_existing_fastapi_route_list() -> None:
    readme = _read(README)

    routes = [
        "GET /health",
        "POST /upload_document",
        "POST /generate_ppt",
        "POST /stream_ppt_outline",
        "POST /stream_ppt_from_outline",
        "POST /stream_evaluate/ppt",
        "GET /download_ppt/{filename}",
        "GET /preview_ppt/{filename}/{image_name}",
    ]

    for route in routes:
        assert route in readme


def test_docs_do_not_contain_local_absolute_paths() -> None:
    for path in _all_docs():
        text = _read(path)
        for marker in LOCAL_PATH_MARKERS:
            assert marker not in text, path


def test_readme_does_not_make_affirmative_autonomous_mas_claims() -> None:
    readme = _read(README)

    forbidden_claims = [
        "Built a fully autonomous multi-agent system",
        "This project is a fully autonomous multi-agent system",
        "Agents autonomously negotiate",
        "多 Agent 自主协商",
    ]

    for claim in forbidden_claims:
        assert claim not in readme

    assert "not a fully autonomous multi-agent system" in readme
    assert "不是完全自治式多 Agent 系统" in readme


def test_docs_do_not_contain_fake_benchmark_numbers() -> None:
    for path in _all_docs():
        text = _read(path)
        for pattern in FAKE_BENCHMARK_PATTERNS:
            assert pattern.search(text) is None, path


def test_readme_has_normal_markdown_line_count() -> None:
    assert len(_read(README).splitlines()) >= 120


def test_key_docs_are_not_single_line_compressed() -> None:
    minimum_lines = {
        "harness_engineering_overview.md": 40,
        "module_map.md": 30,
        "artifact_walkthrough.md": 30,
        "benchmark_guide.md": 40,
        "demo_runbook.md": 70,
        "resume_alignment.md": 40,
        "interview_playbook.md": 50,
        "limitations_and_next_steps.md": 25,
        "architecture_overview.md": 50,
        "agent_harness_positioning.md": 40,
        "refactor_guardrails.md": 40,
    }

    for path in DOC_FILES:
        assert len(_read(path).splitlines()) >= minimum_lines[path.name], path


def test_readme_doc_links_exist() -> None:
    readme = _read(README)

    for target in README_DOC_LINKS:
        assert f"]({target})" in readme
        assert (ROOT / target).exists()


def test_core_markdown_fences_are_present() -> None:
    readme = _read(README)

    assert "```mermaid\n" in readme
    assert "```bash\n" in readme
    assert readme.count("```") % 2 == 0
