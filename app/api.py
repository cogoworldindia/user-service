from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from cogo_common.auth import CurrentUser
from cogo_common.responses import Envelope, ok
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import MeOut, ProfileUpdate, PublicUserOut
from app.service import UserService

router = APIRouter(prefix="/v1/users", tags=["users"])


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def _service(session: Annotated[AsyncSession, Depends(_session)]) -> UserService:
    return UserService(session)


Users = Annotated[UserService, Depends(_service)]


# Every route acts on the *verified* caller (token `sub`) — never on an id from the client.
@router.get("/me", response_model=Envelope[MeOut])
async def get_me(principal: CurrentUser, users: Users) -> Envelope[MeOut]:
    user = await users.get_me(principal.user_id)
    return ok(MeOut.model_validate(user))


@router.patch("/me/profile", response_model=Envelope[MeOut])
async def update_my_profile(
    body: ProfileUpdate, principal: CurrentUser, users: Users
) -> Envelope[MeOut]:
    changes = body.model_dump(exclude_unset=True)
    if changes.get("profile_photo_url") is not None:
        changes["profile_photo_url"] = str(changes["profile_photo_url"])
    user = await users.update_profile(principal.user_id, changes)
    return ok(MeOut.model_validate(user), message="Profile updated")


@router.get("/{user_id}", response_model=Envelope[PublicUserOut])
async def get_user(user_id: UUID, _: CurrentUser, users: Users) -> Envelope[PublicUserOut]:
    """Public projection of another user (requires being signed in)."""
    user = await users.get_public(user_id)
    profile = user.profile
    return ok(
        PublicUserOut(
            id=user.id,
            full_name=profile.full_name if profile else None,
            bio=profile.bio if profile else None,
            profile_photo_url=profile.profile_photo_url if profile else None,
        )
    )
