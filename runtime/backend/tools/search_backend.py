from __future__ import annotations

import logging
from urllib.parse import urlparse
from typing import Any

import httpx
from tavily import AsyncTavilyClient

import config

logger = logging.getLogger(__name__)

SEARXNG_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 DirectionAI/1.0"
    ),
}


def _normalize_search_item(item: dict[str, Any]) -> dict[str, str]:
    title = str(item.get("title") or item.get("content") or item.get("snippet") or "").strip()
    summary = str(item.get("content") or item.get("snippet") or item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    domain = str(item.get("domain") or "").strip()
    if not domain and url:
        try:
            domain = urlparse(url).netloc
        except Exception:
            domain = ""
    published_at = str(
        item.get("published_date")
        or item.get("published_at")
        or item.get("date")
        or ""
    ).strip()
    return {
        "title": title,
        "summary": summary,
        "url": url,
        "domain": domain,
        "published_at": published_at,
    }


class SearchBackend:
    def __init__(self) -> None:
        self.provider = config.SEARCH_PROVIDER
        self._tavily = (
            AsyncTavilyClient(api_key=config.TAVILY_API_KEY)
            if config.TAVILY_API_KEY and self.provider in {"auto", "tavily"}
            else None
        )
        self._has_searxng = bool(config.SEARXNG_BASE_URL) and self.provider in {"auto", "searxng"}

    @property
    def enabled(self) -> bool:
        return self._tavily is not None or self._has_searxng

    async def search_text(self, query: str, max_results: int = 3) -> list[str]:
        results = await self.search_text_results(query, max_results=max_results)
        return [item.get("summary", "") for item in results if item.get("summary")]

    async def search_text_results(self, query: str, max_results: int = 3) -> list[dict[str, str]]:
        errors: list[str] = []

        if self._tavily is not None:
            try:
                results = await self._search_text_tavily(query, max_results=max_results)
                logger.info(
                    "[SearchBackend] text query served by Tavily query=%r results=%d",
                    query,
                    len(results),
                )
                return results
            except Exception as exc:
                logger.warning(
                    "[SearchBackend] Tavily text search failed, falling back if available query=%r error=%s",
                    query,
                    exc,
                )
                errors.append(f"tavily: {exc}")

        if self._has_searxng:
            try:
                results = await self._search_text_searxng(query, max_results=max_results)
                logger.info(
                    "[SearchBackend] text query served by SearXNG query=%r results=%d",
                    query,
                    len(results),
                )
                return results
            except Exception as exc:
                logger.warning(
                    "[SearchBackend] SearXNG text search failed query=%r error=%s",
                    query,
                    exc,
                )
                errors.append(f"searxng: {exc}")

        if errors:
            logger.error(
                "[SearchBackend] text search failed on all providers query=%r details=%s",
                query,
                " | ".join(errors),
            )
            raise RuntimeError(" | ".join(errors))
        logger.info(
            "[SearchBackend] text search skipped because no provider is configured query=%r",
            query,
        )
        return []

    async def search_images(self, query: str, max_results: int = 5) -> list[str]:
        errors: list[str] = []

        if self._tavily is not None:
            try:
                urls = await self._search_images_tavily(query, max_results=max_results)
                if urls:
                    logger.info(
                        "[SearchBackend] image query served by Tavily query=%r results=%d",
                        query,
                        len(urls),
                    )
                    return urls
                logger.info(
                    "[SearchBackend] Tavily image search returned no results, falling back if available query=%r",
                    query,
                )
            except Exception as exc:
                logger.warning(
                    "[SearchBackend] Tavily image search failed, falling back if available query=%r error=%s",
                    query,
                    exc,
                )
                errors.append(f"tavily: {exc}")

        if self._has_searxng:
            try:
                urls = await self._search_images_searxng(query, max_results=max_results)
                logger.info(
                    "[SearchBackend] image query served by SearXNG query=%r results=%d",
                    query,
                    len(urls),
                )
                return urls
            except Exception as exc:
                logger.warning(
                    "[SearchBackend] SearXNG image search failed query=%r error=%s",
                    query,
                    exc,
                )
                errors.append(f"searxng: {exc}")

        if errors:
            logger.error(
                "[SearchBackend] image search failed on all providers query=%r details=%s",
                query,
                " | ".join(errors),
            )
            raise RuntimeError(" | ".join(errors))
        logger.info(
            "[SearchBackend] image search skipped because no provider is configured query=%r",
            query,
        )
        return []

    async def _search_text_tavily(self, query: str, max_results: int) -> list[dict[str, str]]:
        result = await self._tavily.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
        )
        items: list[dict[str, str]] = []
        for item in result.get("results", [])[:max_results]:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_search_item(item)
            if normalized["summary"] or normalized["title"] or normalized["url"]:
                items.append(normalized)
        return items

    async def _search_images_tavily(self, query: str, max_results: int) -> list[str]:
        result = await self._tavily.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_images=True,
        )
        urls: list[str] = []
        for item in result.get("images", []):
            url = item if isinstance(item, str) else item.get("url", "")
            if url:
                urls.append(url)
        return urls

    async def _search_text_searxng(self, query: str, max_results: int) -> list[dict[str, str]]:
        payload = await self._search_searxng(query, categories="general")
        snippets: list[dict[str, str]] = []
        for item in payload.get("results", [])[:max_results]:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_search_item(item)
            if normalized["summary"] or normalized["title"] or normalized["url"]:
                snippets.append(normalized)
        return snippets

    async def _search_images_searxng(self, query: str, max_results: int) -> list[str]:
        payload = await self._search_searxng(query, categories="images")
        urls: list[str] = []
        for item in payload.get("results", [])[:max_results]:
            if not isinstance(item, dict):
                continue
            for key in ("img_src", "thumbnail_src", "url"):
                candidate = item.get(key)
                if candidate:
                    urls.append(str(candidate))
                    break
        return urls

    async def _search_searxng(self, query: str, *, categories: str) -> dict[str, Any]:
        headers = dict(SEARXNG_BROWSER_HEADERS)
        if config.SEARXNG_API_KEY:
            headers["Authorization"] = f"Bearer {config.SEARXNG_API_KEY}"

        params = {
            "q": query,
            "format": "json",
            "categories": categories,
        }

        async with httpx.AsyncClient(timeout=config.SEARXNG_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{config.SEARXNG_BASE_URL}/search",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()
