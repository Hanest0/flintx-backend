"""
Flint — Auth API Routes

POST /api/auth/signup           — create account
POST /api/auth/login            — login, receive tokens
POST /api/auth/logout           — revoke session
POST /api/auth/refresh          — new access token from cookie
POST /api/auth/verify-email     — confirm email address
POST /api/auth/resend-verify    — resend verification email
POST /api/auth/forgot-password  — request reset link
POST /api/auth/reset-password   — set new password
GET  /api/auth/me               — current user profile
PATCH /api/auth/me              — update profile
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, field_validator

from ..database.connection import get_db
from ..database.models import (
    User, CreatorProfile, ViewerProfile, AdvertiserProfile,
    UserRole, AccountStatus,
)
from .security import (
    hash_password, verify_password, validate_password_strength,
    create_access_token, decode_access_token,
    create_refresh_token, validate_refresh_token, revoke_refresh_token,
    revoke_all_user_tokens, create_email_verify_token, verify_email_token,
    create_password_reset_token, validate_password_reset_token,
)
from ..email.service import (
    send_verification_email, send_welcome_email, send_password_reset_email,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

REFRESH_COOKIE = "flintx_refresh"


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class SignupRequest(BaseModel):
    email:          EmailStr
    password:       str
    full_name:      str
    role:           str = "viewer"
    country:        str = ""
    referral_code:  str = ""    # optional — from flintx.tv/join/{code}
    referral_click: str = ""    # optional — click_id from /api/referral/click

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in ("viewer", "creator", "both", "advertiser"):
            raise ValueError("Invalid role")
        return v

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, v):
        if len(v.strip()) < 2:
            raise ValueError("Full name must be at least 2 characters")
        return v.strip()


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token:    str
    password: str


class UpdateProfileRequest(BaseModel):
    full_name:    str | None = None
    country:      str | None = None


# ─────────────────────────────────────────────
# DEPENDENCIES
# ─────────────────────────────────────────────

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_access_token(auth.split(" ")[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user or user.status == AccountStatus.banned:
        raise HTTPException(status_code=401, detail="Account not found or banned")
    return user


def require_verified(user: User = Depends(get_current_user)) -> User:
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email not verified")
    return user


def require_admin(user: User = Depends(require_verified)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _user_dict(user: User) -> dict:
    return {
        "id":             user.id,
        "email":          user.email,
        "full_name":      user.full_name,
        "role":           user.role.value,
        "status":         user.status.value,
        "email_verified": user.email_verified,
        "pass_status":    user.pass_status.value,
        "wallet_balance": user.wallet_balance,
        "created_at":     user.created_at.isoformat(),
    }


def _set_refresh_cookie(response: Response, token: str):
    response.set_cookie(
        key=REFRESH_COOKIE, value=token,
        httponly=True, secure=True, samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )


# ─────────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────────

@router.post("/signup", status_code=201)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == req.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    valid, msg = validate_password_strength(req.password)
    if not valid:
        raise HTTPException(status_code=422, detail=msg)

    # Auto-verify when no RESEND_API_KEY set (launch mode — no email service yet)
    import os as _os
    _auto = not bool(_os.getenv("RESEND_API_KEY", ""))

    user = User(
        email          = req.email.lower(),
        password_hash  = hash_password(req.password),
        full_name      = req.full_name,
        country        = req.country,
        role           = UserRole(req.role),
        email_verified = _auto,
        status         = AccountStatus.active if _auto else AccountStatus.pending,
    )
    db.add(user)
    db.flush()

    if req.role in ("viewer", "both"):
        db.add(ViewerProfile(user_id=user.id))
    if req.role in ("creator", "both"):
        db.add(CreatorProfile(user_id=user.id, tools_active="script,voice,predictor"))
    if req.role == "advertiser":
        db.add(AdvertiserProfile(user_id=user.id))

    db.commit()
    db.refresh(user)

    token = create_email_verify_token(db, user)
    send_verification_email(user.email, user.full_name, token)

    # Tag referral if signup came via a referral link
    if req.referral_code:
        try:
            from ..referral.routes import tag_referral_signup
            tag_referral_signup(db, user.id, req.referral_code, req.referral_click or None)
        except Exception:
            pass

    # Register as founding creator if platform is still in Phase 1
    if req.role in ("creator", "both"):
        try:
            from ..payouts.models import PlatformState, PlatformPhase, FoundingCreator
            from ..payouts.routes import _get_state
            state = _get_state(db)
            if state.phase == PlatformPhase.foundation:
                db.add(FoundingCreator(
                    user_id              = user.id,
                    viewer_count_at_join = state.collective_viewers,
                ))
                db.commit()
        except Exception:
            pass

    return {
        "message": "Account created. Check your email to verify your address.",
        "user_id": user.id,
    }


# ─────────────────────────────────────────────
# VERIFY EMAIL
# ─────────────────────────────────────────────

@router.post("/verify-email")
def verify_email(req: VerifyEmailRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    user = verify_email_token(db, req.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    send_welcome_email(user.email, user.full_name, user.role.value)

    access_token  = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(db, user.id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent", ""))

    _set_refresh_cookie(response, refresh_token)
    user.last_login = datetime.utcnow()
    db.commit()

    return {"access_token": access_token, "token_type": "bearer", "user": _user_dict(user)}


# ─────────────────────────────────────────────
# RESEND VERIFICATION
# ─────────────────────────────────────────────

@router.post("/resend-verify")
def resend_verify(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(
        User.email == req.email.lower(), User.email_verified == False
    ).first()
    if user:
        token = create_email_verify_token(db, user)
        send_verification_email(user.email, user.full_name, token)
    return {"message": "If that email is registered and unverified, we've sent a new link."}


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────

@router.post("/login")
def login(req: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.status == AccountStatus.banned:
        raise HTTPException(status_code=403, detail="This account has been suspended")
    if not user.email_verified and user.status != AccountStatus.active:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in")

    access_token  = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(db, user.id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent", ""))

    _set_refresh_cookie(response, refresh_token)
    user.last_login = datetime.utcnow()
    db.commit()

    return {"access_token": access_token, "token_type": "bearer", "user": _user_dict(user)}


# ─────────────────────────────────────────────
# REFRESH
# ─────────────────────────────────────────────

@router.post("/refresh")
def refresh(request: Request, db: Session = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token")
    user = validate_refresh_token(db, raw)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return {"access_token": create_access_token(user.id, user.role.value), "token_type": "bearer"}


# ─────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────

@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        revoke_refresh_token(db, raw)
    response.delete_cookie(REFRESH_COOKIE)
    return {"message": "Logged out"}


@router.post("/logout-all")
def logout_all(request: Request, response: Response, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    revoke_all_user_tokens(db, user.id)
    response.delete_cookie(REFRESH_COOKIE)
    return {"message": "Logged out from all devices"}


# ─────────────────────────────────────────────
# FORGOT / RESET PASSWORD
# ─────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()
    if user and user.email_verified:
        token = create_password_reset_token(db, user)
        send_password_reset_email(user.email, user.full_name, token)
    return {"message": "If that email is registered, you'll receive a reset link shortly."}


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = validate_password_reset_token(db, req.token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    valid, msg = validate_password_strength(req.password)
    if not valid:
        raise HTTPException(status_code=422, detail=msg)
    user.password_hash          = hash_password(req.password)
    user.password_reset_token   = None
    user.password_reset_expires = None
    revoke_all_user_tokens(db, user.id)
    db.commit()
    return {"message": "Password updated. Please log in with your new password."}


# ─────────────────────────────────────────────
# CURRENT USER
# ─────────────────────────────────────────────

@router.get("/me")
def get_me(user: User = Depends(require_verified)):
    return _user_dict(user)


@router.patch("/me")
def update_me(req: UpdateProfileRequest, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    if req.full_name:
        user.full_name = req.full_name
    if req.country:
        user.country = req.country
    db.commit()
    db.refresh(user)
    return _user_dict(user)
