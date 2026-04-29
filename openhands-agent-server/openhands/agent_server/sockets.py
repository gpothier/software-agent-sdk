"""
WebSocket endpoints for OpenHands SDK.

These endpoints are separate from the main API routes to handle WebSocket-specific
authentication.  Three auth methods are supported (highest to lowest precedence):

1. **First-message auth** (recommended): The client sends
   ``{"type": "auth", "session_api_key": "..."}`` as the very first WebSocket
   frame after the connection opens.  This keeps tokens out of URLs and
   therefore out of reverse-proxy / load-balancer access logs.
2. Query parameter ``session_api_key`` — deprecated, kept for backwards compat.
3. ``X-Session-API-Key`` header — for non-browser clients.
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import select
import struct
import termios
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.websockets import WebSocketState

from openhands.agent_server.bash_service import get_default_bash_event_service
from openhands.agent_server.config import Config, get_default_config
from openhands.agent_server.conversation_service import (
    get_default_conversation_service,
)
from openhands.agent_server.event_router import normalize_datetime_to_server_timezone
from openhands.agent_server.models import (
    BashError,
    BashEventBase,
    ExecuteBashRequest,
    ServerErrorEvent,
)
from openhands.agent_server.pub_sub import MaxSubscribersError, Subscriber
from openhands.sdk import Event, Message
from openhands.sdk.utils.paging import page_iterator


sockets_router = APIRouter(prefix="/sockets", tags=["WebSockets"])
conversation_service = get_default_conversation_service()
bash_event_service = get_default_bash_event_service()
logger = logging.getLogger(__name__)


def _get_config(websocket: WebSocket) -> Config:
    """Return the Config associated with this FastAPI app instance.

    This ensures WebSocket auth follows the same configuration as the REST API
    when the agent server is used as a library (e.g., tests or when mounted into
    another FastAPI app), rather than always reading environment defaults.
    """
    config = getattr(websocket.app.state, "config", None)
    if isinstance(config, Config):
        return config
    return get_default_config()


def _resolve_websocket_session_api_key(
    websocket: WebSocket,
    session_api_key: str | None,
) -> str | None:
    """Resolve the session API key from multiple sources.

    Precedence order (highest to lowest):
    1. Query parameter (session_api_key) - for browser compatibility
    2. X-Session-API-Key header - for non-browser clients

    Returns None if no key is provided in any source.
    """
    if session_api_key is not None:
        return session_api_key

    header_key = websocket.headers.get("x-session-api-key")
    if header_key is not None:
        return header_key

    return None


# Give clients 10 seconds to send auth frame after connection opens.
# This balances security (don't hold connections indefinitely) with
# accommodating slow networks and client startup time.
_FIRST_MESSAGE_AUTH_TIMEOUT_SECONDS = 10


async def _accept_authenticated_websocket(
    websocket: WebSocket,
    session_api_key: str | None,
) -> bool:
    """Authenticate and accept the socket, or close with an auth error.

    Authentication is attempted in the following order:

    1. Query parameter / header (legacy, deprecated).
    2. First-message auth — the client sends
       ``{"type": "auth", "session_api_key": "..."}`` as the first frame.

    The WebSocket is always *accepted* before first-message auth is attempted
    because raw WebSocket requires ``accept()`` before any frames can be read.
    """
    config = _get_config(websocket)
    resolved_key = _resolve_websocket_session_api_key(websocket, session_api_key)

    # No auth configured — accept unconditionally.
    if not config.session_api_keys:
        await websocket.accept()
        return True

    # Legacy path: key supplied via query param or header.
    if resolved_key is not None:
        if resolved_key in config.session_api_keys:
            logger.warning(
                "session_api_key passed via query param or header is deprecated. "
                "Use first-message auth instead."
            )
            await websocket.accept()
            return True
        logger.warning("WebSocket authentication failed: invalid API key")
        await websocket.close(code=4001, reason="Authentication failed")
        return False

    # First-message auth: we must accept() before reading frames because the
    # WebSocket protocol requires the handshake to complete first.  The legacy
    # path above can reject *before* accepting (close on an un-accepted socket
    # sends an HTTP 403-style response), but here we need to read a frame.
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=_FIRST_MESSAGE_AUTH_TIMEOUT_SECONDS,
        )
        data = json.loads(raw)
    except TimeoutError:
        logger.warning(
            "WebSocket first-message auth failed: timeout waiting for auth frame"
        )
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False
    except json.JSONDecodeError:
        logger.warning("WebSocket first-message auth failed: malformed JSON")
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False
    except WebSocketDisconnect:
        logger.warning("WebSocket first-message auth failed: client disconnected")
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False

    if not isinstance(data, dict):
        logger.warning(
            "WebSocket first-message auth failed: payload is not a JSON object"
        )
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False
    if data.get("type") != "auth":
        logger.warning("WebSocket first-message auth failed: wrong message type")
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False
    if data.get("session_api_key") not in config.session_api_keys:
        logger.warning("WebSocket first-message auth failed: invalid API key")
        await _safe_close_websocket(
            websocket, code=4001, reason="Authentication failed"
        )
        return False

    logger.info("WebSocket authenticated via first-message auth")
    return True


@sockets_router.websocket("/events/{conversation_id}")
async def events_socket(
    conversation_id: UUID,
    websocket: WebSocket,
    session_api_key: Annotated[str | None, Query(alias="session_api_key")] = None,
    resend_mode: Annotated[
        Literal["all", "since"] | None,
        Query(
            description=(
                "Mode for resending historical events on connect. "
                "'all' sends all events, 'since' sends events after 'after_timestamp'."
            )
        ),
    ] = None,
    after_timestamp: Annotated[
        datetime | None,
        Query(
            description=(
                "Required when resend_mode='since'. Events with timestamp >= this "
                "value will be sent. Accepts ISO 8601 format. Timezone-aware "
                "datetimes are converted to server local time; naive datetimes "
                "assumed in server timezone."
            )
        ),
    ] = None,
    # Deprecated parameter - kept for backward compatibility
    resend_all: Annotated[
        bool,
        Query(
            include_in_schema=False,
            deprecated=True,
        ),
    ] = False,
):
    """WebSocket endpoint for conversation events.

    Args:
        conversation_id: The conversation ID to subscribe to.
        websocket: The WebSocket connection.
        session_api_key: Optional API key for authentication.
        resend_mode: Mode for resending historical events on connect.
            - 'all': Resend all existing events
            - 'since': Resend events after 'after_timestamp' (requires after_timestamp)
            - None: Don't resend, just subscribe to new events
        after_timestamp: Required when resend_mode='since'. Events with
            timestamp >= this value will be sent. Timestamps are interpreted in
            server local time. Timezone-aware datetimes are converted to server
            timezone. Enables efficient bi-directional loading where REST fetches
            historical events and WebSocket handles events after a specific point.
        resend_all: DEPRECATED. Use resend_mode='all' instead. Kept for
            backward compatibility - if True and resend_mode is None, behaves
            as resend_mode='all'.
    """
    if not await _accept_authenticated_websocket(websocket, session_api_key):
        return

    logger.info(f"Event Websocket Connected: {conversation_id}")
    event_service = await conversation_service.get_event_service(conversation_id)
    if event_service is None:
        logger.warning(f"Converation not found: {conversation_id}")
        await websocket.close(code=4004, reason="Conversation not found")
        return

    try:
        subscriber_id = await event_service.subscribe_to_events(
            _WebSocketSubscriber(websocket)
        )
    except MaxSubscribersError:
        logger.warning(f"Subscriber limit reached for conversation {conversation_id}")
        await websocket.close(
            code=1013, reason="Too many connections for this conversation"
        )
        return

    # Determine effective resend mode (handle deprecated resend_all)
    effective_mode = resend_mode
    if effective_mode is None and resend_all:
        logger.warning(
            "resend_all is deprecated, use resend_mode='all' instead: "
            f"{conversation_id}"
        )
        effective_mode = "all"

    # Normalize timezone-aware datetimes to server timezone
    normalized_after_timestamp = (
        normalize_datetime_to_server_timezone(after_timestamp)
        if after_timestamp
        else None
    )

    try:
        # Resend existing events based on mode
        if effective_mode == "all":
            logger.info(f"Resending all events: {conversation_id}")
            async for event in page_iterator(event_service.search_events):
                await _send_event(event, websocket)
        elif effective_mode == "since":
            if not normalized_after_timestamp:
                logger.warning(
                    f"resend_mode='since' requires after_timestamp, "
                    f"no events will be resent: {conversation_id}"
                )
            else:
                logger.info(
                    f"Resending events since {normalized_after_timestamp}: "
                    f"{conversation_id}"
                )
                async for event in page_iterator(
                    event_service.search_events,
                    timestamp__gte=normalized_after_timestamp,
                ):
                    await _send_event(event, websocket)

        # Listen for messages over the socket
        while True:
            try:
                data = await websocket.receive_json()
                if _is_auth_control_message(data):
                    logger.debug(
                        "ignoring redundant auth control frame: %s",
                        conversation_id,
                    )
                    continue
                logger.info(f"Received message: {conversation_id}")
                message = Message.model_validate(data)
                await event_service.send_message(message, True)
            except WebSocketDisconnect:
                logger.info("Event websocket disconnected")
                return
            except Exception as e:
                # Something went wrong - Tell the client so they can handle it
                try:
                    error_event = ServerErrorEvent(
                        source="environment",
                        code=e.__class__.__name__,
                        detail=str(e),
                    )
                    dumped = error_event.model_dump(mode="json")
                    await websocket.send_json(dumped)
                    # Log after - if send event raises an error logging is handled
                    # in the except block
                    logger.exception("error_in_subscription", stack_info=True)
                except Exception:
                    # Sending the error event failed - likely a closed socket
                    logger.info("Event websocket disconnected")
                    logger.debug("error_sending_error", exc_info=True, stack_info=True)
                    await _safe_close_websocket(websocket)
                    return
    finally:
        await event_service.unsubscribe_from_events(subscriber_id)


@sockets_router.websocket("/bash-events")
async def bash_events_socket(
    websocket: WebSocket,
    session_api_key: Annotated[str | None, Query(alias="session_api_key")] = None,
    resend_mode: Annotated[
        Literal["all"] | None,
        Query(
            description=(
                "Mode for resending historical events on connect. "
                "'all' sends all events."
            )
        ),
    ] = None,
    # Deprecated parameter - kept for backward compatibility
    resend_all: Annotated[
        bool,
        Query(
            include_in_schema=False,
            deprecated=True,
        ),
    ] = False,
):
    """WebSocket endpoint for bash events.

    Args:
        websocket: The WebSocket connection.
        session_api_key: Optional API key for authentication.
        resend_mode: Mode for resending historical events on connect.
            - 'all': Resend all existing bash events
            - None: Don't resend, just subscribe to new events
        resend_all: DEPRECATED. Use resend_mode='all' instead.
    """
    if not await _accept_authenticated_websocket(websocket, session_api_key):
        return

    logger.info("Bash Websocket Connected")
    try:
        subscriber_id = await bash_event_service.subscribe_to_events(
            _BashWebSocketSubscriber(websocket)
        )
    except MaxSubscribersError:
        logger.warning("Subscriber limit reached for bash events")
        await websocket.close(code=1013, reason="Too many bash event connections")
        return

    # Determine effective resend mode (handle deprecated resend_all)
    effective_mode = resend_mode
    if effective_mode is None and resend_all:
        logger.warning("resend_all is deprecated, use resend_mode='all' instead")
        effective_mode = "all"

    try:
        # Resend all existing events if requested
        if effective_mode == "all":
            logger.info("Resending bash events")
            async for event in page_iterator(bash_event_service.search_bash_events):
                await _send_bash_event(event, websocket)

        while True:
            try:
                # Keep the connection alive and handle any incoming messages
                data = await websocket.receive_json()
                logger.info("Received bash request")
                request = ExecuteBashRequest.model_validate(data)
                await bash_event_service.start_bash_command(request)
            except WebSocketDisconnect:
                logger.info("Bash websocket disconnected")
                return
            except Exception as e:
                # Something went wrong - Tell the client so they can handle it
                try:
                    error_event = BashError(
                        code=e.__class__.__name__,
                        detail=str(e),
                    )
                    dumped = error_event.model_dump(mode="json")
                    await websocket.send_json(dumped)
                    # Log after - if send event raises an error logging is handled
                    # in the except block
                    logger.exception(
                        "error_in_bash_event_subscription", stack_info=True
                    )
                except Exception:
                    # Sending the error event failed - likely a closed socket
                    logger.info("Base websocket disconnected")
                    logger.debug(
                        "error_sending_bash_error", exc_info=True, stack_info=True
                    )
                    await _safe_close_websocket(websocket)
                    return
    finally:
        await bash_event_service.unsubscribe_from_events(subscriber_id)


async def _send_event(event: Event, websocket: WebSocket):
    if not _is_websocket_connected(websocket):
        # Client already disconnected; the pub/sub callback was racing with
        # cleanup. Avoid noisy tracebacks from starlette refusing to send.
        logger.debug("skip_sending_event_socket_disconnected: %r", event)
        return
    try:
        dumped = event.model_dump(mode="json")
        await websocket.send_json(dumped)
    except (RuntimeError, WebSocketDisconnect) as e:
        # Expected race: client disconnected between our state check and send.
        logger.debug("error_sending_event_disconnected: %r (%s)", event, e)
    except Exception:
        logger.exception("error_sending_event: %r", event, stack_info=True)


def _is_auth_control_message(data: object) -> bool:
    """Return True for ``{"type": "auth", ...}`` first-message-auth frames.

    Clients that handle both legacy and first-message auth may send this
    frame even after legacy (query/header) auth has already succeeded.
    The post-auth receive loops must ignore it instead of validating it
    as a regular message payload.
    """
    return isinstance(data, dict) and data.get("type") == "auth"


async def _safe_close_websocket(
    websocket: WebSocket,
    code: int = 1000,
    reason: str = "Connection closed",
):
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        # WebSocket may already be closed or in inconsistent state
        logger.debug("WebSocket close failed (may already be closed)")


def _is_websocket_connected(websocket: WebSocket) -> bool:
    """Best-effort check that the websocket is still in the CONNECTED state.

    Starlette raises ``RuntimeError('Cannot call "send" once a close message
    has been sent.')`` if we try to send on a socket whose ``application_state``
    is ``DISCONNECTED``. Pre-checking avoids noisy tracebacks when a pub/sub
    callback fires after the peer has gone away.

    Returns ``True`` when the state is unknown (e.g. tests using ``MagicMock``)
    so callers still attempt the send and get the original behaviour.
    """
    app_state = getattr(websocket, "application_state", None)
    client_state = getattr(websocket, "client_state", None)
    if app_state is WebSocketState.DISCONNECTED:
        return False
    if client_state is WebSocketState.DISCONNECTED:
        return False
    return True


@dataclass
class _WebSocketSubscriber(Subscriber):
    """WebSocket subscriber for conversation events."""

    websocket: WebSocket

    async def __call__(self, event: Event):
        await _send_event(event, self.websocket)


async def _send_bash_event(event: BashEventBase, websocket: WebSocket):
    if not _is_websocket_connected(websocket):
        logger.debug("skip_sending_bash_event_socket_disconnected: %r", event)
        return
    try:
        dumped = event.model_dump(mode="json")
        await websocket.send_json(dumped)
    except (RuntimeError, WebSocketDisconnect) as e:
        logger.debug("error_sending_bash_event_disconnected: %r (%s)", event, e)
    except Exception:
        logger.exception("error_sending_bash_event: %r", event, stack_info=True)


@dataclass
class _BashWebSocketSubscriber(Subscriber[BashEventBase]):
    """WebSocket subscriber for bash events."""

    websocket: WebSocket

    async def __call__(self, event: BashEventBase):
        await _send_bash_event(event, self.websocket)


# ---------------------------------------------------------------------------
# Shell PTY session
# ---------------------------------------------------------------------------

_SHELL_REPLAY_BUFFER_SIZE = 128 * 1024  # 128 KB ring buffer for reconnect replay


@dataclass
class _ShellSession:
    """A persistent PTY shell session for the human terminal tab."""

    master_fd: int
    pid: int
    replay_buffer: bytearray = field(default_factory=bytearray)
    output_queues: list[asyncio.Queue] = field(default_factory=list)
    loop: asyncio.AbstractEventLoop | None = None
    _reader_thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)

    def is_alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def start_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self.master_fd, 4096)
                if not data:
                    break
                # Update replay buffer
                self.replay_buffer.extend(data)
                if len(self.replay_buffer) > _SHELL_REPLAY_BUFFER_SIZE:
                    self.replay_buffer = self.replay_buffer[-_SHELL_REPLAY_BUFFER_SIZE:]
                # Notify all subscribers
                if self.loop:
                    for q in list(self.output_queues):
                        asyncio.run_coroutine_threadsafe(q.put(data), self.loop)
            except OSError:
                break
        # Signal EOF to all subscribers
        if self.loop:
            for q in list(self.output_queues):
                asyncio.run_coroutine_threadsafe(q.put(None), self.loop)

    def set_winsize(self, rows: int, cols: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass


# One shell session per agent server instance (one per sandbox)
_shell_session: _ShellSession | None = None
_shell_lock = threading.Lock()


def _create_shell_session() -> _ShellSession:
    """Fork a bash child process under a PTY and return the session."""
    master_fd, slave_fd = pty.openpty()

    # Set initial window size (80×24)
    winsize = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()
    if pid == 0:  # child
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        for fd in (0, 1, 2):
            os.dup2(slave_fd, fd)
        if slave_fd > 2:
            os.close(slave_fd)
        env = {
            "TERM": "xterm-256color",
            "HOME": os.environ.get("HOME", "/root"),
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "SHELL": "/bin/bash",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        os.execve("/bin/bash", ["/bin/bash"], env)
        os._exit(1)
    else:  # parent
        os.close(slave_fd)
        return _ShellSession(master_fd=master_fd, pid=pid)


def _get_or_create_shell_session() -> _ShellSession:
    global _shell_session
    with _shell_lock:
        if _shell_session is None or not _shell_session.is_alive():
            _shell_session = _create_shell_session()
        return _shell_session


@sockets_router.websocket("/shell")
async def shell_socket(
    websocket: WebSocket,
    session_api_key: Annotated[str | None, Query(alias="session_api_key")] = None,
):
    """WebSocket endpoint for a persistent interactive shell session.

    Provides a raw PTY shell (bash) that persists across WebSocket disconnects.
    The browser xterm.js connects via AttachAddon; keystrokes flow in as bytes,
    terminal output flows out as bytes.

    Resize: send a JSON text frame ``{"type":"resize","cols":N,"rows":M}``.
    """
    if not await _accept_authenticated_websocket(websocket, session_api_key):
        return

    logger.info("Shell WebSocket connected")
    loop = asyncio.get_event_loop()

    session = _get_or_create_shell_session()

    # Start the PTY reader thread if not running
    if session._reader_thread is None or not session._reader_thread.is_alive():
        session.start_reader(loop)
    else:
        session.loop = loop  # Update loop reference for this connection

    # Subscribe this connection to PTY output
    output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    session.output_queues.append(output_queue)

    # Replay recent output so reconnected clients see the current state
    if session.replay_buffer:
        try:
            await websocket.send_bytes(bytes(session.replay_buffer))
        except Exception:
            pass

    async def send_output() -> None:
        """Forward PTY output to the WebSocket."""
        while True:
            data = await output_queue.get()
            if data is None:
                break
            try:
                await websocket.send_bytes(data)
            except Exception:
                break

    async def handle_input() -> None:
        """Forward WebSocket input to the PTY."""
        while True:
            try:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw_bytes = message.get("bytes")
                if raw_bytes:
                    session.write(raw_bytes)
                else:
                    text = message.get("text", "")
                    if text:
                        try:
                            msg = json.loads(text)
                            if msg.get("type") == "resize":
                                session.set_winsize(
                                    int(msg.get("rows", 24)),
                                    int(msg.get("cols", 80)),
                                )
                        except (json.JSONDecodeError, ValueError):
                            session.write(text.encode())
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.debug(f"Shell WebSocket input error: {e}")
                break

    try:
        await asyncio.gather(send_output(), handle_input())
    finally:
        try:
            session.output_queues.remove(output_queue)
        except ValueError:
            pass
        logger.info("Shell WebSocket disconnected")
