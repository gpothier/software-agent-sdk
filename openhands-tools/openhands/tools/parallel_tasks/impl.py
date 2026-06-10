"""Parallel tasks executor.

This module implements `ParallelTasksExecutor`, which:
1. Resolves `@path` items in `shared_context` by reading files from disk.
2. Builds an augmented system prompt for each subagent:
   ``resolved_shared_context + agent_definition.system_prompt``
3. Runs all tasks concurrently via a `ThreadPoolExecutor`, each in its
   own `LocalConversation`.
4. Optionally runs a single reduce step after all tasks finish.
5. Aggregates subagent metrics into the parent conversation.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Final

from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation.response_utils import get_agent_final_response
from openhands.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.logger import get_logger
from openhands.sdk.subagent.registry import AgentFactory, get_agent_factory
from openhands.sdk.tool.tool import ToolExecutor
from openhands.tools.parallel_tasks.definition import (
    ParallelTasksAction,
    ParallelTasksObservation,
    TaskSpec,
)


if TYPE_CHECKING:
    from openhands.sdk.agent.agent import Agent
    from openhands.sdk.event.base import Event
    from openhands.tools.task.manager import TaskManager


logger = get_logger(__name__)

_SUBAGENTS_DIR: Final[str] = "subagents"


class ParallelTasksExecutor(ToolExecutor):
    """Executor for the parallel_tasks tool."""

    def __init__(self, manager: TaskManager):
        self._manager = manager
        self._persistence_dir: Path | None = None
        self._persistence_lock = threading.Lock()

    def __call__(
        self,
        action: ParallelTasksAction,
        conversation: LocalConversation | None = None,
    ) -> ParallelTasksObservation:
        if conversation is not None:
            self._manager.attach_parent(conversation)
            self._ensure_persistence_dir(conversation)

        if not action.tasks:
            return ParallelTasksObservation.from_text(
                text="No tasks provided.",
                task_count=0,
                completed=0,
                failed=0,
                is_error=True,
            )

        working_dir = conversation.state.workspace.working_dir if conversation else None
        try:
            shared_text = _resolve_shared_context(action.shared_context, working_dir)
        except Exception as exc:
            return ParallelTasksObservation.from_text(
                text=f"Failed to resolve shared_context: {exc}",
                task_count=len(action.tasks),
                completed=0,
                failed=len(action.tasks),
                is_error=True,
            )

        results: dict[int, str] = {}
        errors: dict[int, str] = {}
        max_workers = min(action.max_concurrency, len(action.tasks))
        parent_event_id = action.action_id

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="parallel_tasks"
        ) as pool:
            future_to_idx = {
                pool.submit(
                    self._run_one_task,
                    task,
                    idx,
                    shared_text,
                    conversation,
                    parent_event_id,
                ): idx
                for idx, task in enumerate(action.tasks)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    logger.error(
                        f"parallel_tasks task[{idx}] failed: {exc}", exc_info=True
                    )
                    errors[idx] = str(exc)

        completed = len(results)
        failed = len(errors)

        # Build combined result text
        parts: list[str] = []
        for idx, task in enumerate(action.tasks):
            label = task.description or task.subagent_type or f"task-{idx + 1}"
            if idx in results:
                parts.append(f"### {label}\n{results[idx]}")
            else:
                parts.append(f"### {label} (ERROR)\n{errors.get(idx, 'Unknown error')}")
        combined = "\n\n".join(parts)

        # Optional reduce step; task_index = len(action.tasks) to distinguish
        # it from worker task indices.
        reduce_task_index = len(action.tasks)
        if action.reduce and completed > 0:
            serialised = "\n\n".join(
                f"Task {idx + 1} ({action.tasks[idx].description or action.tasks[idx].subagent_type}):"  # noqa: E501
                f"\n{results[idx]}"
                for idx in sorted(results)
            )
            reduce_prompt = f"Task results:\n\n{serialised}\n\n{action.reduce.prompt}"
            reduce_task = TaskSpec(
                prompt=reduce_prompt,
                subagent_type=action.reduce.subagent_type,
                description="reduce",
            )
            try:
                final_text = self._run_one_task(
                    reduce_task,
                    reduce_task_index,
                    shared_text,
                    conversation,
                    parent_event_id,
                )
            except Exception as exc:
                logger.error(f"parallel_tasks reduce step failed: {exc}", exc_info=True)
                final_text = f"Reduce step failed: {exc}\n\n{combined}"
        else:
            final_text = combined

        is_error = failed > 0 and completed == 0
        return ParallelTasksObservation.from_text(
            text=final_text,
            task_count=len(action.tasks),
            completed=completed,
            failed=failed,
            is_error=is_error,
        )

    def _ensure_persistence_dir(self, conversation: LocalConversation) -> None:
        with self._persistence_lock:
            if self._persistence_dir is not None:
                return
            parent_dir = conversation.state.persistence_dir
            if parent_dir is not None:
                subdir = Path(parent_dir) / _SUBAGENTS_DIR
                subdir.mkdir(parents=True, exist_ok=True)
                self._persistence_dir = subdir
            else:
                self._persistence_dir = Path(
                    tempfile.mkdtemp(prefix="openhands_ptasks_")
                )

    def _run_one_task(
        self,
        task: TaskSpec,
        idx: int,
        shared_text: str,
        parent: LocalConversation | None,
        parent_event_id: str | None = None,
    ) -> str:
        factory = get_agent_factory(task.subagent_type)
        worker_agent = self._build_worker_agent(factory, shared_text, parent)

        persistence_dir = self._persistence_dir
        workspace = parent.state.workspace.working_dir if parent else "/"
        parent_visualizer = parent._visualizer if parent else None

        label = task.description or f"task-{idx + 1}"
        visualizer = None
        if parent_visualizer is not None:
            visualizer = parent_visualizer.create_sub_visualizer(label)

        effective_max_iter = (
            factory.definition.max_iteration_per_run
            if factory.definition.max_iteration_per_run
            else (parent.max_iteration_per_run if parent else 50)
        )

        # Forward sub-task events to the parent conversation's WebSocket subscribers
        # tagged with parent_event_id + task_index so the frontend can partition them.
        additional_callbacks: list = []
        if parent is not None and parent_event_id is not None:

            def _subagent_forward(
                event: Event,
                _pid: str = parent_event_id,
                _idx: int = idx,
            ) -> None:
                tagged = event.model_copy(
                    update={"parent_event_id": _pid, "task_index": _idx}
                )
                parent.emit_passthrough_event(tagged)

            additional_callbacks.append(_subagent_forward)

        conv = LocalConversation(
            agent=worker_agent,
            workspace=workspace,
            visualizer=visualizer,
            persistence_dir=persistence_dir,
            conversation_id=uuid.uuid4(),
            callbacks=additional_callbacks if additional_callbacks else None,
            max_iteration_per_run=effective_max_iter,
            hook_config=factory.definition.hooks,
            delete_on_close=True,
        )

        confirmation_policy = factory.definition.get_confirmation_policy()
        if confirmation_policy is None and parent is not None:
            conv.set_confirmation_policy(parent.state.confirmation_policy)
        elif confirmation_policy is not None:
            conv.set_confirmation_policy(confirmation_policy)

        parent_name = None
        if (
            parent is not None
            and hasattr(parent, "_visualizer")
            and parent._visualizer is not None
        ):
            parent_name = getattr(parent._visualizer, "_name", None)

        try:
            conv.send_message(task.prompt, sender=parent_name)
            self._run_until_finished(conv)
            result = get_agent_final_response(conv.state.events)
            logger.info(f"parallel_tasks {label} completed")
            self._sync_metrics(parent, label, conv)
            return result or "Task completed with no result."
        except Exception:
            self._sync_metrics(parent, label, conv)
            raise
        finally:
            try:
                conv.pause()
                conv.close()
            except Exception:
                pass

    def _build_worker_agent(
        self,
        factory: AgentFactory,
        shared_text: str,
        parent: LocalConversation | None,
    ) -> Agent:
        """Create a worker agent for the given factory, applying the shared context."""
        if parent is not None:
            base_llm = getattr(parent.agent, "subagent_llm", None) or parent.agent.llm
        else:
            raise RuntimeError(
                "ParallelTasksExecutor requires a parent conversation to be set "
                "before calling __call__."
            )

        sub_llm = base_llm.model_copy(update={"stream": False})
        sub_llm.reset_metrics()

        worker_agent = factory.factory_func(sub_llm)
        worker_agent = worker_agent.model_copy(
            update={"llm": worker_agent.llm.model_copy(update={"stream": False})}
        )

        if shared_text:
            definition_prompt = factory.definition.system_prompt or ""
            augmented = (
                shared_text + "\n\n" + definition_prompt
                if definition_prompt
                else shared_text
            )
            worker_agent = worker_agent.model_copy(update={"system_prompt": augmented})

        return worker_agent

    def _run_until_finished(self, conv: LocalConversation) -> None:
        """Run a conversation to completion, handling confirmation requests."""
        conv.run()
        while (
            conv.state.execution_status
            == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION
        ):
            pending = ConversationState.get_unmatched_actions(conv.state.events)
            if not pending:
                break
            confirmation_handler = self._manager._confirmation_handler
            if confirmation_handler is None or confirmation_handler(
                "parallel_task", pending
            ):
                conv.run()
            else:
                conv.reject_pending_actions("User rejected the actions")
                conv.run()

    @staticmethod
    def _sync_metrics(
        parent: LocalConversation | None,
        label: str,
        conv: LocalConversation,
    ) -> None:
        if parent is None:
            return
        key = f"parallel_task:{label}"
        parent.conversation_stats.usage_to_metrics[key] = (
            conv.conversation_stats.get_combined_metrics()
        )

    def close(self) -> None:
        self._manager.close()
        with self._persistence_lock:
            if self._persistence_dir is not None and self._persistence_dir.exists():
                # Only clean up temp dirs (those not under a parent persistence
                # dir). We rely on the manager's cleanup logic for that case.
                parent_conv = self._manager._parent_conversation
                if parent_conv is None or parent_conv.state.persistence_dir is None:
                    shutil.rmtree(self._persistence_dir, ignore_errors=True)


def _resolve_shared_context(
    items: list[str],
    working_dir: str | None,
) -> str:
    """Resolve shared_context items to a single string.

    Items prefixed with '@' are read from disk; other items are used verbatim.
    Relative paths are resolved against `working_dir` (or cwd as fallback).
    """
    base = working_dir or os.getcwd()
    parts: list[str] = []
    for item in items:
        if item.startswith("@"):
            path_str = item[1:]
            path = Path(path_str) if os.path.isabs(path_str) else Path(base) / path_str
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ValueError(
                    f"Could not read shared_context file '{path_str}': {exc}"
                ) from exc
        else:
            parts.append(item)
    return "\n\n".join(p for p in parts if p)
