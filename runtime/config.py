import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
DOCS_DIR = PROJECT_ROOT / "docs"
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
BENCHMARKS_DIR = WORKSPACE_DIR / "benchmarks"
RUNTIME_MEMORY_DIR = WORKSPACE_DIR / "runtime_memory"
LEGACY_RUNTIME_SKILLS_DIR = WORKSPACE_DIR / "runtime_skills"


def _normalize_openai_base_url(url: str) -> str:
    """
    OpenAI-compatible SDK expects a base URL like:
    https://openrouter.ai/api/v1
    rather than a full endpoint like:
    https://openrouter.ai/api/v1/chat/completions
    """
    normalized = (url or "").strip().rstrip("/")
    for suffix in (
        "/chat/completions",
        "/completions",
        "/responses",
    ):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized

DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "minimax-m2.7-highspeed"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_QWEN_MODEL = "qwen3.6-plus"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = _normalize_openai_base_url(os.getenv("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL))
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", DEFAULT_MINIMAX_MODEL)

QWEN_API_KEY = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
QWEN_BASE_URL = _normalize_openai_base_url(os.getenv("QWEN_BASE_URL", DEFAULT_QWEN_BASE_URL))
QWEN_MODEL = os.getenv("QWEN_MODEL", DEFAULT_QWEN_MODEL)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _normalize_openai_base_url(os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL))
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY",
    os.getenv("CLAUDE_API_KEY", os.getenv("RESEARCH_API_KEY", "")),
)
OPENROUTER_BASE_URL = _normalize_openai_base_url(
    os.getenv(
        "OPENROUTER_BASE_URL",
        os.getenv("CLAUDE_BASE_URL", os.getenv("RESEARCH_BASE_URL", DEFAULT_OPENROUTER_BASE_URL)),
    )
)
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    os.getenv("CLAUDE_MODEL", os.getenv("RESEARCH_MODEL", "")),
)

GLM_API_KEY = os.getenv("GLM_API_KEY", MINIMAX_API_KEY)
GLM_BASE_URL = _normalize_openai_base_url(os.getenv("GLM_BASE_URL", MINIMAX_BASE_URL))

# Planner Agent (can be configured independently from GLM / Research)
PLANNER_API_KEY = os.getenv("PLANNER_API_KEY", MINIMAX_API_KEY or GLM_API_KEY)
PLANNER_BASE_URL = _normalize_openai_base_url(os.getenv("PLANNER_BASE_URL", GLM_BASE_URL))
PLANNER_MODEL = os.getenv("PLANNER_MODEL", MINIMAX_MODEL)
MAX_TOKENS_PLANNER = 32768
PLANNER_REQUEST_TIMEOUT = float(os.getenv("PLANNER_REQUEST_TIMEOUT", "300"))

# Research Agent (Tavily)
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_BASE_URL = "https://api.tavily.com"
SEARCH_PROVIDER = (os.getenv("SEARCH_PROVIDER", "auto").strip().lower() or "auto")
_SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "").strip()
if not _SEARXNG_BASE_URL and SEARCH_PROVIDER in {"auto", "searxng"}:
    _SEARXNG_BASE_URL = "http://searxng:8080"
SEARXNG_BASE_URL = _SEARXNG_BASE_URL.rstrip("/")
SEARXNG_API_KEY = os.getenv("SEARXNG_API_KEY", "").strip()
SEARXNG_TIMEOUT = float(os.getenv("SEARXNG_TIMEOUT", "20"))
RESEARCH_API_KEY = os.getenv("RESEARCH_API_KEY", PLANNER_API_KEY)
RESEARCH_BASE_URL = _normalize_openai_base_url(os.getenv("RESEARCH_BASE_URL", PLANNER_BASE_URL))
RESEARCH_MODEL = os.getenv("RESEARCH_MODEL", PLANNER_MODEL)
MAX_TOKENS_RESEARCHER = 4096


def get_llm_provider_settings(provider: str | None) -> dict[str, str]:
    normalized = (provider or "minmax").strip().lower()

    if normalized in {"qwen", "dashscope", "qwen3.6-plus"}:
        return {
            "provider": "qwen",
            "provider_id": "qwen",
            "api_key": QWEN_API_KEY or PLANNER_API_KEY,
            "base_url": QWEN_BASE_URL or PLANNER_BASE_URL,
            "model": QWEN_MODEL,
            "model_id": QWEN_MODEL,
        }

    if normalized in {"deepseek", "deepseek-v4-pro"}:
        return {
            "provider": "deepseek",
            "provider_id": "deepseek",
            "api_key": DEEPSEEK_API_KEY or PLANNER_API_KEY,
            "base_url": DEEPSEEK_BASE_URL or PLANNER_BASE_URL,
            "model": DEEPSEEK_MODEL,
            "model_id": DEEPSEEK_MODEL,
        }

    if normalized == "claude":
        return {
            "provider": "claude",
            "provider_id": "claude",
            "api_key": OPENROUTER_API_KEY or RESEARCH_API_KEY or PLANNER_API_KEY,
            "base_url": OPENROUTER_BASE_URL or RESEARCH_BASE_URL or PLANNER_BASE_URL,
            "model": OPENROUTER_MODEL or RESEARCH_MODEL or PLANNER_MODEL,
            "model_id": OPENROUTER_MODEL or RESEARCH_MODEL or PLANNER_MODEL,
        }

    return {
        "provider": "minmax",
        "provider_id": "minmax",
        "api_key": MINIMAX_API_KEY or PLANNER_API_KEY or GLM_API_KEY,
        "base_url": MINIMAX_BASE_URL or PLANNER_BASE_URL or GLM_BASE_URL,
        "model": MINIMAX_MODEL or PLANNER_MODEL,
        "model_id": MINIMAX_MODEL or PLANNER_MODEL,
    }

# 幻灯片尺寸（英寸，16:9）
SLIDE_WIDTH_INCH = 13.333
SLIDE_HEIGHT_INCH = 7.5
MAX_PPT_SLIDES = int(os.getenv("MAX_PPT_SLIDES", "60"))

OUTPUT_DIR = str(WORKSPACE_DIR / "outputs")
ASSETS_DIR = str(WORKSPACE_DIR / "assets")

# Unsplash
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
UNSPLASH_BASE_URL = "https://api.unsplash.com"

# 豆包图片生成
IMAGE_PROVIDER = (os.getenv("IMAGE_PROVIDER", "minimax").strip().lower() or "minimax")
MINIMAX_IMAGE_BASE_URL = _normalize_openai_base_url(os.getenv("MINIMAX_IMAGE_BASE_URL", MINIMAX_BASE_URL))
MINIMAX_IMAGE_MODEL = os.getenv("MINIMAX_IMAGE_MODEL", "image-01")
MINIMAX_IMAGE_ASPECT_RATIO = os.getenv("MINIMAX_IMAGE_ASPECT_RATIO", "16:9")
MINIMAX_IMAGE_RESPONSE_FORMAT = os.getenv("MINIMAX_IMAGE_RESPONSE_FORMAT", "url")
MINIMAX_IMAGE_PROMPT_OPTIMIZER = os.getenv("MINIMAX_IMAGE_PROMPT_OPTIMIZER", "true").strip().lower() in {"1", "true", "yes", "on"}
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_BASE_URL = _normalize_openai_base_url("https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_IMAGE_MODEL = os.getenv("DOUBAO_IMAGE_MODEL", "doubao-seedream-4-5-251128")
DOUBAO_IMAGE_SIZE = os.getenv("DOUBAO_IMAGE_SIZE", "2K")

# Qwen-VL 视觉评估
_BOOK_GENERATION_QWEN_FALLBACK = os.getenv("DIRECTIONAI_BOOK_GENERATION_QWEN_FALLBACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
QWEN_VL_API_KEY = os.getenv("QWEN_VL_API_KEY") or QWEN_API_KEY
if not QWEN_VL_API_KEY and _BOOK_GENERATION_QWEN_FALLBACK:
    QWEN_VL_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN_VL_BASE_URL = _normalize_openai_base_url(
    os.getenv(
        "QWEN_VL_BASE_URL",
        DEFAULT_QWEN_BASE_URL if _BOOK_GENERATION_QWEN_FALLBACK else "",
    )
)
QWEN_VL_MODEL = os.getenv("QWEN_VL_MODEL", "qwen-vl-max")
EVAL_SCORE_THRESHOLD = float(os.getenv("EVAL_SCORE_THRESHOLD", "3.0"))
EVAL_MAX_ROUNDS = int(os.getenv("EVAL_MAX_ROUNDS", "2"))
