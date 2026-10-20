"""Bounded, temporary delivery state. Nothing here survives a restart."""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from .config import RelayConfig
from .events import Announcement, format_message


@dataclass(frozen=True)
class Pending:
    channel: str
    text: str
    deadline: float
    connection: int
    channel_version: int


class Relay:
    def __init__(
        self,
        config: RelayConfig,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.config = config
        self.clock = clock
        self.pending: deque[Pending] = deque()
        self.seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self.ready: set[str] = set()
        self.versions: dict[str, int] = {}
        self.connection = 0
        self.connected = False
        self.closed = False
        self.wake = asyncio.Event()
        self.sender: Callable[[Pending], Awaitable[None]] | None = None

    def duplicate(self, hook: str, delivery: str) -> bool:
        now = self.clock()
        while self.seen and next(iter(self.seen.values())) <= now:
            self.seen.popitem(last=False)
        key = (hook, delivery)
        if key in self.seen:
            return True
        self.seen[key] = now + self.config.dedupe_seconds
        while len(self.seen) > self.config.dedupe_capacity:
            self.seen.popitem(last=False)
        return False

    def begin_connection(self) -> None:
        self.disconnect()
        self.connected = True

    def channel_ready(self, channel: str) -> None:
        if self.connected and not self.closed:
            self.ready.add(channel)

    def channel_lost(self, channel: str) -> None:
        self.ready.discard(channel)
        self.versions[channel] = self.versions.get(channel, 0) + 1
        self.pending = deque(p for p in self.pending if p.channel != channel)

    def disconnect(self) -> None:
        self.connected = False
        self.connection += 1
        self.ready.clear()
        self.pending.clear()
        self.wake.set()

    def valid(self, item: Pending) -> bool:
        return (
            not self.closed
            and self.connected
            and item.channel in self.ready
            and item.connection == self.connection
            and item.channel_version == self.versions.get(item.channel, 0)
            and item.deadline > self.clock()
        )

    def offer(self, announcement: Announcement, channels: tuple[str, ...]) -> int:
        if self.closed or not self.connected:
            return 0
        accepted = 0
        # Expired work must not consume the bounded queue's remaining capacity.
        self.pending = deque(p for p in self.pending if self.valid(p))
        for channel in channels:
            if channel not in self.ready or len(self.pending) >= self.config.capacity:
                continue
            text = format_message(announcement, channel)
            if text is None:
                continue
            self.pending.append(
                Pending(
                    channel,
                    text,
                    self.clock() + self.config.expiry_seconds,
                    self.connection,
                    self.versions.get(channel, 0),
                )
            )
            accepted += 1
        if accepted:
            self.wake.set()
        return accepted

    def close(self) -> None:
        self.closed = True
        self.disconnect()

    async def run(self) -> None:
        next_send = 0.0
        while not self.closed:
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            item = self.pending.popleft()
            delay = next_send - self.clock()
            if delay > 0:
                await asyncio.sleep(delay)
            if not self.valid(item):
                continue
            if self.sender is None:
                raise RuntimeError("Relay sender is not configured")
            await self.sender(item)
            next_send = self.clock() + self.config.send_interval
