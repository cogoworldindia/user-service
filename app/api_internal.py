"""`/internal/admin/*` for admin-service (spec §6.4): service token from admin-service only,
with the acting admin's id in `X-Admin-Id` (logged here; admin-service keeps the audit log)."""

from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from cogo_common.auth.service_tokens import ServiceIdentity, require_service
from cogo_common.responses import Envelope, ok
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import AdminUsers
from app.models import User
from app.service import age_of

router = APIRouter(prefix="/internal/admin", tags=["internal"], include_in_schema=False)
AdminService = Annotated[ServiceIdentity, Depends(require_service("admin-service"))]
AdminId = Annotated[UUID, Header(alias="X-Admin-Id")]


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.db.session():
        yield session


def _admin(session: Annotated[AsyncSession, Depends(_session)]) -> AdminUsers:
    return AdminUsers(session)


Admin = Annotated[AdminUsers, Depends(_admin)]


class UserBriefOut(BaseModel):
    id: UUID
    name: str | None
    username: str | None
    avatar_url: str | None
    account_status: str
    kyc_level: str


class AdminUserOut(BaseModel):
    id: UUID
    username: str | None
    name: str | None
    dob: date | None
    age: int | None
    gender: str | None
    bio: str | None
    city: str | None
    avatar_url: str | None
    account_status: str
    suspended_until: datetime | None
    kyc_level: str
    kyc_review_status: str | None
    official_email: str | None
    gov_id_type: str | None
    gov_id_last4: str | None
    stats: dict[str, int]
    created_at: datetime
    deleted_at: datetime | None
    deletion_completed_at: datetime | None


def brief(user: User) -> UserBriefOut:
    return UserBriefOut(
        id=user.id,
        name=user.name,
        username=user.username,
        avatar_url=user.avatar_url,
        account_status=user.account_status.value,
        kyc_level=user.kyc_level,
    )


def admin_user(user: User) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        name=user.name,
        dob=user.dob,
        age=age_of(user.dob),
        gender=user.gender.value if user.gender else None,
        bio=user.bio,
        city=user.city_name,
        avatar_url=user.avatar_url,
        account_status=user.account_status.value,
        suspended_until=user.suspended_until,
        kyc_level=user.kyc_level,
        kyc_review_status=user.kyc_review_status,
        official_email=user.official_email if user.email_verified_at else None,
        gov_id_type=user.gov_id_type,
        gov_id_last4=user.gov_id_last4,
        stats={
            "followers": user.followers_count,
            "following": user.following_count,
            "journeys": user.journeys_count,
        },
        created_at=user.created_at,
        deleted_at=user.deleted_at,
        deletion_completed_at=user.deletion_completed_at,
    )


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["suspend", "ban", "reinstate"]
    until: datetime | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class KycReviewOut(BaseModel):
    user_id: UUID
    name: str | None
    username: str | None
    dob: date | None
    avatar_url: str | None
    submitted_at: datetime
    attempt_id: UUID
    result: dict[str, object]


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Annotated[str, Field(min_length=1, max_length=500)]


class ReviewOut(BaseModel):
    user_id: UUID
    kyc_level: str
    kyc_review_status: str | None


class MediaOut(BaseModel):
    id: UUID
    owner: UserBriefOut | None
    kind: str
    url: str
    status: str


@router.get("/users", response_model=Envelope[list[UserBriefOut]])
async def search_users(
    _: AdminService, admin: Admin, q: Annotated[str, Query(max_length=100)]
) -> Envelope[list[UserBriefOut]]:
    return ok([brief(u) for u in await admin.search(q)])


@router.get("/users/{user_id}", response_model=Envelope[AdminUserOut])
async def get_user(user_id: UUID, _: AdminService, admin: Admin) -> Envelope[AdminUserOut]:
    return ok(admin_user(await admin.get(user_id)))


@router.post("/users/{user_id}/status", response_model=Envelope[AdminUserOut])
async def set_status(
    user_id: UUID, body: StatusIn, _: AdminService, admin: Admin, admin_id: AdminId
) -> Envelope[AdminUserOut]:
    user = await admin.set_status(user_id, body.action, until=body.until, admin_id=admin_id)
    return ok(admin_user(user))


@router.get("/kyc-reviews", response_model=Envelope[list[KycReviewOut]])
async def kyc_reviews(
    _: AdminService,
    admin: Admin,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,  # internal: admin-service pages with limit + 1
    after_at: datetime | None = None,
    after_id: UUID | None = None,
) -> Envelope[list[KycReviewOut]]:
    after = (after_at, after_id) if after_at and after_id else None
    rows = await admin.kyc_reviews(limit=limit, after=after)
    return ok(
        [
            KycReviewOut(
                user_id=u.id,
                name=u.name,
                username=u.username,
                dob=u.dob,
                avatar_url=u.avatar_url,
                submitted_at=a.updated_at,
                attempt_id=a.id,
                result=dict(a.result or {}),
            )
            for u, a in rows
        ]
    )


@router.post("/kyc-reviews/{user_id}/{decision}", response_model=Envelope[ReviewOut])
async def review(
    user_id: UUID,
    decision: Literal["approve", "reject"],
    body: ReviewIn,
    _: AdminService,
    admin: Admin,
    admin_id: AdminId,
) -> Envelope[ReviewOut]:
    user = await admin.review(user_id, approve=decision == "approve", admin_id=admin_id)
    return ok(
        ReviewOut(
            user_id=user.id, kyc_level=user.kyc_level, kyc_review_status=user.kyc_review_status
        )
    )


@router.get("/media/{media_id}", response_model=Envelope[MediaOut])
async def get_media(
    request: Request, media_id: UUID, _: AdminService, admin: Admin
) -> Envelope[MediaOut]:
    upload = await admin.media(media_id)
    try:
        owner = brief(await admin.get(upload.user_id))
    except Exception:
        owner = None
    return ok(
        MediaOut(
            id=upload.id,
            owner=owner,
            kind=upload.kind,
            url=request.app.state.media.public_url(upload.object_key),
            status=upload.status,
        )
    )


@router.post("/media/{media_id}/remove", response_model=Envelope[None])
async def remove_media(
    request: Request, media_id: UUID, _: AdminService, admin: Admin, admin_id: AdminId
) -> Envelope[None]:
    await admin.remove_media(media_id, media=request.app.state.media, admin_id=admin_id)
    return ok(None)
