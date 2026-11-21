"""user-service's periodic work: suspensions that ran out, and the deletion tracker. Safe on
several replicas at once (row locks with SKIP LOCKED; the tracker is idempotent)."""

import asyncio
import contextlib

from cogo_common.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import AdminUsers
from app.deletion import DeletionTracker

log = get_logger(__name__)


class Jobs:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tracker: DeletionTracker,
        *,
        interval: float = 60.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._tracker = tracker
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> None:
        async with self._sessionmaker() as session:
            await AdminUsers(session).expire_suspensions()
        await self._tracker.run_once()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("user_jobs_failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="user-jobs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
