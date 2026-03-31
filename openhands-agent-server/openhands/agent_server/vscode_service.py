"""VSCode service for managing OpenVSCode Server in the agent server."""

import asyncio
import os
from pathlib import Path

import aiohttp

from openhands.sdk.logger import get_logger
from openhands.sdk.utils import sanitized_env


logger = get_logger(__name__)

# Default health check settings
DEFAULT_HEALTH_CHECK_TIMEOUT = 5.0
DEFAULT_WATCHDOG_INTERVAL = 60.0
DEFAULT_CONSECUTIVE_FAILURES_THRESHOLD = 3


class VSCodeService:
    """Service to manage VSCode server startup and token generation."""

    def __init__(
        self,
        port: int = 8001,
        connection_token: str | None = None,
        server_base_path: str | None = None,
    ):
        """Initialize VSCode service.

        Args:
            port: Port to run VSCode server on (default: 8001)
            workspace_path: Path to the workspace directory
            create_workspace: Whether to create the workspace directory if it doesn't
                exist
            server_base_path: Base path for the server (used in path-based routing)
        """
        self.port: int = port
        self.connection_token: str | None = connection_token
        self.server_base_path: str | None = server_base_path
        self.process: asyncio.subprocess.Process | None = None
        self.openvscode_server_root: Path = Path("/openhands/.openvscode-server")
        self.extensions_dir: Path = self.openvscode_server_root / "extensions"
        self._watchdog_task: asyncio.Task | None = None
        self._consecutive_failures: int = 0

    async def start(self) -> bool:
        """Start the VSCode server.

        Returns:
            True if started successfully, False otherwise
        """
        try:
            # Check if VSCode server binary exists
            if not self._check_vscode_available():
                logger.warning(
                    "VSCode server binary not found, VSCode will be disabled"
                )
                return False

            # Generate connection token if not already set
            if self.connection_token is None:
                self.connection_token = os.urandom(32).hex()

            # Check if port is available
            if not await self._is_port_available():
                logger.warning(
                    f"Port {self.port} is not available, VSCode will be disabled"
                )
                return False

            # Start VSCode server with extensions
            await self._start_vscode_process()

            logger.info(f"VSCode server started successfully on port {self.port}")
            return True

        except Exception as e:
            logger.error(f"Failed to start VSCode server: {e}")
            return False

    async def stop(self) -> None:
        """Stop the VSCode server."""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                logger.info("VSCode server stopped successfully")
            except TimeoutError:
                logger.warning("VSCode server did not stop gracefully, killing process")
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error stopping VSCode server: {e}")
            finally:
                self.process = None

    def get_vscode_url(
        self,
        base_url: str | None = None,
        workspace_dir: str = "workspace",
    ) -> str | None:
        """Get the VSCode URL with authentication token.

        Args:
            base_url: Base URL for the VSCode server
            workspace_dir: Path to workspace directory

        Returns:
            VSCode URL with token, or None if not available
        """
        if self.connection_token is None:
            return None

        if base_url is None:
            base_url = f"http://localhost:{self.port}"

        return f"{base_url}/?tkn={self.connection_token}&folder={workspace_dir}"

    def is_running(self) -> bool:
        """Check if VSCode server process is running.

        Note: This only checks if the process exists and hasn't exited.
        Use health_check() to verify the server is actually responsive.

        Returns:
            True if process is running, False otherwise
        """
        return self.process is not None and self.process.returncode is None

    async def health_check(
        self, timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT
    ) -> bool:
        """Check if VSCode server is actually responsive.

        This performs an HTTP request to verify the server can respond,
        not just that the process is running.

        Args:
            timeout: Timeout for the health check request in seconds

        Returns:
            True if VSCode responds to HTTP requests, False otherwise
        """
        if not self.is_running():
            return False

        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://localhost:{self.port}/"
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    # VSCode returns 200 or 302 (redirect to auth page)
                    return resp.status in (200, 302)
        except asyncio.TimeoutError:
            logger.warning(f"VSCode health check timed out after {timeout}s")
            return False
        except aiohttp.ClientError as e:
            logger.warning(f"VSCode health check failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during VSCode health check: {e}")
            return False

    async def restart(self) -> bool:
        """Restart the VSCode server.

        Returns:
            True if restart was successful, False otherwise
        """
        logger.info("Restarting VSCode server...")
        await self.stop()
        self._consecutive_failures = 0
        return await self.start()

    async def restart_if_unhealthy(
        self, timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT
    ) -> bool:
        """Check health and restart VSCode if it's unresponsive.

        Args:
            timeout: Timeout for the health check request in seconds

        Returns:
            True if restart was needed and successful, False if healthy or restart failed
        """
        if await self.health_check(timeout):
            self._consecutive_failures = 0
            return False

        logger.warning("VSCode server is unresponsive, initiating restart...")
        return await self.restart()

    async def start_watchdog(
        self,
        check_interval: float = DEFAULT_WATCHDOG_INTERVAL,
        failure_threshold: int = DEFAULT_CONSECUTIVE_FAILURES_THRESHOLD,
    ) -> None:
        """Start a background watchdog task that monitors VSCode health.

        The watchdog periodically checks if VSCode is responsive and
        automatically restarts it after consecutive failures exceed the threshold.

        Args:
            check_interval: Seconds between health checks
            failure_threshold: Number of consecutive failures before restart
        """
        if self._watchdog_task is not None:
            logger.warning("Watchdog already running")
            return

        async def watchdog_loop():
            logger.info(
                f"VSCode watchdog started (interval={check_interval}s, "
                f"threshold={failure_threshold})"
            )
            while True:
                try:
                    await asyncio.sleep(check_interval)

                    if not self.is_running():
                        logger.debug("VSCode process not running, watchdog skipping")
                        continue

                    if await self.health_check():
                        self._consecutive_failures = 0
                    else:
                        self._consecutive_failures += 1
                        logger.warning(
                            f"VSCode health check failed "
                            f"({self._consecutive_failures}/{failure_threshold})"
                        )

                        if self._consecutive_failures >= failure_threshold:
                            logger.error(
                                f"VSCode failed {failure_threshold} consecutive "
                                "health checks, restarting..."
                            )
                            await self.restart()

                except asyncio.CancelledError:
                    logger.info("VSCode watchdog stopped")
                    break
                except Exception as e:
                    logger.error(f"Error in VSCode watchdog: {e}")

        self._watchdog_task = asyncio.create_task(watchdog_loop())

    async def stop_watchdog(self) -> None:
        """Stop the background watchdog task."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
            logger.info("VSCode watchdog stopped")

    def _check_vscode_available(self) -> bool:
        """Check if VSCode server binary is available.

        Returns:
            True if available, False otherwise
        """
        vscode_binary = self.openvscode_server_root / "bin" / "openvscode-server"
        return vscode_binary.exists() and vscode_binary.is_file()

    async def _is_port_available(self) -> bool:
        """Check if the specified port is available.

        Returns:
            True if port is available, False otherwise
        """
        try:
            # Try to bind to the port
            server = await asyncio.start_server(
                lambda _r, _w: None, "localhost", self.port
            )
            server.close()
            await server.wait_closed()
            return True
        except OSError:
            return False

    async def _start_vscode_process(self) -> None:
        """Start the VSCode server process."""
        extensions_arg = (
            f"--extensions-dir {self.extensions_dir} "
            if self.extensions_dir.exists()
            else ""
        )
        base_path_arg = (
            f"--server-base-path {self.server_base_path} "
            if self.server_base_path
            else ""
        )
        cmd = (
            f"exec {self.openvscode_server_root}/bin/openvscode-server "
            f"--host 0.0.0.0 "
            f"--connection-token {self.connection_token} "
            f"--port {self.port} "
            f"{extensions_arg}"
            f"{base_path_arg}"
            f"--disable-workspace-trust\n"
        )

        # Start the process
        self.process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=sanitized_env(),
        )

        # Wait for server to start (look for startup message)
        await self._wait_for_startup()

    async def _wait_for_startup(self) -> None:
        """Wait for VSCode server to start up."""
        if not self.process or not self.process.stdout:
            return

        try:
            # Read output until we see the server is ready
            timeout = 30  # 30 second timeout
            start_time = asyncio.get_event_loop().time()

            while (
                self.process.returncode is None
                and (asyncio.get_event_loop().time() - start_time) < timeout
            ):
                try:
                    line_bytes = await asyncio.wait_for(
                        self.process.stdout.readline(), timeout=1.0
                    )
                    if not line_bytes:
                        break

                    line = line_bytes.decode("utf-8", errors="ignore").strip()
                    logger.debug(f"VSCode server output: {line}")

                    # Look for startup indicators
                    if "Web UI available at" in line or "Server bound to" in line:
                        logger.info("VSCode server startup detected")
                        break

                except TimeoutError:
                    continue

        except Exception as e:
            logger.warning(f"Error waiting for VSCode startup: {e}")


# Global VSCode service instance
_vscode_service: VSCodeService | None = None


def get_vscode_service() -> VSCodeService | None:
    """Get the global VSCode service instance.

    Returns:
        VSCode service instance if enabled, None if disabled
    """
    global _vscode_service
    if _vscode_service is None:
        from openhands.agent_server.config import (
            get_default_config,
        )

        config = get_default_config()

        if not config.enable_vscode:
            logger.info("VSCode is disabled in configuration")
            return None
        else:
            connection_token = None
            if config.session_api_keys:
                connection_token = config.session_api_keys[0]
            _vscode_service = VSCodeService(
                port=config.vscode_port,
                connection_token=connection_token,
                server_base_path=config.vscode_base_path,
            )
    return _vscode_service
