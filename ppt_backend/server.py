"""Backend-only FastAPI app for the standalone PPT generator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .runtime_bridge import get_runtime_api_module

api = get_runtime_api_module()

if os.getenv("PPT_OUTPUT_DIR"):
    output_root = Path(os.environ["PPT_OUTPUT_DIR"]).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    api.OUTPUT_ROOT = output_root
    api.config.OUTPUT_DIR = str(output_root)

app = FastAPI(title="Standalone PPT Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/upload_document")
async def upload_document(
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
) -> Any:
    return await api.upload_document_route(files=files, file=file)


@app.post("/generate_ppt")
def generate_ppt(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        req = api.PPTGenerationRequest.model_validate(payload)
        return api.generate_ppt_bundle(req).to_response()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/stream_ppt_outline")
async def stream_ppt_outline(payload: dict[str, Any], request: Request) -> StreamingResponse:
    req = api.PPTGenerationRequest.model_validate(payload)
    return await api.stream_ppt_outline_route(req, request)


@app.post("/stream_ppt_from_outline")
async def stream_ppt_from_outline(payload: dict[str, Any], request: Request) -> StreamingResponse:
    req = api.PPTGenerationFromOutlineRequest.model_validate(payload)
    return await api.stream_ppt_from_outline_route(req, request)


@app.post("/stream_evaluate/ppt")
async def stream_evaluate_ppt(payload: dict[str, Any], request: Request) -> StreamingResponse:
    req = api.PPTEvaluationRequest.model_validate(payload)
    return await api.stream_evaluate_ppt_route(req, request)


@app.get("/download_ppt/{filename}")
def download_ppt(filename: str) -> FileResponse:
    return api.download_ppt_route(filename)


@app.get("/preview_ppt/{filename}/{image_name}")
def preview_ppt(filename: str, image_name: str) -> FileResponse:
    return api.preview_ppt_image_route(filename, image_name)

