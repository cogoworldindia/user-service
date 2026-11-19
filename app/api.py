import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from cogo_common.auth import CurrentUser
from cogo_common.responses import Envelope, ok
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import ChatClient, PlacesClient, SocialClient
from app.models import KycLevel, User
from app.schemas import (
    CityOut,
    CoverIn,
    CoverOut,
    KycPromptOut,
    LegacyMeOut,
    LegacyProfileOut,
    LegacyProfileUpdate,
    MediaOut,
    MePatch,
    MeProfileOut,
    PublicProfileOut,
    StatsOut,
    TagOut,
    UploadUrlIn,
    UploadUrlOut,
)
from app.service import UserService, age_of

router = APIRouter(prefix="/v1", tags=["users"])


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def _service(request: Request, session: Annotated[AsyncSession, Depends(_session)]) -> UserService:
    return UserService(session, media=request.app.state.media)


Users = Annotated[UserService, Depends(_service)]


def _places(request: Request) -> PlacesClient:
    places: PlacesClient = request.app.state.places
    return places


def _chat(request: Request) -> ChatClient:
    chat: ChatClient = request.app.state.chat
    return chat


def _social(request: Request) -> SocialClient:
    social: SocialClient = request.app.state.social
    return social


# ---------------------------------------------------------------- builders
def _covers(user: User) -> list[CoverOut]:
    return [
        CoverOut(position=c.position, url=c.url) for c in user.covers if not c.moderation_hidden
    ]


def _stats(user: User) -> StatsOut:
    return StatsOut(
        journeys=user.journeys_count, followers=user.followers_count, following=user.following_count
    )


def _kyc_prompt(user: User) -> KycPromptOut:
    snoozed = user.kyc_prompt_snoozed_until and user.kyc_prompt_snoozed_until > datetime.now(UTC)
    show = user.kyc_level != KycLevel.FULL.value and not snoozed
    variant = "none" if user.kyc_level == KycLevel.NONE.value else "partial_need_id"
    return KycPromptOut(show=bool(show), variant=variant)


async def _me(user: User, users: UserService) -> MeProfileOut:
    tags = await users.tags(list(user.personality_tag_ids or []))
    return MeProfileOut(
        id=user.id,
        username=user.username,
        name=user.name,
        age=age_of(user.dob),
        dob=user.dob,
        gender=user.gender,
        bio=user.bio,
        profession=user.profession,
        city=CityOut(name=user.city_name, place_id=user.city_place_id) if user.city_name else None,
        avatar_url=user.avatar_url,
        cover_photos=_covers(user),
        personality_tags=[TagOut.model_validate(t) for t in tags],
        stats=_stats(user),
        kyc_level=user.kyc_level,
        kyc_prompt=_kyc_prompt(user),
        guide_seen=user.guide_seen_at is not None,
        account_status=user.account_status,
        created_at=user.created_at,
    )


def _legacy(user: User) -> LegacyMeOut:
    has_profile = any(
        v is not None for v in (user.name, user.dob, user.gender, user.bio, user.avatar_url)
    )
    profile = (
        LegacyProfileOut(
            full_name=user.name,
            date_of_birth=user.dob,
            gender=user.gender,
            bio=user.bio,
            profile_photo_url=user.avatar_url,
            updated_at=user.updated_at,
        )
        if has_profile
        else None
    )
    return LegacyMeOut(
        id=user.id, status=user.account_status, created_at=user.created_at, profile=profile
    )


# ---------------------------------------------------------------- /v1/me (spec §6.2)
@router.get("/me", response_model=Envelope[MeProfileOut])
async def get_me(principal: CurrentUser, users: Users) -> Envelope[MeProfileOut]:
    return ok(await _me(await users.get_me(principal.user_id), users))


@router.patch("/me", response_model=Envelope[MeProfileOut])
async def patch_me(
    body: MePatch,
    principal: CurrentUser,
    users: Users,
    places: Annotated[PlacesClient, Depends(_places)],
) -> Envelope[MeProfileOut]:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    if changes.get("avatar_url") is not None:
        changes["avatar_url"] = str(changes["avatar_url"])
    place = await places.resolve(changes["city_place_id"]) if changes.get("city_place_id") else None
    user = await users.update_me(principal.user_id, changes, place=place)
    return ok(await _me(user, users), message="Profile updated")


@router.put("/me/cover-photos", response_model=Envelope[list[CoverOut]])
async def put_covers(
    body: list[CoverIn], principal: CurrentUser, users: Users
) -> Envelope[list[CoverOut]]:
    if len(body) > 3:
        from cogo_common.errors import BadRequest

        raise BadRequest("At most 3 cover photos", error_type="invalid_covers")
    user = await users.set_covers(principal.user_id, [(c.position, str(c.url)) for c in body])
    return ok(_covers(user), message="Cover photos updated")


@router.post("/me/guide/seen", response_model=Envelope[None])
async def guide_seen(principal: CurrentUser, users: Users) -> Envelope[None]:
    await users.mark_guide_seen(principal.user_id)
    return ok(None)


@router.get("/personality-tags", response_model=Envelope[list[TagOut]])
async def personality_tags(_: CurrentUser, users: Users) -> Envelope[list[TagOut]]:
    return ok([TagOut.model_validate(t) for t in await users.tags()])


@router.post("/media/upload-url", response_model=Envelope[UploadUrlOut])
async def upload_url(
    request: Request, body: UploadUrlIn, principal: CurrentUser, users: Users
) -> Envelope[UploadUrlOut]:
    upload, url, expires_at = await users.create_upload(
        principal.user_id, body.kind, body.content_type
    )
    media = request.app.state.media
    return ok(
        UploadUrlOut(
            media_id=upload.id,
            upload_url=url,
            file_url=media.public_url(upload.object_key),
            expires_at=expires_at,
            max_bytes=media.max_bytes,
        )
    )


@router.post("/media/{media_id}/confirm", response_model=Envelope[MediaOut])
async def confirm_upload(
    request: Request, media_id: UUID, principal: CurrentUser, users: Users
) -> Envelope[MediaOut]:
    upload = await users.confirm_upload(principal.user_id, media_id)
    return ok(
        MediaOut(
            media_id=upload.id,
            file_url=request.app.state.media.public_url(upload.object_key),
            status=upload.status,
        )
    )


# ---------------------------------------------------------------- deprecated aliases (app v0)
@router.get("/users/me", response_model=Envelope[LegacyMeOut], deprecated=True)
async def legacy_get_me(principal: CurrentUser, users: Users) -> Envelope[LegacyMeOut]:
    return ok(_legacy(await users.get_me(principal.user_id)))


@router.patch("/users/me/profile", response_model=Envelope[LegacyMeOut], deprecated=True)
async def legacy_update_profile(
    body: LegacyProfileUpdate, principal: CurrentUser, users: Users
) -> Envelope[LegacyMeOut]:
    mapping = {
        "full_name": "name",
        "date_of_birth": "dob",
        "gender": "gender",
        "bio": "bio",
        "profile_photo_url": "avatar_url",
    }
    changes = {mapping[k]: v for k, v in body.model_dump(exclude_unset=True).items()}
    if changes.get("avatar_url") is not None:
        changes["avatar_url"] = str(changes["avatar_url"])
    return ok(_legacy(await users.update_me(principal.user_id, changes)), message="Profile updated")


# ---------------------------------------------------------------- other users
@router.get("/users/{user_id}", response_model=Envelope[PublicProfileOut])
async def get_user(
    user_id: UUID,
    principal: CurrentUser,
    users: Users,
    social: Annotated[SocialClient, Depends(_social)],
    chat: Annotated[ChatClient, Depends(_chat)],
) -> Envelope[PublicProfileOut]:
    user = await users.get_public(user_id, viewer_id=principal.user_id)
    tags = await users.tags(list(user.personality_tag_ids or []))
    following, connection = None, None
    if user_id != principal.user_id:  # both relation calls in parallel, null on failure
        follows, connections = await asyncio.gather(
            social.follow_status(principal.user_id, [user_id]),
            chat.connection_status(principal.user_id, [user_id]),
        )
        following = follows.get(user_id, False) if follows is not None else None
        connection = connections.get(user_id, "none") if connections is not None else None
    return ok(
        PublicProfileOut(
            id=user.id,
            username=user.username,
            name=user.name,
            age=age_of(user.dob),
            avatar_url=user.avatar_url,
            cover_photos=_covers(user),
            bio=user.bio,
            profession=user.profession,
            city=CityOut(name=user.city_name) if user.city_name else None,
            personality_tags=[TagOut.model_validate(t) for t in tags],
            stats=_stats(user),
            kyc_level=user.kyc_level,
            is_following=following,
            connection_status=connection,
        )
    )
