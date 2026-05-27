from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SELF = ROOT / "tests/test_documentation_alignment.py"

DOC_MIN_LINES = {
    "docs/harness_engineering_overview.md": 40,
    "docs/module_map.md": 40,
    "docs/artifact_walkthrough.md": 40,
    "docs/benchmark_guide.md": 40,
    "docs/demo_runbook.md": 40,
    "docs/resume_alignment.md": 40,
    "docs/interview_playbook.md": 60,
    "docs/limitations_and_next_steps.md": 40,
    "docs/architecture_overview.md": 50,
    "docs/agent_harness_positioning.md": 30,
    "docs/refactor_guardrails.md": 30,
}

README_DOC_LINKS = [
    "docs/run_full_flow_locally.md",
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
    re.compile(r"improved by\s*\d+%", re.IGNORECASE),
    re.compile(r"success rate from\s*\d+%\s*to\s*\d+%", re.IGNORECASE),
    re.compile(r"\d+%\s*->\s*\d+%"),
]

README_SECTIONS = [
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _doc_paths() -> list[Path]:
    return [ROOT / name for name in DOC_MIN_LINES]


def _all_markdown_paths() -> list[Path]:
    return [README, *_doc_paths()]


def _lines(path: Path) -> list[str]:
    return _read(path).splitlines()


def test_readme_has_real_markdown_line_breaks() -> None:
    lines = _lines(README)

    assert len(lines) >= 120
    assert lines[0].strip() == "# PPT Generation Agent Harness"


def test_readme_does_not_use_old_title_or_local_paths() -> None:
    text = _read(README)

    assert "# Standalone PPT Generation Backend" not in text
    for marker in LOCAL_PATH_MARKERS:
        assert marker not in text


def test_readme_does_not_have_compressed_heading_lines() -> None:
    text = _read(README)

    assert "# PPT Generation Agent Harness Evaluation-and-repair" not in text
    assert "## Project Positioning This project" not in text

    for line in text.splitlines():
        assert line.count("## ") <= 1


def test_readme_required_sections_are_standalone_headings() -> None:
    lines = set(_lines(README))

    for section in README_SECTIONS:
        assert section in lines


def test_readme_mermaid_block_is_multiline() -> None:
    text = _read(README)

    assert "```mermaid\nflowchart TD\n" in text
    assert "```mermaid flowchart TD" not in text


def test_readme_bash_blocks_are_multiline_and_install_command_is_valid() -> None:
    text = _read(README)

    assert "```bash\ncd <your-local-checkout>\n" in text
    assert "cp .env.example .env" in text
    assert "uv sync" in text
    assert "```bash cd" not in text
    assert "cd  cp .env.example" not in text
    assert "cd cp .env.example" not in text


def test_readme_capability_map_is_real_markdown_table() -> None:
    lines = _lines(README)

    assert "| Layer | Module Path | Purpose | Key Artifacts |" in lines
    assert "|---|---|---|---|" in lines


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


def test_docs_have_required_raw_line_counts() -> None:
    for path_name, min_lines in DOC_MIN_LINES.items():
        path = ROOT / path_name
        assert len(_lines(path)) >= min_lines, path_name


def test_docs_do_not_have_compressed_heading_lines() -> None:
    for path in _all_markdown_paths():
        for line in _lines(path):
            assert line.count("## ") <= 1, path


def test_markdown_code_fences_are_not_inline_blobs() -> None:
    for path in _all_markdown_paths():
        for line in _lines(path):
            stripped = line.strip()
            if stripped.startswith("```"):
                parts = stripped.split(maxsplit=1)
                assert len(parts) == 1, path


def test_docs_do_not_contain_local_absolute_paths() -> None:
    for path in _all_markdown_paths():
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
    for path in _all_markdown_paths():
        text = _read(path)
        for pattern in FAKE_BENCHMARK_PATTERNS:
            assert pattern.search(text) is None, path


def test_readme_doc_links_exist() -> None:
    readme = _read(README)

    for target in README_DOC_LINKS:
        assert f"]({target})" in readme
        assert (ROOT / target).exists()


def test_resume_alignment_has_required_copy_ready_sections() -> None:
    text = _read(ROOT / "docs/resume_alignment.md")

    required = [
        "## 中文项目名",
        "## 一句话",
        "## 简历 Bullet 中文版",
        "## English Resume Bullets",
        "## Do Not Say",
        "## Quantitative Template",
    ]

    for heading in required:
        assert heading in text


def test_interview_playbook_has_required_explanations() -> None:
    text = _read(ROOT / "docs/interview_playbook.md")

    required = [
        "## Two-Minute Version",
        "## Ten-Minute Version",
        "## Questions and Answers",
        "Why not build a fully autonomous multi-agent system?",
        "What is the boundary between AgentRuntime and ToolRuntime?",
        "How is memory designed?",
        "How do you avoid repair memory pollution?",
        "How do you prove PPT quality improved?",
        "Why does the Replanner not use an LLM?",
    ]

    for phrase in required:
        assert phrase in text

    assert text.count("### ") >= 10


def test_limitations_doc_states_current_boundaries_in_chinese() -> None:
    text = _read(ROOT / "docs/limitations_and_next_steps.md")

    required = [
        "Orchestrator 主流程尚未迁移到 AgentExecutor。",
        "Post-run harness 是 offline integration。",
        "Repair plan 默认不自动执行。",
        "Replan patch 默认不自动应用。",
        "Benchmark 默认 offline，不调用真实生成。",
        "Memory 是 JSONL + lexical retrieval，不是 vector DB。",
        "Semantic memory 不自动从 LLM 总结。",
        "没有 Trace Viewer UI。",
        "没有真实长期线上数据。",
        "没有真实 benchmark 数字时不应写量化提升。",
    ]

    for phrase in required:
        assert phrase in text


def test_markdown_files_are_not_one_line_blobs() -> None:
    for path in _all_markdown_paths():
        lines = _lines(path)
        max_line_len = max((len(line) for line in lines), default=0)

        assert len(lines) > 10, path
        assert max_line_len < 400, path


def test_documentation_alignment_test_file_is_multiline_python() -> None:
    lines = _lines(SELF)
    max_line_len = max((len(line) for line in lines), default=0)

    assert len(lines) >= 100
    assert lines[0] == "from __future__ import annotations"
    assert max_line_len < 140
