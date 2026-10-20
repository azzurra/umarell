import asyncio
from dataclasses import replace

import pytest

from umarell.config import RelayConfig
from umarell.events import Announcement
from umarell.relay import Relay

ANNOUNCEMENT = Announcement("[owner/repo] Issue #1 opened", "https://github.com/o/r")


def ready(config=None, clock=None):
    relay = Relay(config or RelayConfig(), **({"clock": clock} if clock else {}))
    relay.begin_connection()
    relay.channel_ready("#one")
    relay.channel_ready("#two")
    return relay


def test_offline_readiness_and_partial_fanout():
    relay = Relay(RelayConfig(capacity=1))
    assert relay.offer(ANNOUNCEMENT, ("#one",)) == 0
    relay.begin_connection()
    assert relay.offer(ANNOUNCEMENT, ("#one",)) == 0
    relay.channel_ready("#one")
    relay.channel_ready("#two")
    assert relay.offer(ANNOUNCEMENT, ("#one", "#two")) == 1
    assert relay.pending[0].channel == "#one"
    assert relay.offer(ANNOUNCEMENT, ("#one",)) == 0


def test_channel_rejoin_invalidates_popped_work_without_losing_other_channel():
    relay = ready()
    relay.offer(ANNOUNCEMENT, ("#one", "#two"))
    popped = relay.pending.popleft()
    relay.channel_lost("#one")
    relay.channel_ready("#one")
    assert not relay.valid(popped)
    assert len(relay.pending) == 1
    assert relay.valid(relay.pending[0])


def test_disconnect_invalidates_popped_work_and_clears_pending():
    relay = ready()
    relay.offer(ANNOUNCEMENT, ("#one", "#two"))
    popped = relay.pending.popleft()
    relay.disconnect()
    assert not relay.pending
    assert not relay.valid(popped)
    relay.begin_connection()
    relay.channel_ready("#one")
    assert not relay.valid(popped)
    assert relay.offer(ANNOUNCEMENT, ("#one",)) == 1


def test_expiry_releases_capacity_and_prevents_send():
    now = [0.0]
    relay = ready(RelayConfig(capacity=1, expiry_seconds=1), lambda: now[0])
    relay.offer(ANNOUNCEMENT, ("#one",))
    item = relay.pending[0]
    now[0] = 1
    assert not relay.valid(item)
    assert relay.offer(ANNOUNCEMENT, ("#two",)) == 1
    assert relay.pending[0].channel == "#two"


def test_dedupe_scope_capacity_expiry_and_restart():
    now = [0.0]
    relay = ready(RelayConfig(dedupe_capacity=2, dedupe_seconds=1), lambda: now[0])
    assert not relay.duplicate("hook", "one")
    assert relay.duplicate("hook", "one")
    assert not relay.duplicate("other", "one")
    assert not relay.duplicate("hook", "two")
    assert len(relay.seen) == 2
    assert not relay.duplicate("hook", "one")  # oldest was evicted
    now[0] = 1
    assert not relay.duplicate("hook", "one")
    assert not Relay(RelayConfig()).duplicate("hook", "one")


async def test_dispatch_paces_and_close_wakes_worker():
    relay = ready(RelayConfig(send_interval=0.02))
    times = []
    completed = asyncio.Event()

    async def send(item):
        times.append(asyncio.get_running_loop().time())
        if len(times) == 2:
            completed.set()

    relay.sender = send
    relay.offer(ANNOUNCEMENT, ("#one", "#two"))
    task = asyncio.create_task(relay.run())
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert times[1] - times[0] >= 0.019
    finally:
        relay.close()
        await task
    assert relay.offer(ANNOUNCEMENT, ("#one",)) == 0


async def test_dispatch_drops_item_invalidated_during_rate_wait():
    relay = ready(RelayConfig(send_interval=0.05))
    sent = []
    first = asyncio.Event()

    async def send(item):
        sent.append(item)
        first.set()

    relay.sender = send
    relay.offer(ANNOUNCEMENT, ("#one", "#two"))
    task = asyncio.create_task(relay.run())
    try:
        await asyncio.wait_for(first.wait(), 1)
        relay.disconnect()
        relay.begin_connection()
        relay.channel_ready("#two")
        await asyncio.sleep(0.07)
        assert len(sent) == 1
    finally:
        relay.close()
        await task


async def test_sender_programming_failure_propagates():
    relay = ready()

    async def fail(item):
        raise RuntimeError("bug")

    relay.sender = fail
    relay.offer(ANNOUNCEMENT, ("#one",))
    with pytest.raises(RuntimeError, match="bug"):
        await relay.run()
    relay.close()


def test_expired_guard_cannot_be_refreshed_by_channel_readiness():
    relay = ready()
    relay.offer(ANNOUNCEMENT, ("#one",))
    item = replace(relay.pending[0], deadline=0)
    assert not relay.valid(item)
