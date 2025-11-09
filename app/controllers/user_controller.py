from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.encoders import jsonable_encoder
from app.db.session import get_db
from app.services.user_service import UserService
from app.schemas.user_schema import UserCreate, UserResponse, UserUpdate
from app.schemas.user_profile_schema import UserProfileCreate, UserProfileResponse, UserProfileUpdate
from app.utils.response_helper import success_response, error_response

router = APIRouter(prefix="", tags=["Users"])


# Create User
@router.post("", response_model=None)
async def create_user(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    try:
        user = await service.create_user(user_data)
        return success_response(
            message="User created successfully",
            data=jsonable_encoder(user),
            code=201,
        )
    except Exception as e:
        return error_response(
            message="Failed to create user",
            error=str(e),
            http_status=500,
            code=500
        )


# Get User (with full profile if exists)
@router.get("/{user_id}", response_model=None)
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    try:
        user = await service.get_user(user_id)
        if not user:
            return error_response(
                message="User not found",
                http_status=404,
                code=404
            )
        return success_response(
            message="User fetched successfully",
            data=jsonable_encoder(user),
            code=202
        )
    except Exception as e:
        return error_response(
            message="Error fetching user details",
            error=str(e),
            http_status=500,
            code=500
        )


# Update User
@router.put("/{user_id}", response_model=None)
async def update_user(user_id: str, data: UserUpdate, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    try:
        user = await service.update_user(user_id, data)
        if not user:
            return error_response(
                message="User not found",
                http_status=404,
                code=404
            )
        return success_response(
            message="User updated successfully",
            data=jsonable_encoder(user),
            code=200
        )
    except Exception as e:
        return error_response(
            message="Failed to update user",
            error=str(e),
            http_status=500,
            code=500
        )


# Create Profile
@router.post("/{user_id}/profile", response_model=None)
async def create_profile(user_id: str, data: UserProfileCreate, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    try:
        profile = await service.create_profile(user_id, data)
        return success_response(
            message="User profile created successfully",
            data=jsonable_encoder(profile),
            code=200
        )
    except Exception as e:
        return error_response(
            message="Failed to create user profile",
            error=str(e),
            http_status=500,
            code=500
        )


# Update Profile
@router.put("/{user_id}/profile", response_model=None)
async def update_profile(user_id: str, data: UserProfileUpdate, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    try:
        profile = await service.update_profile(user_id, data)
        if not profile:
            return error_response(
                message="Profile not found",
                http_status=404,
                code=404
            )
        return success_response(
            message="Profile updated successfully",
            data=jsonable_encoder(profile),
            code=200
        )
    except Exception as e:
        return error_response(
            message="Failed to update profile",
            error=str(e),
            http_status=500,
            code=500
        )
