"""
Flint — Auth Security
JWT tokens, password hashing, email/reset token management.
"""

import os
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
import bcrypt as _bcrypt
from sqlalchemy.orm import Session

from ..database.models import User, RefreshToken, AccountStatus

SECRET_KEY       = os.getenv("SECRET_KEY", "change-this-in-production-minimum-32-chars")
ALGORITHM        = "HS256"
ACCESS_EXPIRE_M  = 30          # 30 minutes
REFRESH_EXPIRE_D = 30          # 30 days

# bcrypt used directly — no passlib CryptContext needed


# ─────────────────────────────────────────────
# PASSWORDS
# ─────────────────────────────────────────────

def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, ""


# ─────────────────────────────────────────────
# ACCESS TOKENS (JWT, short-lived)
# ─────────────────────────────────────────────

def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_EXPIRE_M)
    return jwt.encode(
        {"sub": user_id, "role": role, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ─────────────────────────────────────────────
# REFRESH TOKENS (opaque, stored in DB)
# ─────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(db: Session, user_id: str, ip_address: str = "", user_agent: str = "") -> str:
    raw_token = secrets.token_urlsafe(64)
    expires   = datetime.utcnow() + timedelta(days=REFRESH_EXPIRE_D)

    db.add(RefreshToken(
        user_id    = user_id,
        token_hash = _hash_token(raw_token),
        expires_at = expires,
        ip_address = ip_address,
        user_agent = user_agent,
    ))
    db.commit()
    return raw_token


def validate_refresh_token(db: Session, raw_token: str) -> Optional[User]:
    token_hash = _hash_token(raw_token)
    record = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.revoked    == False,
        RefreshToken.expires_at >= datetime.utcnow(),
    ).first()
    if not record:
        return None
    user = db.query(User).filter(User.id == record.user_id).first()
    if not user or user.status == AccountStatus.banned:
        return None
    return user


def revoke_refresh_token(db: Session, raw_token: str):
    token_hash = _hash_token(raw_token)
    db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).update({"revoked": True})
    db.commit()


def revoke_all_user_tokens(db: Session, user_id: str):
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update({"revoked": True})
    db.commit()


# ─────────────────────────────────────────────
# EMAIL VERIFICATION TOKENS
# ─────────────────────────────────────────────

def create_email_verify_token(db: Session, user: User) -> str:
    token   = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=48)
    user.email_verify_token   = token
    user.email_verify_expires = expires
    db.commit()
    return token


def verify_email_token(db: Session, token: str) -> Optional[User]:
    user = db.query(User).filter(
        User.email_verify_token   == token,
        User.email_verify_expires >= datetime.utcnow(),
    ).first()
    if not user:
        return None
    user.email_verified       = True
    user.email_verify_token   = None
    user.email_verify_expires = None
    user.status               = AccountStatus.active
    db.commit()
    return user


# ─────────────────────────────────────────────
# PASSWORD RESET TOKENS
# ─────────────────────────────────────────────

def create_password_reset_token(db: Session, user: User) -> str:
    token   = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=2)
    user.password_reset_token   = token
    user.password_reset_expires = expires
    db.commit()
    return token


def validate_password_reset_token(db: Session, token: str) -> Optional[User]:
    return db.query(User).filter(
        User.password_reset_token   == token,
        User.password_reset_expires >= datetime.utcnow(),
    ).first()
