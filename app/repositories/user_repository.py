from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user_model import User


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_user(self):
        user = User()
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def get_user(self, user_id: str):
        result = await self.db.execute(select(User).filter(User.id == user_id))
        return result.scalars().first()

    async def update_user(self, user_id: str, data: dict):
        result = await self.db.execute(select(User).filter(User.id == user_id))
        user = result.scalars().first()
        if not user:
            return None

        for key, value in data.items():
            setattr(user, key, value)

        await self.db.commit()
        await self.db.refresh(user)
        return user