"""Terminal interface for the standalone PPT generator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .runtime_bridge import get_runtime_api_module


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).expanduser().resolve().read_text(encoding="utf-8")


def _configure_output_dir(api: Any, output_dir: str | None) -> None:
    if not output_dir:
        return
    target = Path(output_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    api.OUTPUT_ROOT = target
    api.config.OUTPUT_DIR = str(target)


def _extract_documents(api: Any, paths: list[str]) -> tuple[list[str], list[str]]:
    document_names: list[str] = []
    document_texts: list[str] = []

    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {path}")
        text, _tables, _page_count = api.extract_document_content(str(path))
        if not str(text or "").strip():
            raise RuntimeError(f"Document text extraction failed: {path}")
        document_names.append(path.name)
        document_texts.append(text)

    return document_names, document_texts


def _base_payload(args: argparse.Namespace, api: Any) -> dict[str, Any]:
    document_names, document_texts = _extract_documents(api, args.document or [])
    content_parts = []
    if args.content:
        content_parts.append(args.content)
    if args.content_file:
        content_parts.append(_read_text(args.content_file))

    return {
        "topic": args.topic,
        "model_provider": args.model_provider,
        "output_language": args.language,
        "target_audience": args.audience,
        "style": args.style or "",
        "enable_web_search": args.web_search,
        "image_mode": args.image_mode,
        "min_slides": args.min_slides,
        "max_slides": args.max_slides,
        "debug_layout": args.debug_layout,
        "content": "\n\n".join(part for part in content_parts if part.strip()) or None,
        "document_names": document_names,
        "document_texts": document_texts,
    }


def _print_event(event: str, data: Any, *, json_events: bool) -> None:
    if json_events:
        print(json.dumps({"event": event, "data": data}, ensure_ascii=False), flush=True)
        return

    if event == "progress":
        message = data.get("message") if isinstance(data, dict) else str(data)
        step = data.get("step") if isinstance(data, dict) else None
        total = data.get("total") if isinstance(data, dict) else None
        prefix = f"[{step}/{total}] " if step is not None and total else ""
        print(prefix + str(message), flush=True)
    elif event == "stage_status" and isinstance(data, dict):
        print(f"[{data.get('label', 'stage')}] {data.get('message', '')}", flush=True)
    elif event == "error":
        print(f"[error] {data}", file=sys.stderr, flush=True)


def cmd_generate(args: argparse.Namespace) -> int:
    api = get_runtime_api_module()
    _configure_output_dir(api, args.output_dir)
    payload = _base_payload(args, api)
    req = api.PPTGenerationRequest.model_validate(payload)
    artifacts = api.generate_ppt_bundle(
        req,
        emit=lambda event, data: _print_event(event, data, json_events=args.json_events),
    )
    print(json.dumps(artifacts.to_response(), ensure_ascii=False, indent=2))
    return 0


def cmd_outline(args: argparse.Namespace) -> int:
    api = get_runtime_api_module()
    _configure_output_dir(api, args.output_dir)
    payload = _base_payload(args, api)
    req = api.PPTGenerationRequest.model_validate(payload)
    outline, research_results, sources = api._plan_outline_bundle(
        req,
        emit=lambda event, data: _print_event(event, data, json_events=args.json_events),
    )
    result = {
        "outline": outline.model_dump(mode="json"),
        "research_results": research_results,
        "sources": sources,
    }
    if args.out:
        target = Path(args.out).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(target))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_from_outline(args: argparse.Namespace) -> int:
    api = get_runtime_api_module()
    _configure_output_dir(api, args.output_dir)
    outline_payload = json.loads(Path(args.outline).expanduser().resolve().read_text(encoding="utf-8"))
    outline = outline_payload.get("outline", outline_payload)
    payload = _base_payload(args, api)
    payload["outline"] = outline
    req = api.PPTGenerationFromOutlineRequest.model_validate(payload)
    artifacts = api._generate_from_outline_bundle(
        req,
        emit=lambda event, data: _print_event(event, data, json_events=args.json_events),
    )
    print(json.dumps(artifacts.to_response(), ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    os.environ.setdefault("PPT_OUTPUT_DIR", str(Path(args.output_dir).expanduser().resolve()) if args.output_dir else "")
    import uvicorn

    uvicorn.run(
        "ppt_backend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(Path(__file__).resolve().parents[1]),
    )
    return 0


def _add_generation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--topic", required=True, help="PPT 主题")
    parser.add_argument("--content", default="", help="补充要求或原始内容")
    parser.add_argument("--content-file", help="从文本/Markdown 文件读取补充内容")
    parser.add_argument("--document", action="append", help="可重复传入 PDF/DOCX/DOC/MD/PPTX 文档")
    parser.add_argument("--min-slides", type=int, default=6)
    parser.add_argument("--max-slides", type=int, default=10)
    parser.add_argument("--language", default="中文")
    parser.add_argument("--audience", default="general")
    parser.add_argument("--style", default="")
    parser.add_argument("--model-provider", choices=["minmax", "claude"], default="minmax")
    parser.add_argument("--image-mode", choices=["generate", "search", "auto", "off"], default="generate")
    parser.add_argument("--web-search", action="store_true")
    parser.add_argument("--debug-layout", action="store_true")
    parser.add_argument("--output-dir", help="PPT 输出目录，默认 runtime/workspace/outputs")
    parser.add_argument("--json-events", action="store_true", help="按 JSONL 输出进度事件")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ppt-backend", description="Standalone PPT generation backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="直接生成 PPT")
    _add_generation_options(generate)
    generate.set_defaults(func=cmd_generate)

    outline = subparsers.add_parser("outline", help="只生成可编辑大纲 JSON")
    _add_generation_options(outline)
    outline.add_argument("--out", help="保存大纲 JSON 的路径")
    outline.set_defaults(func=cmd_outline)

    from_outline = subparsers.add_parser("from-outline", help="从已确认大纲生成 PPT")
    _add_generation_options(from_outline)
    from_outline.add_argument("--outline", required=True, help="outline 命令导出的 JSON 文件")
    from_outline.set_defaults(func=cmd_from_outline)

    serve = subparsers.add_parser("serve", help="启动无前端 FastAPI 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--output-dir", help="PPT 输出目录")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

