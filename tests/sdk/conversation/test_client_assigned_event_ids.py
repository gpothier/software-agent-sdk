"""Tests for client-assigned event IDs."""

import uuid

import pytest

from openhands.sdk import LLM, Agent, Message
from openhands.sdk.conversation import Conversation
from openhands.sdk.conversation.request import SendMessageRequest
from openhands.sdk.event import MessageEvent
from openhands.sdk.llm.message import TextContent


class TestSendMessageRequestEventId:
    """Tests for event_id field in SendMessageRequest."""

    def test_send_message_request_accepts_event_id(self):
        """SendMessageRequest should accept an optional event_id."""
        event_id = str(uuid.uuid4())
        req = SendMessageRequest(
            role="user",
            content=[TextContent(text="hello")],
            event_id=event_id,
        )
        assert req.event_id == event_id

    def test_send_message_request_event_id_defaults_to_none(self):
        """SendMessageRequest event_id should default to None."""
        req = SendMessageRequest(
            role="user",
            content=[TextContent(text="hello")],
        )
        assert req.event_id is None


class TestLocalConversationClientAssignedEventIds:
    """Tests for client-assigned event IDs in LocalConversation."""

    @pytest.fixture
    def minimal_agent(self):
        """Create a minimal agent for testing."""
        # Use a model with larger context window for the SDK validation
        llm = LLM(model="gpt-4o")  # Won't actually call the LLM
        return Agent(llm=llm, tools=[])

    def test_send_message_uses_provided_event_id(self, minimal_agent, tmp_path):
        """When event_id is provided, the MessageEvent should use it."""
        event_id = str(uuid.uuid4())

        conversation = Conversation(
            agent=minimal_agent,
            workspace=str(tmp_path),
            delete_on_close=True,
        )

        # Send message with custom event_id
        message = Message(role="user", content=[TextContent(text="test message")])
        conversation.send_message(message, event_id=event_id)

        # Check that the event was created with the provided ID
        events = list(conversation._state.events)
        user_messages = [
            e for e in events if isinstance(e, MessageEvent) and e.source == "user"
        ]
        assert len(user_messages) == 1
        assert user_messages[0].id == event_id

        conversation.close()

    def test_send_message_generates_id_when_not_provided(self, minimal_agent, tmp_path):
        """When event_id is not provided, a UUID should be generated."""
        conversation = Conversation(
            agent=minimal_agent,
            workspace=str(tmp_path),
            delete_on_close=True,
        )

        # Send message without event_id
        message = Message(role="user", content=[TextContent(text="test message")])
        conversation.send_message(message)

        # Check that the event was created with a generated UUID
        events = list(conversation._state.events)
        user_messages = [
            e for e in events if isinstance(e, MessageEvent) and e.source == "user"
        ]
        assert len(user_messages) == 1
        # Verify it's a valid UUID format
        uuid.UUID(user_messages[0].id)  # This will raise if not a valid UUID

        conversation.close()

    def test_send_message_string_uses_provided_event_id(self, minimal_agent, tmp_path):
        """When sending a string message with event_id, it should use the provided ID."""  # noqa: E501
        event_id = str(uuid.uuid4())

        conversation = Conversation(
            agent=minimal_agent,
            workspace=str(tmp_path),
            delete_on_close=True,
        )

        # Send string message with custom event_id
        conversation.send_message("test message", event_id=event_id)

        # Check that the event was created with the provided ID
        events = list(conversation._state.events)
        user_messages = [
            e for e in events if isinstance(e, MessageEvent) and e.source == "user"
        ]
        assert len(user_messages) == 1
        assert user_messages[0].id == event_id

        conversation.close()
