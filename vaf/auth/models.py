"""
SQLAlchemy models for VAF Local Network Authentication.

Tables:
- local_users: Local user accounts (Argon2 password, optional TOTP 2FA)
- user_sessions: Active JWT sessions with device info
"""

import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    LargeBinary,
    Boolean,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from vaf.memory.models import Base


class UserRole(str, Enum):
    """Role for RBAC."""

    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"


class LocalUser(Base):
    """
    Local user account for network access.

    Passwords hashed with Argon2id. TOTP secrets encrypted at rest (AES-256-GCM).
    """

    __tablename__ = "local_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)  # Argon2
    role = Column(String(20), nullable=False, default="user")  # admin, user, guest
    totp_secret = Column(LargeBinary, nullable=True)  # Encrypted
    totp_nonce = Column(LargeBinary, nullable=True)
    permissions = Column(JSONB, default=dict)  # {"tools": [], "workflows": []}
    user_scope_id = Column(UUID(as_uuid=True), unique=True, default=uuid.uuid4)
    is_active = Column(Boolean, default=True)
    requires_2fa_setup = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, nullable=True)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<LocalUser(username='{self.username}', role='{self.role}')>"


class UserSession(Base):
    """
    Active session for a JWT token.

    token_hash is SHA-256 of the JWT for lookup; device_info stores ip, user_agent, device_type.
    """

    __tablename__ = "user_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("local_users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, index=True)  # SHA-256 of JWT
    device_info = Column(JSONB)  # {ip, user_agent, device_type}
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    last_activity = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True)
    is_2fa_verified = Column(Boolean, default=False, nullable=False)

    user = relationship("LocalUser", back_populates="sessions")


Index("ix_user_sessions_user_id", UserSession.user_id)
Index("ix_user_sessions_expires", UserSession.expires_at)
