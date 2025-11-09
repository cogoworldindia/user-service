import uuid
from datetime import datetime
from sqlalchemy import Column, String, Enum, DateTime
from app.db.session import Base
from enum import Enum as PyEnum
from sqlalchemy.orm import relationship


class UserStatus(str, PyEnum):
    active = "active"
    inactive = "inactive"
    deleted = "deleted"


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(Enum(UserStatus), default=UserStatus.active, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    profile = relationship(
    "UserProfile",
    back_populates="user",
    uselist=False,
    lazy="joined"
    )
