import uuid
from datetime import datetime, date
from sqlalchemy import Column, String, Enum, Date, DateTime, ForeignKey, Text
from app.db.session import Base
from enum import Enum as PyEnum
from sqlalchemy.orm import relationship


class Gender(str, PyEnum):
    male = "male"
    female = "female"
    other = "other"


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    full_name = Column(String(255), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(Enum(Gender), nullable=True)
    profile_photo = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = relationship("User", back_populates="profile")
