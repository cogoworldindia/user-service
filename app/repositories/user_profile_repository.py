from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user_profile_model import UserProfile
from uuid import UUID


class UserProfileRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_profile(self, user_id: str, data: dict):
        profile = UserProfile(user_id=user_id, **data)
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)
        return profile

    async def get_profile_by_user(self, user_id: str):
        result = await self.db.execute(select(UserProfile).filter(UserProfile.user_id == user_id))
        return result.scalars().first()

    async def update_profile(self, user_id: str, data: dict):
            # Ensure user_id is proper UUID type if DB column is UUID
            try:
                user_uuid = UUID(user_id)
            except ValueError:
                raise ValueError("Invalid user_id format")

            result = await self.db.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
            profile = result.scalars().first()

            if not profile:
                return None

            # Update fields dynamically
            for key, value in data.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)

            # Persist changes
            await self.db.commit()
            await self.db.refresh(profile)
            return profile