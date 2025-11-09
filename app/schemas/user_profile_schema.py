from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum


class Gender(str, Enum):
    male = "male"
    female = "female"
    other = "other"


class UserProfileBase(BaseModel):
    full_name: Optional[str] = Field(None, example="Arjeet Tekam")
    date_of_birth: Optional[date] = Field(None, example="1998-06-14")
    gender: Optional[Gender] = None
    profile_photo: Optional[str] = Field(None, example="https://example.com/avatar.png")


class UserProfileCreate(UserProfileBase):
    pass


class UserProfileUpdate(UserProfileCreate):
    pass


class UserProfileResponse(UserProfileBase):
    id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
