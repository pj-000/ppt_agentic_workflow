"""
agents/asset_agent.py

为 PPT 每页获取配图，支持三种模式：
- image_source="search"   : 仅检索搜图，搜不到则跳过
- image_source="generate" : 仅生成，不搜索
- image_source="auto"     : 搜图优先，搜不到降级生成（默认）
"""
import asyncio
import base64
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

import httpx

import config
from backend.harness.runtime import HarnessTrace, PromptComposer, RepairOrchestrator, SkillContext, SkillRuntime
from backend.models.schemas import SlideLayout, SlideOutline, VisualMode, resolve_visual_mode
from backend.tools.search_backend import SearchBackend

logger = logging.getLogger(__name__)

SKIP_LAYOUTS = {SlideLayout.COVER, SlideLayout.CLOSING, SlideLayout.TOC}
BLOCKED_STOCK_DOMAINS = (
    "dreamstime.com",
    "shutterstock.com",
    "gettyimages.com",
    "istockphoto.com",
    "alamy.com",
    "123rf.com",
    "depositphotos.com",
    "vectorstock.com",
)
BLOCKED_URL_KEYWORDS = (
    "watermark",
    "logo",
    "thumbnail",
    "thumb",
    "preview",
    "comp",
)


@dataclass
class SearchAttemptResult:
    image_url: str | None = None
    error: str = ""
    error_signature: str = ""
    provider: str = ""


class AssetAgent:
    """
    并发为大纲每页搜索或生成配图，返回本地路径列表。
    列表长度与 slides 一致，无图片的页为 None。
    """

    def __init__(self, image_source: str = "auto", harness_trace: HarnessTrace | None = None):
        """
        image_source:
          "auto"     — 搜图优先，搜不到降级生成
          "search"   — 仅检索搜图
          "generate" — 仅生成
        """
        self.image_source = image_source
        self.image_provider = config.IMAGE_PROVIDER
        self.harness_trace = harness_trace
        self.search_backend = SearchBackend() if image_source in ("auto", "search") else None
        self._composer = PromptComposer()
        runtime_candidate = getattr(self._composer, "runtime", None)
        self._skill_runtime = runtime_candidate if isinstance(runtime_candidate, SkillRuntime) else SkillRuntime()
        self._repair_orchestrator = RepairOrchestrator(
            self._skill_runtime,
            run_id=uuid.uuid4().hex[:8],
            phase="visual-production",
        )
        self._image_prompt_template = self._composer.load_image_generation_prompt_template()

    def _record_loaded_guidance(
        self,
        *,
        stage: str,
        mode: str,
        context: SkillContext,
        promoted_records: Sequence[object],
        runtime_records: Sequence[object],
        attempt: int | None = None,
        error_signature: str = "",
    ) -> None:
        if not self.harness_trace:
            return

        loaded_records = [
            *self._skill_runtime._promoted_records_to_trace(list(promoted_records)),
            *self._skill_runtime._runtime_records_to_trace(list(runtime_records)),
        ]
        if not loaded_records:
            return

        self.harness_trace.record(
            stage=stage,
            payload={
                "mode": mode,
                "context": context.to_dict(),
                "attempt": attempt,
                "error_signature": error_signature,
                "text_present": False,
                "runtime_memory_ids": [getattr(item, "memory_id", "") for item in runtime_records if getattr(item, "memory_id", "")],
                "records": [item.to_dict() for item in loaded_records],
            },
        )

    @staticmethod
    def _parse_bool_token(value: str) -> bool | None:
        lowered = str(value or "").strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return None

    def _extract_generation_config(
        self,
        *,
        conditions: Sequence[str] | None = None,
        after_pattern: str = "",
    ) -> tuple[str | None, bool | None]:
        response_format: str | None = None
        prompt_optimizer: bool | None = None

        tokens = list(conditions or [])
        if after_pattern:
            tokens.extend(part.strip() for part in after_pattern.split(",") if part.strip())

        for token in tokens:
            raw = str(token or "").strip()
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "response_format" and value:
                response_format = value
            elif key == "prompt_optimizer":
                parsed = self._parse_bool_token(value)
                if parsed is not None:
                    prompt_optimizer = parsed

        return response_format, prompt_optimizer

    def _build_generation_attempt_plan(
        self,
        *,
        trigger_stage: str,
        layout_scope: str,
        visual_mode_scope: str,
        error_signature: str | None = None,
    ) -> tuple[list[tuple[str, bool]], list[str]]:
        attempts: list[tuple[str, bool]] = []
        loaded_repair_memory_ids: list[str] = []
        prompt_context = SkillContext(
            phase="visual-production",
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            provider=self.image_provider,
        )

        def add_attempt(response_format: str | None, prompt_optimizer: bool | None) -> None:
            rf = (response_format or "").strip().lower()
            if rf not in {"url", "base64"}:
                return
            po = bool(
                config.MINIMAX_IMAGE_PROMPT_OPTIMIZER if prompt_optimizer is None else prompt_optimizer
            )
            candidate = (rf, po)
            if candidate not in attempts:
                attempts.append(candidate)

        promoted_prevention = self._skill_runtime.match_promoted_lessons(
            phase="visual-production",
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
            max_items=self._skill_runtime.PREVENTION_MAX_ITEMS,
        )
        runtime_prevention = self._repair_orchestrator.prevention_matches(
            trigger_stage=trigger_stage,
            layout_scope=layout_scope,
            visual_mode_scope=visual_mode_scope,
        )
        self._record_loaded_guidance(
            stage=trigger_stage,
            mode="prevention",
            context=prompt_context,
            promoted_records=promoted_prevention,
            runtime_records=runtime_prevention,
        )
        for item in runtime_prevention:
            response_format, prompt_optimizer = self._extract_generation_config(
                conditions=getattr(item, "conditions", []),
                after_pattern=getattr(item, "after_pattern", ""),
            )
            add_attempt(response_format, prompt_optimizer)

        add_attempt(config.MINIMAX_IMAGE_RESPONSE_FORMAT, config.MINIMAX_IMAGE_PROMPT_OPTIMIZER)
        add_attempt("base64", False)
        add_attempt("url", False)
        add_attempt("url", True)

        for item in promoted_prevention:
            response_format, prompt_optimizer = self._extract_generation_config(
                conditions=getattr(item, "conditions", []),
                after_pattern=getattr(item, "after_pattern", ""),
            )
            add_attempt(response_format, prompt_optimizer)

        if error_signature:
            promoted_repair = self._skill_runtime.match_promoted_lessons(
                phase="visual-production",
                trigger_stage=trigger_stage,
                error_signature=error_signature,
                layout_scope=layout_scope,
                visual_mode_scope=visual_mode_scope,
                max_items=self._skill_runtime.REPAIR_MAX_ITEMS,
            )
            runtime_repair = self._repair_orchestrator.repair_matches(
                trigger_stage=trigger_stage,
                error_signature=error_signature,
                layout_scope=layout_scope,
                visual_mode_scope=visual_mode_scope,
            )
            loaded_repair_memory_ids = [item.memory_id for item in runtime_repair]
            self._record_loaded_guidance(
                stage=trigger_stage,
                mode="repair",
                context=prompt_context,
                promoted_records=promoted_repair,
                runtime_records=runtime_repair,
                error_signature=error_signature,
            )
            for item in [*promoted_repair, *runtime_repair]:
                response_format, prompt_optimizer = self._extract_generation_config(
                    conditions=getattr(item, "conditions", []),
                    after_pattern=getattr(item, "after_pattern", ""),
                )
                add_attempt(response_format, prompt_optimizer)

        return attempts, loaded_repair_memory_ids

    async def fetch_all(
        self,
        slides: list[SlideOutline],
        job_id: str,
        concurrency: int = 3,
    ) -> list[Optional[str]]:
        asset_dir = Path(config.ASSETS_DIR) / job_id
        asset_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(concurrency)

        async def _bounded(slide: SlideOutline) -> Optional[str]:
            async with sem:
                return await self._fetch_for_slide(slide, asset_dir)

        results = await asyncio.gather(*[_bounded(s) for s in slides], return_exceptions=True)

        processed = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"[AssetAgent] 第 {i} 页图片获取异常: {r}")
                processed.append(None)
            else:
                processed.append(r)

        fetched = sum(1 for p in processed if p)
        print(f"[AssetAgent] 完成，{fetched}/{len(slides)} 页获取到图片（模式: {self.image_source}）")
        return processed

    async def _fetch_for_slide(self, slide: SlideOutline, asset_dir: Path) -> Optional[str]:
        if slide.layout in SKIP_LAYOUTS:
            return None

        effective_visual_mode = resolve_visual_mode(slide)

        if effective_visual_mode == VisualMode.JS_DIAGRAM:
            print(f"[AssetAgent] 第 {slide.slide_index} 页 visual_mode=js_diagram，跳过图片获取")
            return None

        query = slide.image_prompt.strip() if slide.image_prompt else f"{slide.topic} photo"
        cache_key = hashlib.md5(
            f"{self.image_source}:{effective_visual_mode.value}:{query}".encode()
        ).hexdigest()[:10]

        for ext in (".jpg", ".png"):
            cached = asset_dir / f"{cache_key}{ext}"
            if cached.exists() and cached.stat().st_size > 0:
                print(f"[AssetAgent] 第 {slide.slide_index} 页命中缓存")
                return str(cached)

        if effective_visual_mode == VisualMode.GENERATED_IMAGE:
            if self.image_source in ("auto", "generate"):
                return await self._try_generate(slide, asset_dir, cache_key)
            result, _ = await self._try_search(slide, asset_dir, cache_key, query)
            return result

        if self.image_source == "generate":
            return await self._try_generate(slide, asset_dir, cache_key)

        if self.image_source == "search":
            result, _ = await self._try_search(slide, asset_dir, cache_key, query)
            return result

        # auto: 先搜，搜不到再生成
        result, search_attempt = await self._try_search(slide, asset_dir, cache_key, query)
        if result:
            return result
        generated = await self._try_generate(slide, asset_dir, cache_key)
        if generated and search_attempt.error and search_attempt.error_signature:
            repair_instruction = self._repair_orchestrator.build_repair_instruction(
                error_signature=search_attempt.error_signature,
                error=search_attempt.error,
                layout_scope=search_attempt.provider or "search-disabled",
                visual_mode_scope="image_search",
            )
            self._repair_orchestrator.remember_success(
                trigger_stage="asset_search",
                error_signature=search_attempt.error_signature,
                error=search_attempt.error,
                repair_instruction=repair_instruction,
                layout_scope=search_attempt.provider or "search-disabled",
                visual_mode_scope="image_search",
                provider_scope=search_attempt.provider or "search-disabled",
                before_pattern=query[:200],
                after_pattern="fallback=generate-image",
                conditions=[f"provider={search_attempt.provider or 'none'}"],
            )
        return generated

    async def _try_search(self, slide, asset_dir, cache_key, query) -> tuple[Optional[str], SearchAttemptResult]:
        search_attempt = await self._search_image(query)
        if search_attempt.image_url:
            local_path = asset_dir / f"{cache_key}.jpg"
            if await self._download(search_attempt.image_url, local_path):
                print(f"[AssetAgent] 第 {slide.slide_index} 页 {self.search_backend.provider if self.search_backend else 'search'} 搜图成功")
                return str(local_path), search_attempt
        return None, search_attempt

    async def _try_generate(self, slide, asset_dir, cache_key) -> Optional[str]:
        local_path = asset_dir / f"{cache_key}_gen.png"
        prompt = self._make_image_prompt(slide.topic, slide.image_prompt)
        if await self._generate_image(prompt, local_path):
            print(f"[AssetAgent] 第 {slide.slide_index} 页{self.image_provider}生成成功")
            return str(local_path)
        return None

    async def _generate_image(self, prompt: str, output_path: Path) -> bool:
        if self.image_provider == "doubao":
            return await self._generate_doubao(prompt, output_path)
        return await self._generate_minimax(prompt, output_path)

    async def _search_image(self, query: str) -> SearchAttemptResult:
        try:
            if not self.search_backend:
                return SearchAttemptResult()
            images = await self.search_backend.search_images(query, max_results=5)
            for url in images:
                if self._is_usable_search_image_url(url) and any(
                    url.lower().split("?")[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
                ):
                    return SearchAttemptResult(
                        image_url=url,
                        provider=self.search_backend.provider,
                    )
            if images:
                for url in images:
                    if self._is_usable_search_image_url(url):
                        return SearchAttemptResult(
                            image_url=str(url),
                            provider=self.search_backend.provider,
                        )
        except Exception as e:
            logger.warning(f"[AssetAgent] 检索图片失败: {e}")
            error = f"检索图片失败: {e}"
            error_signature = self._repair_orchestrator.classify_error(error, stage="asset_search")
            return SearchAttemptResult(
                error=error,
                error_signature=error_signature,
                provider=self.search_backend.provider if self.search_backend else "search-disabled",
            )
        return SearchAttemptResult(provider=self.search_backend.provider if self.search_backend else "")

    @staticmethod
    def _is_usable_search_image_url(url: str | None) -> bool:
        if not url:
            return False
        lowered = str(url).strip().lower()
        if not lowered.startswith(("http://", "https://")):
            return False

        parsed = urlparse(lowered)
        host = parsed.netloc
        path = parsed.path or ""
        query = parsed.query or ""

        if any(domain in host for domain in BLOCKED_STOCK_DOMAINS):
            return False

        haystack = " ".join(part for part in (host, path, query) if part)
        if any(keyword in haystack for keyword in BLOCKED_URL_KEYWORDS):
            return False

        if path.endswith((".svg", ".gif")):
            return False

        return True

    async def _generate_doubao(self, prompt: str, output_path: Path) -> bool:
        if not config.ARK_API_KEY:
            logger.warning("[AssetAgent] ARK_API_KEY 未配置，无法使用豆包生图")
            return False
        try:
            from openai import AsyncOpenAI

            ark = AsyncOpenAI(
                api_key=config.ARK_API_KEY,
                base_url=config.ARK_BASE_URL,
            )
            response = await ark.images.generate(
                model=config.DOUBAO_IMAGE_MODEL,
                prompt=prompt,
                size=config.DOUBAO_IMAGE_SIZE,
                response_format="url",
            )
            url = response.data[0].url
            return await self._download(url, output_path)
        except Exception as e:
            logger.warning(f"[AssetAgent] 豆包生成失败: {e}")
        return False

    async def _generate_minimax(self, prompt: str, output_path: Path) -> bool:
        if not config.MINIMAX_API_KEY:
            logger.warning("[AssetAgent] MINIMAX_API_KEY 未配置，无法使用 MiniMax 生图")
            return False

        seen: set[tuple[str, bool]] = set()
        last_error = ""
        last_error_signature: str | None = None
        trigger_stage = "asset_generation"
        layout_scope = self.image_provider
        visual_mode_scope = "generated_image"

        while True:
            attempts, loaded_repair_memory_ids = self._build_generation_attempt_plan(
                trigger_stage=trigger_stage,
                layout_scope=layout_scope,
                visual_mode_scope=visual_mode_scope,
                error_signature=last_error_signature,
            )
            next_attempt = None
            for response_format, prompt_optimizer in attempts:
                key = (response_format, bool(prompt_optimizer))
                if key in seen:
                    continue
                next_attempt = key
                seen.add(key)
                break

            if next_attempt is None:
                break

            response_format, prompt_optimizer = next_attempt
            payload = {
                "model": config.MINIMAX_IMAGE_MODEL,
                "prompt": prompt,
                "aspect_ratio": config.MINIMAX_IMAGE_ASPECT_RATIO,
                "response_format": response_format,
                "n": 1,
                "prompt_optimizer": prompt_optimizer,
            }
            ok, error = await self._attempt_minimax_payload(payload, output_path)
            if ok:
                if last_error_signature:
                    repair_instruction = self._repair_orchestrator.build_repair_instruction(
                        error_signature=last_error_signature,
                        error=last_error,
                        layout_scope=layout_scope,
                        visual_mode_scope=visual_mode_scope,
                    )
                    self._repair_orchestrator.remember_success(
                        trigger_stage=trigger_stage,
                        error_signature=last_error_signature,
                        error=last_error,
                        repair_instruction=repair_instruction,
                        layout_scope=layout_scope,
                        visual_mode_scope=visual_mode_scope,
                        provider_scope=self.image_provider,
                        before_pattern=last_error[:200],
                        after_pattern=f"response_format={response_format},prompt_optimizer={prompt_optimizer}",
                        conditions=[f"response_format={response_format}", f"prompt_optimizer={prompt_optimizer}"],
                    )
                return True

            for memory_id in dict.fromkeys(loaded_repair_memory_ids):
                self._repair_orchestrator.mark_memory_failure(memory_id)
            last_error = error or last_error
            last_error_signature = self._repair_orchestrator.classify_error(
                last_error,
                stage=trigger_stage,
            )

        if last_error:
            logger.warning(last_error)
        else:
            logger.warning("[AssetAgent] MiniMax 生图失败: 未知错误")
        return False

    async def _attempt_minimax_payload(self, payload: dict, output_path: Path) -> tuple[bool, str]:
        url = f"{config.MINIMAX_IMAGE_BASE_URL.rstrip('/')}/image_generation"
        headers = {
            "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            return False, f"[AssetAgent] MiniMax 生图失败: {e}"

        image_data = data.get("data") or {}
        if payload["response_format"] == "base64":
            encoded = (image_data.get("image_base64") or [None])[0]
            if not encoded:
                return False, "[AssetAgent] MiniMax 生图响应缺少 image_base64"
            try:
                output_path.write_bytes(base64.b64decode(encoded))
                return True, ""
            except Exception as e:
                return False, f"[AssetAgent] MiniMax base64 解码失败: {e}"

        image_urls = image_data.get("image_urls") or []
        if not image_urls:
            return False, "[AssetAgent] MiniMax 生图响应缺少 image_urls"
        ok = await self._download(str(image_urls[0]), output_path)
        return (ok, "" if ok else "[AssetAgent] MiniMax 生图失败: 下载返回空结果")

    async def _download(self, url: str, output_path: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    output_path.write_bytes(r.content)
                    return True
                logger.warning(f"[AssetAgent] 下载失败 HTTP {r.status_code}: {url[:80]}")
        except Exception as e:
            logger.warning(f"[AssetAgent] 下载异常: {e}")
        return False

    def _make_image_prompt(self, topic: str, image_prompt: Optional[str] = None) -> str:
        base = image_prompt.strip() if image_prompt else topic
        return self._image_prompt_template.replace("{base}", base)
