# Standalone PPT Generation Backend

这是从 `DirectionAICloud/deerflow_backend` 拆出来的 PPT 生成后端，只保留终端/后端能力，不依赖 DeerFlow gateway、router、workload gate 或前端页面。

## 安装

```bash
cd /Users/sss/directionai/ppt_generation_backend
cp .env.example .env
uv sync
cd runtime
npm install
```

系统依赖：

- Node.js/npm，用于运行 `pptxgenjs`
- 可选：LibreOffice 和 `pdftoppm`，用于生成 PPT 预览图和视觉 QA

## 直接在终端生成

```bash
cd /Users/sss/directionai/ppt_generation_backend
uv run ppt-backend generate \
  --topic "大语言模型微调与对齐" \
  --min-slides 6 \
  --max-slides 8 \
  --image-mode off \
  --output-dir ./outputs
```

带文档生成：

```bash
uv run ppt-backend generate \
  --topic "第三章 组合逻辑电路 - 教师授课 PPT" \
  --document /path/to/chapter.pdf \
  --min-slides 8 \
  --max-slides 14 \
  --output-dir ./outputs
```

两阶段生成：

```bash
uv run ppt-backend outline \
  --topic "第三章 组合逻辑电路" \
  --document /path/to/chapter.pdf \
  --out ./outline.json

uv run ppt-backend from-outline \
  --topic "第三章 组合逻辑电路" \
  --outline ./outline.json \
  --image-mode off \
  --output-dir ./outputs
```

## 后端服务模式

```bash
uv run ppt-backend serve --host 127.0.0.1 --port 8010 --output-dir ./outputs
```

接口：

- `GET /health`
- `POST /upload_document`
- `POST /generate_ppt`
- `POST /stream_ppt_outline`
- `POST /stream_ppt_from_outline`
- `POST /stream_evaluate/ppt`
- `GET /download_ppt/{filename}`
- `GET /preview_ppt/{filename}/{image_name}`

## 目录说明

- `ppt_backend/`：独立 CLI 和 FastAPI 服务入口
- `runtime/`：原 PPT 生成 runtime、prompt 模板、vendor skill 和工作区
- `outputs/`：建议的独立输出目录，可由 `--output-dir` 或 `PPT_OUTPUT_DIR` 指定

