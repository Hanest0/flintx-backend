"""
FlintX Child Safety API Routes
Content moderation, Kids Corner, age verification.
"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from ..database.connection import get_db
from ..database.models import User, Video
from ..auth.routes import require_verified
from .policy import (
    check_account_eligibility, check_kids_corner_eligibility,
    check_country_allowed, KIDS_CORNER_NICHES, KIDS_CORNER_RULES,
    BLOCKED_COUNTRIES, AgeThreshold
)

router = APIRouter(prefix="/safety", tags=["Child Safety"])


class AgeCheckRequest(BaseModel):
    date_of_birth: str   # YYYY-MM-DD
    country_code:  str


class KidsCornerSubmission(BaseModel):
    video_id:    str
    title:       str
    description: str
    niche:       str
    tags:        list = []


@router.post("/age-check")
def age_check(req: AgeCheckRequest):
    """Check if a user is eligible based on age and country."""
    try:
        dob = date.fromisoformat(req.date_of_birth)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")

    if dob >= date.today():
        raise HTTPException(400, "Invalid date of birth")

    result = check_account_eligibility(dob, req.country_code)
    return result


@router.get("/blocked-countries")
def blocked_countries():
    """List all countries blocked on FlintX."""
    return {
        "blocked": [
            {"code": code, "reason": reason}
            for code, reason in BLOCKED_COUNTRIES.items()
        ],
        "note": "These restrictions are permanent and cannot be overridden."
    }


@router.get("/kids-corner/rules")
def kids_corner_rules():
    """Return the complete Kids Corner ruleset."""
    return {
        "rules":           KIDS_CORNER_RULES,
        "allowed_niches":  KIDS_CORNER_NICHES,
        "max_age":         AgeThreshold.KIDS_CORNER_MAX_AGE,
        "advertising":     "ZERO — no advertising of any kind, ever",
        "moderation":      "AI screening + mandatory human review before any content goes live",
        "data_collection": "Minimal — session only, no persistent data for under-13s",
        "social_features": "None — no comments, likes, shares, follows or recommendations",
        "legal_basis":     "COPPA, GDPR-K, UK Children's Code, Australia Online Safety Act",
    }


@router.get("/kids-corner/feed")
def kids_corner_feed(db: Session = Depends(get_db)):
    """
    Kids Corner video feed.
    Only returns videos that have:
    1. Been submitted for Kids Corner
    2. Passed AI moderation
    3. Passed human review
    4. Been explicitly approved
    """
    try:
        videos = db.query(Video).filter(
            Video.kids_corner == True,
            Video.kids_approved == True,
            Video.status == "active",
        ).order_by(Video.created_at.desc()).limit(50).all()

        return {
            "videos": [
                {
                    "id":          str(v.id),
                    "title":       v.title,
                    "description": v.description,
                    "niche":       v.niche,
                    "duration":    v.duration_seconds,
                    "creator":     v.creator.full_name if v.creator else "FlintX",
                    "thumbnail":   v.thumbnail_url,
                }
                for v in videos
            ],
            "total": len(videos),
            "advertising": "none",
            "safe_mode": True,
        }
    except Exception:
        return {"videos": [], "total": 0, "advertising": "none", "safe_mode": True}


@router.post("/kids-corner/submit")
def submit_kids_corner(
    req: KidsCornerSubmission,
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """Submit a video for Kids Corner review."""
    # Verify creator is 18+ for Kids Corner
    if not user.date_of_birth:
        raise HTTPException(403, "Age verification required to submit Kids Corner content.")

    try:
        dob = date.fromisoformat(str(user.date_of_birth))
        age = (date.today() - dob).days // 365
        if age < AgeThreshold.MINIMUM_PAYOUT_AGE:
            raise HTTPException(403, "Kids Corner creators must be 18+.")
    except (ValueError, TypeError):
        raise HTTPException(403, "Valid date of birth required.")

    # Content check
    check = check_kids_corner_eligibility(req.title, req.description, req.niche, req.tags)

    if not check["eligible"]:
        return {
            "accepted": False,
            "reason": "Content does not meet Kids Corner standards.",
            "flags": check["flags"],
            "eligible_niches": KIDS_CORNER_NICHES,
        }

    # Queue for human review
    try:
        video = db.query(Video).filter(Video.id == req.video_id).first()
        if video and str(video.user_id) == str(user.id):
            video.kids_corner = True
            video.kids_approved = False  # Awaits human review
            video.kids_review_status = "pending_human_review"
            db.commit()
    except Exception:
        pass

    return {
        "accepted": True,
        "status": "pending_human_review",
        "message": "Your video has passed initial screening and is queued for human review. This takes 24-48 hours.",
        "requires_human_review": True,
        "can_auto_approve": False,
    }


@router.get("/kids-corner/pending")
def kids_pending_review(
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """Admin: list videos pending Kids Corner human review."""
    if user.role not in ["admin", "moderator"]:
        raise HTTPException(403, "Admin access required.")

    try:
        pending = db.query(Video).filter(
            Video.kids_corner == True,
            Video.kids_approved == False,
            Video.kids_review_status == "pending_human_review",
        ).all()

        return {
            "pending": [
                {
                    "id":           str(v.id),
                    "title":        v.title,
                    "creator":      v.creator.full_name if v.creator else "Unknown",
                    "submitted_at": str(v.updated_at),
                    "niche":        v.niche,
                }
                for v in pending
            ],
            "count": len(pending),
        }
    except Exception:
        return {"pending": [], "count": 0}


@router.post("/kids-corner/approve/{video_id}")
def approve_kids_corner(
    video_id: str,
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """Admin: approve a video for Kids Corner after human review."""
    if user.role not in ["admin", "moderator"]:
        raise HTTPException(403, "Admin access required.")

    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(404, "Video not found.")
        video.kids_approved = True
        video.kids_review_status = "approved"
        db.commit()
        return {"approved": True, "video_id": video_id}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/kids-corner/reject/{video_id}")
def reject_kids_corner(
    video_id: str,
    reason: str = "Does not meet Kids Corner standards",
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """Admin: reject a video from Kids Corner."""
    if user.role not in ["admin", "moderator"]:
        raise HTTPException(403, "Admin access required.")

    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise HTTPException(404, "Video not found.")
        video.kids_corner = False
        video.kids_approved = False
        video.kids_review_status = f"rejected: {reason}"
        db.commit()
        return {"rejected": True, "video_id": video_id, "reason": reason}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Parental Consent ──────────────────────────────────────────────────
from pydantic import BaseModel as _BaseModel

class ParentalConsentRequest(_BaseModel):
    parentName:    str
    parentEmail:   str
    parentDob:     str
    parentPhone:   str = ""
    relationship:  str = "parent"
    child_age:     int
    agreeTerms:    bool = False
    agreeModeration: bool = False
    agreeRevenue:  bool = False
    agreeContent:  bool = False


@router.post("/parental-consent")
def submit_parental_consent(
    req: ParentalConsentRequest,
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """
    Record parental consent for a minor creator account.
    Parent must be 18+. All four agreements must be true.
    """
    # Verify all agreements
    if not all([req.agreeTerms, req.agreeModeration, req.agreeRevenue, req.agreeContent]):
        raise HTTPException(400, "All consent items must be agreed to.")

    # Verify parent age
    try:
        from datetime import date as _date
        parent_dob = _date.fromisoformat(req.parentDob)
        parent_age = (_date.today() - parent_dob).days // 365
        if parent_age < 18:
            raise HTTPException(400, "Parent or guardian must be 18 or older.")
    except ValueError:
        raise HTTPException(400, "Invalid date of birth format.")

    # Record consent on user account
    try:
        import json as _json
        from datetime import datetime as _dt
        consent_record = {
            "parent_name":     req.parentName,
            "parent_email":    req.parentEmail,
            "relationship":    req.relationship,
            "consented_at":    _dt.utcnow().isoformat(),
            "child_age":       req.child_age,
            "ip_timestamp":    _dt.utcnow().isoformat(),
            "agreements": {
                "terms":       req.agreeTerms,
                "moderation":  req.agreeModeration,
                "revenue":     req.agreeRevenue,
                "content":     req.agreeContent,
            }
        }
        user.parental_consent = _json.dumps(consent_record)
        user.parental_consent_given = True
        db.commit()
    except Exception as e:
        raise HTTPException(500, f"Could not record consent: {e}")

    # Determine moderation level for this child
    from .policy import ModerationLevel, RevenueShare
    mod_level = ModerationLevel.for_age(req.child_age)
    revenue   = RevenueShare.for_age(req.child_age)

    return {
        "consent_recorded":   True,
        "child_age":          req.child_age,
        "moderation_level":   mod_level,
        "moderation_description": ModerationLevel.description(mod_level),
        "revenue_share":      revenue,
        "payout_to":          "parent_guardian",
        "note":               "Consent recorded. Account is now active with enhanced moderation.",
    }
