"""Parallel tasks tool — fan-out multiple sub-agent tasks concurrently."""

from openhands.tools.parallel_tasks.definition import (
    ParallelTasksAction,
    ParallelTasksObservation,
    ParallelTasksTool,
    ParallelTasksToolSet,
    ReduceSpec,
    TaskSpec,
)

__all__ = [
    "ParallelTasksAction",
    "ParallelTasksObservation",
    "ParallelTasksTool",
    "ParallelTasksToolSet",
    "ReduceSpec",
    "TaskSpec",
]
