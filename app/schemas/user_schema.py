from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from enum import Enum
from app.schemas.user_profile_schema import UserProfileResponse


class UserStatus(str, Enum):
    active = "active"
    inactive = "inactive"
    deleted = "deleted"


class UserBase(BaseModel):
    status: Optional[UserStatus] = UserStatus.active


class UserCreate(UserBase):
    pass  # only used internally (no fields yet, but extendable later)


class UserUpdate(BaseModel):
    status: Optional[UserStatus] = None


class UserResponse(UserBase):
    id: str
    created_at: datetime
    updated_at: datetime
    profile: Optional[UserProfileResponse] = None

    class Config:
        orm_mode = True
