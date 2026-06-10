"""Parallel tasks tool definitions and registration.

This module defines the schema and tool classes for parallel sub-agent task
delegation. It provides:
- TaskSpec / ReduceSpec / ParallelTasksAction / ParallelTasksObservation
  (the action/observation models)
- ParallelTasksTool / ParallelTasksToolSet (registration and wiring)

`parallel_tasks` fans out multiple sub-agent tasks concurrently using a
shared system-prompt prefix that enables prompt-cache hits across all
parallel agents, and optionally runs a reduce step after they all finish.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from rich.text import Text

from openhands.sdk import ImageContent, TextContent
from openhands.sdk.subagent import get_factory_info, get_registered_agent_definitions
from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    register_tool,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState
    from openhands.tools.parallel_tasks.impl import ParallelTasksExecutor
    from openhands.tools.task.manager import ConfirmationHandler


# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class TaskSpec(BaseModel):
    """Specification for a single parallel sub-task."""

    prompt: str = Field(
        description="User message sent to this subagent. Keep it short: the "
        "shared_context already sets the scene.",
    )
    subagent_type: str = Field(
        default="general-purpose",
        description="Agent type to use for this task.",
    )
    description: str | None = Field(
        default=None,
        description="Short label for the task (used in progress display).",
    )


class ReduceSpec(BaseModel):
    """Specification for the optional reduce step."""

    prompt: str = Field(
        description="User message for the reducer. Task results are prepended "
        "automatically before this text.",
    )
    subagent_type: str = Field(
        default="general-purpose",
        description="Agent type to use for the reduce step.",
    )


class ParallelTasksAction(Action):
    """Schema for parallel sub-agent fan-out with optional reduce."""

    description: str | None = Field(
        default=None,
        description="Short label for the whole parallel_tasks invocation.",
    )
    shared_context: list[str] = Field(
        description=(
            "Context injected into every subagent's system prompt. Each item is "
            "either a literal string or a file path prefixed with '@' "
            "(e.g. '@/workspace/src/utils.py') that is read from disk at "
            "execution time."
        ),
    )
    tasks: list[TaskSpec] = Field(
        description="The tasks to run in parallel.",
    )
    reduce: ReduceSpec | None = Field(
        default=None,
        description=(
            "Optional reduce step. When set, a single additional subagent runs "
            "after all tasks finish, receiving all task results prepended to "
            "its prompt."
        ),
    )
    max_concurrency: SkipJsonSchema[int] = Field(
        default=4,
        ge=1,
        description="Maximum number of tasks to run concurrently.",
    )
    # Unique ID for this invocation; generated at parse time, not part of
    # the LLM-facing schema. Serialized in the ActionEvent so the backend
    # can use it as the parent_event_id for sub-task events.
    action_id: SkipJsonSchema[str] = Field(
        default_factory=lambda: str(uuid4()),
    )


class ParallelTasksObservation(Observation):
    """Observation from a parallel_tasks execution."""

    task_count: int = Field(description="Total number of tasks submitted.")
    completed: int = Field(description="Number of tasks that completed successfully.")
    failed: int = Field(description="Number of tasks that failed.")

    @property
    def visualize(self) -> Text:
        text = Text()
        status = "✅" if self.failed == 0 else "⚠️"
        text.append(
            f"{status} parallel_tasks: {self.completed}/{self.task_count} completed",
            style="bold",
        )
        if self.failed:
            text.append(f" ({self.failed} failed)", style="red")
        text.append("\n")
        if self.is_error:
            text.append("❌ ", style="red bold")
            text.append(self.ERROR_MESSAGE_HEADER, style="bold red")
        text.append(self.text)
        return text

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        llm_content: list[TextContent | ImageContent] = []
        if self.is_error:
            llm_content.append(TextContent(text=self.ERROR_MESSAGE_HEADER))
        llm_content.extend(self.content)
        return llm_content


# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------

PARALLEL_TASKS_DESCRIPTION: Final[str] = """Run multiple subagents in parallel, each handling an independent sub-task.

Use this tool to delegate one or more sub-tasks to subagents running a cheaper model.

Available agent types and the tools they have access to:
{agent_types_info}

`shared_context` is a list of strings injected into every subagent's system prompt before the agent definition's own instructions. Each item is either a literal string or a path prefixed with `@` (e.g. `@/workspace/src/utils.py`), which is read from disk at execution time. Use `@path` items to share large or stable content — files to be edited, coding conventions, architectural constraints — without re-generating those tokens in every task prompt. All subagents share this prefix, so prompt-cache hits apply from the second subagent onwards.

Each task's `prompt` is the user message sent to start that subagent's conversation. Keep it short: the shared context already sets the scene.

If `reduce` is provided, a single additional subagent runs after all tasks finish. It receives the same `shared_context` in its system prompt and all task results prepended to its `prompt`. Use it to merge, rank, or summarise the parallel outputs.

Note: tasks in `parallel_tasks` run concurrently — do not use it when each step depends on the previous result.

When to use `parallel_tasks`:
- The sub-task warrants a cheaper or fresher-context model — delegate even a single task
- Applying a consistent change across several independent files simultaneously
- Running parallel investigations whose results will be merged at the end

{parallel_tasks_examples}
"""  # noqa: E501

PARALLEL_TASKS_EXAMPLES: Final[dict[str, str]] = {
    "general-purpose": """
Example — Apply a rename across multiple independent files:
    description="Rename fetchData → retrieveInformation"
    shared_context=[
        "Rename the function `fetchData` to `retrieveInformation` everywhere in the assigned "
        "file. Update all call sites, type annotations, and JSDoc. Do not change behaviour.",
        "@/workspace/src/api/types.ts",
    ]
    tasks=[
        TaskSpec(prompt="Apply the rename in src/api/client.ts.",        subagent_type="general-purpose", description="client"),
        TaskSpec(prompt="Apply the rename in src/api/server.ts.",        subagent_type="general-purpose", description="server"),
        TaskSpec(prompt="Apply the rename in tests/api/client.test.ts.", subagent_type="general-purpose", description="tests"),
    ]
""",  # noqa: E501
    "code-explorer": """
Example — Audit several files then summarise findings:
    description="Error-handling audit"
    shared_context=[
        "Audit the assigned file for unhandled promise rejections and silent catch blocks. "
        "Report each as: file path, line number, code snippet, severity (high/medium/low). "
        "Make no changes.",
    ]
    tasks=[
        TaskSpec(prompt="Audit src/auth/login.ts",        subagent_type="code-explorer", description="auth"),
        TaskSpec(prompt="Audit src/payments/checkout.ts", subagent_type="code-explorer", description="payments"),
        TaskSpec(prompt="Audit src/api/gateway.ts",       subagent_type="code-explorer", description="gateway"),
    ]
    reduce=ReduceSpec(
        prompt="Merge the per-file findings into a single prioritised list grouped by severity.",
        subagent_type="general-purpose",
    )
""",  # noqa: E501
}


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------


class ParallelTasksTool(ToolDefinition[ParallelTasksAction, ParallelTasksObservation]):
    """Tool for running multiple sub-agent tasks in parallel."""

    def declared_resources(self, action: Action) -> DeclaredResources:  # noqa: ARG002
        return DeclaredResources(keys=(), declared=True)

    @classmethod
    def create(
        cls,
        executor: "ParallelTasksExecutor",
        description: str,
    ) -> Sequence["ParallelTasksTool"]:
        return [
            cls(
                action_type=ParallelTasksAction,
                observation_type=ParallelTasksObservation,
                description=description,
                annotations=ToolAnnotations(
                    title="parallel_tasks",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
                executor=executor,
            )
        ]


class ParallelTasksToolSet(
    ToolDefinition[ParallelTasksAction, ParallelTasksObservation]
):
    """Entry point that creates and wires up a ParallelTasksExecutor.

    Usage::

        from openhands.tools.parallel_tasks import ParallelTasksToolSet

        agent = Agent(
            llm=llm,
            tools=[
                Tool(name=TerminalTool.name),
                Tool(name=FileEditorTool.name),
                Tool(name=ParallelTasksToolSet.name),
            ],
        )
    """

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",  # noqa: ARG003
        confirmation_handler: "ConfirmationHandler | None" = None,
    ) -> list[ToolDefinition]:
        from openhands.tools.parallel_tasks.impl import ParallelTasksExecutor
        from openhands.tools.task.manager import TaskManager

        agent_types_info = get_factory_info()

        registered = {d.name for d in get_registered_agent_definitions()}
        parallel_tasks_examples = "\n".join(
            ex for name, ex in PARALLEL_TASKS_EXAMPLES.items() if name in registered
        )

        description = PARALLEL_TASKS_DESCRIPTION.format(
            agent_types_info=agent_types_info,
            parallel_tasks_examples=parallel_tasks_examples,
        )

        manager = TaskManager(confirmation_handler=confirmation_handler)
        executor = ParallelTasksExecutor(manager=manager)

        tools: list[ToolDefinition] = []
        tools.extend(
            ParallelTasksTool.create(executor=executor, description=description)
        )
        return tools


# Automatically register when this module is imported
register_tool(ParallelTasksToolSet.name, ParallelTasksToolSet)
register_tool(ParallelTasksTool.name, ParallelTasksTool)
