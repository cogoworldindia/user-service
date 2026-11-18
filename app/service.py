from typing import Any
from uuid import UUID

from cogo_common.errors import BadRequest, Forbidden, NotFound
from cogo_common.events.inbox import claim_event
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import User, UserProfile, UserStatus


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _ensure_user(self, user_id: UUID) -> None:
        # Idempotent: the row may come from the user.registered event or the first request.
        await self._session.execute(
            insert(User).values(id=user_id).on_conflict_do_nothing(index_elements=[User.id])
        )

    async def _load(self, user_id: UUID) -> User | None:
        return await self._session.scalar(
            select(User)
            .options(selectinload(User.profile))
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
        )

    async def get_me(self, user_id: UUID) -> User:
        async with self._session.begin():
            await self._ensure_user(user_id)
            user = await self._load(user_id)
        assert user is not None
        if user.status == UserStatus.DELETED:
            raise NotFound("User not found")
        return user

    async def update_profile(self, user_id: UUID, changes: dict[str, Any]) -> User:
        if not changes:
            raise BadRequest("No profile fields to update", error_type="empty_update")
        async with self._session.begin():
            await self._ensure_user(user_id)
            status = await self._session.scalar(select(User.status).where(User.id == user_id))
            if status != UserStatus.ACTIVE:
                raise Forbidden("Account is not active", error_type="account_inactive")
            await self._session.execute(
                insert(UserProfile)
                .values(user_id=user_id, **changes)
                .on_conflict_do_update(
                    index_elements=[UserProfile.user_id],
                    set_={**changes, "updated_at": func.now()},
                )
            )
            user = await self._load(user_id)
        assert user is not None
        return user

    async def get_public(self, user_id: UUID) -> User:
        user = await self._load(user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise NotFound("User not found")
        return user

    async def handle_registered(self, event_id: UUID, user_id: UUID) -> bool:
        """Consumer side of `user.registered`. Returns False for a duplicate delivery."""
        async with self._session.begin():
            if not await claim_event(self._session, event_id, "user.registered"):
                return False
            await self._ensure_user(user_id)
        return True
