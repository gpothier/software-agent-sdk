"""Tavily web search tool definition."""

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    register_tool,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


class SearchResult(BaseModel):
    """A single web search result."""

    title: str = Field(description="Title of the page")
    url: str = Field(description="URL of the page")
    content: str = Field(
        description="Snippet or extracted content relevant to the query"
    )
    score: float | None = Field(
        default=None, description="Relevance score from the provider"
    )


class TavilySearchAction(Action):
    """Schema for a Tavily web search."""

    query: str = Field(description="The search query.")
    search_depth: Literal["basic", "advanced"] = Field(
        default="basic",
        description=(
            "Search depth. 'basic' returns fast, cheaper snippet results; "
            "'advanced' returns fuller content and is slower / more expensive."
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Maximum number of results to return (1-20). Prefer the default (5) "
            "for targeted lookups; use a higher value for broad research."
        ),
    )


class TavilySearchObservation(Observation):
    """Observation returned by a Tavily web search."""

    results: list[SearchResult] = Field(
        default_factory=list, description="The search results."
    )
    query: str = Field(default="", description="The query that was searched.")


TOOL_DESCRIPTION = """Real-time web search via the Tavily API.
* Use this to look up current information on the web that may not be in your training data.
* search_depth: \"basic\" = fast, cheaper snippet results; \"advanced\" = fuller content, slower and more expensive.
* max_results: the default of 5 is good for targeted lookups; increase it (up to 20) for broad research.
* Returns a ranked list of results with title, URL and a content snippet.
"""  # noqa: E501


class TavilySearchTool(ToolDefinition[TavilySearchAction, TavilySearchObservation]):
    """Web search tool backed by the Tavily API."""

    @classmethod
    def is_usable(cls) -> bool:
        """Usable when a Tavily API key is present in the environment.

        Note: OpenFeet injects the key per-conversation via the secret registry,
        so the executor also reads it from the conversation at runtime.
        """
        return bool(os.environ.get("TAVILY_API_KEY"))

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",  # noqa: ARG003
    ) -> Sequence["TavilySearchTool"]:
        from openhands.tools.tavily_search.impl import TavilySearchExecutor

        return [
            cls(
                description=TOOL_DESCRIPTION,
                action_type=TavilySearchAction,
                observation_type=TavilySearchObservation,
                annotations=ToolAnnotations(
                    title="tavily_search",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
                executor=TavilySearchExecutor(),
            )
        ]


register_tool(TavilySearchTool.name, TavilySearchTool)
