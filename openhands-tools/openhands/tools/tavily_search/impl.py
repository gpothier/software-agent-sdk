"""Tavily web search tool executor."""

import os
from typing import TYPE_CHECKING

import httpx

from openhands.sdk import TextContent
from openhands.sdk.logger import get_logger
from openhands.sdk.tool import ToolExecutor
from openhands.tools.tavily_search.definition import (
    SearchResult,
    TavilySearchAction,
    TavilySearchObservation,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation


logger = get_logger(__name__)

_DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
_SECRET_NAME = "TAVILY_API_KEY"


def _search_url() -> str:
    """Resolve the Tavily search endpoint.

    Defaults to the public Tavily API. ``TAVILY_API_BASE_URL`` can override the
    base (e.g. a self-hosted proxy, or a fake endpoint in tests).
    """
    base = os.environ.get("TAVILY_API_BASE_URL", _DEFAULT_TAVILY_BASE_URL).rstrip("/")
    return f"{base}/search"


class TavilySearchExecutor(ToolExecutor[TavilySearchAction, TavilySearchObservation]):
    """Executor that queries the Tavily search API."""

    def _get_api_key(self, conversation: "LocalConversation | None") -> str | None:
        if conversation is not None:
            try:
                key = conversation.state.secret_registry.get_secret_value(_SECRET_NAME)
                if key:
                    return key
            except Exception:
                logger.debug(
                    "Could not read TAVILY_API_KEY from secret registry",
                    exc_info=True,
                )
        return os.environ.get(_SECRET_NAME)

    def __call__(
        self,
        action: TavilySearchAction,
        conversation: "LocalConversation | None" = None,
    ) -> TavilySearchObservation:
        api_key = self._get_api_key(conversation)
        if not api_key:
            return TavilySearchObservation.from_text(
                text=(
                    "TAVILY_API_KEY is not configured. Set a Tavily API key in the "
                    "search provider settings to enable web search."
                ),
                is_error=True,
                query=action.query,
            )

        try:
            response = httpx.post(
                _search_url(),
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "query": action.query,
                    "search_depth": action.search_depth,
                    "max_results": action.max_results,
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return TavilySearchObservation.from_text(
                text=f"Tavily API error ({e.response.status_code}): {e.response.text}",
                is_error=True,
                query=action.query,
            )
        except httpx.HTTPError as e:
            return TavilySearchObservation.from_text(
                text=f"Failed to reach Tavily API: {e}",
                is_error=True,
                query=action.query,
            )

        data = response.json()
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
                score=item.get("score"),
            )
            for item in data.get("results", [])
        ]

        return TavilySearchObservation(
            content=[TextContent(text=self._format(action.query, results))],
            results=results,
            query=action.query,
        )

    @staticmethod
    def _format(query: str, results: list[SearchResult]) -> str:
        if not results:
            return f"No results found for query: {query!r}"
        lines = [f"Search results for {query!r}:", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   URL: {r.url}")
            if r.score is not None:
                lines.append(f"   Score: {r.score:.3f}")
            if r.content:
                lines.append(f"   {r.content}")
            lines.append("")
        return "\n".join(lines).rstrip()
