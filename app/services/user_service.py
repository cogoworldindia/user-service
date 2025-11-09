from app.repositories.user_repository import UserRepository
from app.repositories.user_profile_repository import UserProfileRepository
# from app.schemas import UserCreate, UserUpdate, UserProfileCreate, UserProfileUpdate
from app.schemas.user_schema import UserCreate, UserUpdate
from app.schemas.user_profile_schema import UserProfileCreate, UserProfileUpdate
from sqlalchemy.ext.asyncio import AsyncSession


class UserService:
    def __init__(self, db: AsyncSession):
        self.user_repo = UserRepository(db)
        self.profile_repo = UserProfileRepository(db)

    async def create_user(self, data: UserCreate):
        user = await self.user_repo.create_user()
        return user

    async def get_user(self, user_id: str):
        user = await self.user_repo.get_user(user_id)
        user.profile = await self.profile_repo.get_profile_by_user(user_id)
        return user

    async def update_user(self, user_id: str, data: UserUpdate):
        return await self.user_repo.update_user(user_id, data.dict(exclude_unset=True))

    async def create_profile(self, user_id: str, data: UserProfileCreate):
        return await self.profile_repo.create_profile(user_id, data.dict(exclude_unset=True))

    async def update_profile(self, user_id: str, data: UserProfileUpdate):
        return await self.profile_repo.update_profile(user_id, data.dict(exclude_unset=True))
