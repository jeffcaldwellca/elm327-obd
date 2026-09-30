"""Async TCP transport for ELM327 WiFi adapters.

The adapter speaks a line-oriented ASCII protocol: we send a command ending
in ``\\r`` and it replies with one or more ``\\r``-separated lines followed by
the ``>`` prompt once it is ready for the next command.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

TrafficListener = Callable[[str, str], None]  # (direction "tx"/"rx", text)


class TransportError(Exception):
    """Connection-level failure (socket closed, timeout, no prompt)."""


class ElmTransport:
    def __init__(self, host: str, port: int, *, connect_timeout: float = 4.0) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.listeners: list[TrafficListener] = []
        self.last_latency: float = 0.0

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=1 << 20),
                timeout=self.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            reason = str(exc) or "timed out"
            raise TransportError(f"Cannot reach adapter at {self.address}: {reason}") from exc

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None

    @asynccontextmanager
    async def session(self):
        """Hold the adapter exclusively for a multi-command sequence."""
        async with self._lock:
            yield self

    async def command(self, cmd: str, timeout: float = 5.0) -> list[str]:
        async with self._lock:
            return await self.command_unlocked(cmd, timeout)

    async def command_unlocked(self, cmd: str, timeout: float = 5.0) -> list[str]:
        if not self.connected or self._reader is None or self._writer is None:
            raise TransportError("Not connected")
        if not cmd.isascii() or any(c in cmd for c in "\r\n"):
            raise ValueError(f"Adapter commands must be plain ASCII on one line: {cmd!r}")
        self._emit("tx", cmd)
        start = time.perf_counter()
        try:
            self._writer.write((cmd + "\r").encode("ascii"))
            await self._writer.drain()
            raw = await asyncio.wait_for(self._reader.readuntil(b">"), timeout)
        except asyncio.TimeoutError:
            await self._resync()
            raise TransportError(f"Timed out waiting for reply to {cmd!r}") from None
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            await self.close()
            raise TransportError(f"Connection lost: {exc}") from exc
        self.last_latency = time.perf_counter() - start
        lines = _split_reply(raw, cmd)
        for line in lines:
            self._emit("rx", line)
        return lines

    async def _resync(self) -> None:
        """Recover after a timeout without desynchronising the reply stream.

        A bare CR would make the ELM327 *repeat* the previous command and leave
        an extra reply queued, shifting every later answer by one. Instead:

        1. wait a little longer for the late reply's prompt;
        2. otherwise send one non-CR byte, which interrupts a busy adapter
           (it answers ``STOPPED`` + prompt);
        3. if still nothing, the byte is sitting in an idle adapter's input
           buffer, so terminate it with CR (the adapter answers ``?``).
        """
        if self._reader is None or self._writer is None:
            return
        try:
            if await self._await_prompt(1.5):
                return
            self._writer.write(b"X")
            await self._writer.drain()
            if await self._await_prompt(1.0):
                return
            self._writer.write(b"\r")
            await self._writer.drain()
            await self._await_prompt(1.0)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await self.close()

    async def _await_prompt(self, timeout: float) -> bool:
        assert self._reader is not None
        try:
            await asyncio.wait_for(self._reader.readuntil(b">"), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _emit(self, direction: str, text: str) -> None:
        for listener in list(self.listeners):
            try:
                listener(direction, text)
            except Exception:
                pass


def _split_reply(raw: bytes, cmd: str) -> list[str]:
    text = raw.decode("ascii", errors="replace").replace("\x00", "")
    text = text.rstrip(">").replace("\r\n", "\r").replace("\n", "\r")
    lines = [line.strip() for line in text.split("\r")]
    lines = [line for line in lines if line]
    # Drop the echoed command if echo is still on (e.g. right after ATZ).
    if lines and lines[0].replace(" ", "").upper() == cmd.replace(" ", "").upper():
        lines = lines[1:]
    return lines
