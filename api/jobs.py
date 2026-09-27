# Aftershock — API: job runner (§10.1 SSE progress, §10.2 job cancellation)
#
# "One job per (pr, head_sha); a newer synchronize cancels the older job"
# (§10.2). Job *status* lives on the ChangeSet node in FalkorDB (§5: "job
# status stored on the ChangeSet node — FalkorDB stays the only DB"); this
# module only holds the in-process asyncio.Task handle needed to cancel a
# superseded run and the SSE fan-out queues, which are runtime plumbing, not
# data. Single-process only, by design of the in-memory queues — a
# multi-worker deployment would need to move the fan-out onto FalkorDB's own
# pub/sub (it speaks Redis) or an external broker; noted honestly rather than
# pretended away.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_DONE_SENTINEL = None


@dataclass
class JobHandle:
    task: asyncio.Task
    head_sha: str
    changeset_id: str


class ProgressBus:
    """Fan-out of stage-name events for one changeset_id to any number of
    SSE subscribers (§10.1 GET /changesets/{id}/events)."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[str | None]]] = {}

    def subscribe(self, changeset_id: str) -> asyncio.Queue[str | None]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.setdefault(changeset_id, []).append(queue)
        return queue

    def unsubscribe(self, changeset_id: str, queue: asyncio.Queue[str | None]) -> None:
        subs = self._subscribers.get(changeset_id)
        if subs and queue in subs:
            subs.remove(queue)
            if not subs:
                self._subscribers.pop(changeset_id, None)

    def publish(self, changeset_id: str, stage: str) -> None:
        for queue in self._subscribers.get(changeset_id, []):
            queue.put_nowait(stage)

    def finish(self, changeset_id: str) -> None:
        for queue in self._subscribers.get(changeset_id, []):
            queue.put_nowait(_DONE_SENTINEL)


class JobRegistry:
    """Tracks the single active job per PR number, so a newer `synchronize`
    can cancel a stale one still running against an old head_sha."""

    def __init__(self) -> None:
        self._jobs: dict[int, JobHandle] = {}
        self.progress = ProgressBus()

    def current(self, pr: int) -> JobHandle | None:
        return self._jobs.get(pr)

    def start(
        self,
        pr: int,
        head_sha: str,
        changeset_id: str,
        coro_factory: Callable[[], Awaitable[None]],
    ) -> JobHandle:
        existing = self._jobs.get(pr)
        if existing is not None and existing.head_sha != head_sha and not existing.task.done():
            logger.info("PR #%d: cancelling stale job for %s in favour of %s", pr, existing.head_sha, head_sha)
            existing.task.cancel()
            self.progress.finish(existing.changeset_id)

        async def _run() -> None:
            try:
                await coro_factory()
            except asyncio.CancelledError:
                logger.info("PR #%d job for %s cancelled", pr, head_sha)
                raise
            except Exception:
                logger.exception("PR #%d job for %s failed", pr, head_sha)
            finally:
                self.progress.finish(changeset_id)

        task = asyncio.create_task(_run())
        handle = JobHandle(task=task, head_sha=head_sha, changeset_id=changeset_id)
        self._jobs[pr] = handle
        return handle


_REGISTRY: JobRegistry | None = None


def get_job_registry() -> JobRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = JobRegistry()
    return _REGISTRY
