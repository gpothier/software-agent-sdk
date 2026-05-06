"""Event emitted when new skills are discovered during a conversation turn."""

from openhands.sdk.event.base import Event
from openhands.sdk.event.types import SourceType


class SkillsUpdatedEvent(Event):
    """Emitted at the start of each agent turn when newly-discovered skills are added.

    The agent-server re-scans the workspace for git repos on every turn.  If new
    skills are found that were not known at conversation-creation time (e.g. a repo
    cloned by the agent in a previous turn), they are added to agent_context.skills
    and this event is emitted so that OpenFeet can notify the frontend to refresh
    its skill list.

    ``added`` contains the names of skills added this turn.  An empty list is
    never emitted — the event is only created when at least one skill is new.
    """

    source: SourceType = "environment"
    added: list[str]
