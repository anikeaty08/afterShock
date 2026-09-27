# Tests for api/jobs.py's job cancellation and SSE fan-out. Pure asyncio,
# no FalkorDB needed.

from __future__ import annotations

import asyncio

from api.jobs import JobRegistry


async def test_newer_synchronize_cancels_the_older_job():
    registry = JobRegistry()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_job():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    registry.start(pr=1, head_sha="sha1", changeset_id="pr-1-sha1", coro_factory=slow_job)
    await started.wait()

    async def fast_job():
        pass

    registry.start(pr=1, head_sha="sha2", changeset_id="pr-1-sha2", coro_factory=fast_job)
    await asyncio.sleep(0.05)

    assert cancelled.is_set()
    assert registry.current(1).head_sha == "sha2"


async def test_same_head_sha_does_not_cancel_itself():
    registry = JobRegistry()
    ran = asyncio.Event()

    async def job():
        await asyncio.sleep(0.01)
        ran.set()

    handle1 = registry.start(pr=2, head_sha="samesha", changeset_id="pr-2-samesha", coro_factory=job)
    await asyncio.sleep(0.05)
    assert ran.is_set()
    assert not handle1.task.cancelled()


async def test_progress_bus_publishes_and_finishes():
    registry = JobRegistry()
    queue = registry.progress.subscribe("cs-1")
    registry.progress.publish("cs-1", "copy")
    registry.progress.publish("cs-1", "apply")
    registry.progress.finish("cs-1")

    assert await queue.get() == "copy"
    assert await queue.get() == "apply"
    assert await queue.get() is None  # sentinel


async def test_job_completion_always_publishes_finish_sentinel():
    registry = JobRegistry()
    queue = registry.progress.subscribe("pr-3-shaX")

    async def job():
        registry.progress.publish("pr-3-shaX", "copy")

    registry.start(pr=3, head_sha="shaX", changeset_id="pr-3-shaX", coro_factory=job)
    assert await queue.get() == "copy"
    assert await queue.get() is None
