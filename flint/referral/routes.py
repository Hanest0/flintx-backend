"""
FlintX — Referral & Badge API Routes

Referral Flywheel:
  POST /api/referral/link              — create/get my referral link
  GET  /api/referral/link              — get my referral link + stats
  POST /api/referral/click/{code}      — record a link click (called on landing page)
  POST /api/referral/signup            — tag a signup as referred (called during signup)
  GET  /api/referral/referrals         — my referred users + earnings
  GET  /api/referral/earnings          — referral bonus breakdown
  GET  /api/referral/leaderboard       — top referrers (public)

Badge Programme:
  POST /api/referral/badge             — enrol in badge programme
  GET  /api/referral/badge             — my badges
  PATCH /api/referral/badge/{id}       — update badge (pause/resume, update URL)
  DELETE /api/referral/badge/{id}      — remove badge
  GET  /api/referral/badge/assets      — download badge assets (SVG/PNG)

Studio External Use:
  POST /api/referral/external          — log external content publish
  GET  /api/referral/external          — my external use history + quota
  GET  /api/referral/external/quota    — remaining quota this month

Admin:
  GET  /api/referral/admin/badges      — pending badge verifications
  POST /api/referral/admin/badge/{id}/verify — verify a badge
  GET  /api/referral/admin/stats       — referral programme stats
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import User, Transaction, TxnType
from ..auth.routes import require_verified, require_admin
from ..currency.service import CurrencyService
from .models import (
    ReferralLink, ReferralClick, Referral, ReferralBonus, ReferralStatus,
    CreatorBadge, BadgeCredit, BadgeType, BadgeStatus, ExternalPlatform,
    StudioExternalUse,
)

router = APIRouter(prefix="/referral", tags=["Referral & Badge"])

# Bonus rate: 5% of FlintX's 20% platform share per referred user impression
REFERRAL_BONUS_RATE   = 0.05
REFERRAL_WINDOW_DAYS  = 365     # 12 months
BADGE_MONTHLY_CREDIT  = 500     # $5.00 in USD cents
BADGE_REACH_TIERS = [           # higher reach = higher monthly credit
    (0,        500),             # <100K reach:  $5/mo
    (100_000,  1000),            # 100K–500K:    $10/mo
    (500_000,  2500),            # 500K–1M:      $25/mo
    (1_000_000, 5000),           # 1M+:          $50/mo
]

# External use quotas per studio plan (scripts/month)
EXTERNAL_QUOTAS = {
    "none":   {"script": 0,  "voice": 0,  "opportunity": 0},
    "basic":  {"script": 10, "voice": 5,  "opportunity": 5},
    "pro":    {"script": 50, "voice": 25, "opportunity": 25},
    "agency": {"script": -1, "voice": -1, "opportunity": -1},   # -1 = unlimited
}


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class CreateBadgeRequest(BaseModel):
    badge_type:       str = "made_with"
    platform:         str = "youtube"
    platform_url:     str
    platform_handle:  str = ""
    reported_reach:   int = 0

class UpdateBadgeRequest(BaseModel):
    status:           Optional[str] = None
    platform_url:     Optional[str] = None
    platform_handle:  Optional[str] = None
    reported_reach:   Optional[int] = None

class LogExternalRequest(BaseModel):
    tool:             str               # script | voice | opportunity
    platform:         str
    platform_url:     str = ""
    content_title:    str = ""
    external_views:   int = 0
    external_revenue: int = 0


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _badge_credit_amount(reach: int) -> int:
    """Calculate monthly badge credit based on reported reach."""
    for threshold, credit in reversed(BADGE_REACH_TIERS):
        if reach >= threshold:
            return credit
    return BADGE_MONTHLY_CREDIT


def _referral_dict(r: Referral, currency: str = "USD") -> dict:
    days_remaining = max(0, (r.window_ends - datetime.utcnow()).days) if r.window_ends else 0
    return {
        "id":               r.id,
        "status":           r.status.value,
        "window_ends":      r.window_ends.isoformat() if r.window_ends else None,
        "days_remaining":   days_remaining,
        "bonus_rate":       f"{int(r.bonus_rate*100)}%",
        "total_earned":     CurrencyService.to_display(r.total_bonus_earned, currency),
        "impressions":      r.impressions_generated,
        "signup_source":    r.signup_source,
        "created_at":       r.created_at.isoformat(),
    }


def _badge_dict(b: CreatorBadge, currency: str = "USD") -> dict:
    return {
        "id":              b.id,
        "badge_type":      b.badge_type.value,
        "platform":        b.platform.value,
        "platform_url":    b.platform_url,
        "platform_handle": b.platform_handle,
        "status":          b.status.value,
        "verified":        b.verified,
        "monthly_credit":  CurrencyService.to_display(b.monthly_credit_cents, currency),
        "total_earned":    CurrencyService.to_display(b.total_earned_cents, currency),
        "reported_reach":  b.reported_reach,
        "next_credit_at":  b.next_credit_at.isoformat() if b.next_credit_at else None,
        "created_at":      b.created_at.isoformat(),
    }


def _get_creator_plan(user: User, db: Session) -> str:
    """Get the creator's current studio plan."""
    from ..database.models import CreatorProfile
    profile = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    return profile.studio_plan.value if profile and profile.studio_plan else "none"


# ─────────────────────────────────────────────
# REFERRAL LINK
# ─────────────────────────────────────────────

@router.post("/link", status_code=201)
def create_referral_link(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Create a referral link for the creator. One per creator."""
    existing = db.query(ReferralLink).filter(ReferralLink.creator_id == user.id).first()
    if existing:
        return _link_dict(existing, db)

    # Use creator's handle as the code, fall back to random
    from ..database.models import CreatorProfile
    profile = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    base_code = profile.channel_handle if profile and profile.channel_handle else user.full_name.lower().replace(" ", "")

    # Ensure uniqueness
    code = base_code
    if db.query(ReferralLink).filter(ReferralLink.code == code).first():
        code = f"{base_code}-{secrets.token_urlsafe(4)}"

    link = ReferralLink(creator_id=user.id, code=code)
    db.add(link)
    db.commit()
    db.refresh(link)
    return _link_dict(link, db)


@router.get("/link")
def get_referral_link(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    link = db.query(ReferralLink).filter(ReferralLink.creator_id == user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="No referral link yet. POST /api/referral/link to create one.")
    return _link_dict(link, db, currency)


def _link_dict(link: ReferralLink, db: Session, currency: str = "USD") -> dict:
    import os
    base_url = os.getenv("FRONTEND_URL", "https://flintx.tv")
    conversion_rate = round(link.total_signups / max(link.total_clicks, 1) * 100, 1)

    # Recent referrals
    recent = db.query(Referral).filter(
        Referral.link_id == link.id
    ).order_by(desc(Referral.created_at)).limit(5).all()

    return {
        "id":              link.id,
        "code":            link.code,
        "url":             f"{base_url}/join/{link.code}",
        "total_clicks":    link.total_clicks,
        "total_signups":   link.total_signups,
        "active_referrals": link.active_referrals,
        "conversion_rate": f"{conversion_rate}%",
        "total_earned":    CurrencyService.to_display(link.total_earned, currency),
        "share_messages": {
            "twitter":  f"I make content on @FlintX — creators earn 80% ad revenue. Join me: {base_url}/join/{link.code}",
            "youtube":  f"Sign up to FlintX and start earning 80% of your ad revenue: {base_url}/join/{link.code}",
            "general":  f"Join FlintX — the platform where creators keep 80%. Use my link: {base_url}/join/{link.code}",
        },
        "recent_referrals": [_referral_dict(r, currency) for r in recent],
    }


# ─────────────────────────────────────────────
# RECORD A CLICK (called from landing page JS)
# ─────────────────────────────────────────────

@router.post("/click/{code}")
def record_click(
    code: str,
    request: Request,
    source: str = "direct",
    db: Session = Depends(get_db),
):
    """
    Called when someone visits flintx.tv/join/{code}.
    Records the click for conversion analytics.
    Returns the referral code to store in a cookie/localStorage
    so signup can tag the new user correctly.
    """
    link = db.query(ReferralLink).filter(ReferralLink.code == code).first()
    if not link:
        raise HTTPException(status_code=404, detail="Referral link not found")

    ip_hash = hashlib.sha256(request.client.host.encode()).hexdigest()[:16]

    click = ReferralClick(
        link_id    = link.id,
        ip_hash    = ip_hash,
        user_agent = request.headers.get("user-agent", ""),
        source     = source,
    )
    db.add(click)
    link.total_clicks += 1
    db.commit()
    db.refresh(click)

    return {
        "click_id":    click.id,
        "referral_code": code,
        "message":     "Store this referral_code in localStorage. Pass it during signup.",
    }


# ─────────────────────────────────────────────
# TAG SIGNUP AS REFERRED (called from auth/signup)
# ─────────────────────────────────────────────

def tag_referral_signup(db: Session, new_user_id: str, referral_code: str, click_id: str = None):
    """
    Called during signup if a referral_code is present.
    Creates a Referral record linking new user to referring creator.
    Called internally — not a direct API endpoint.
    """
    link = db.query(ReferralLink).filter(ReferralLink.code == referral_code).first()
    if not link:
        return

    # Don't let creators refer themselves
    if link.creator_id == new_user_id:
        return

    # Don't double-refer
    existing = db.query(Referral).filter(Referral.referred_user == new_user_id).first()
    if existing:
        return

    window_end = datetime.utcnow() + timedelta(days=REFERRAL_WINDOW_DAYS)
    referral = Referral(
        link_id           = link.id,
        referring_creator = link.creator_id,
        referred_user     = new_user_id,
        window_starts     = datetime.utcnow(),
        window_ends       = window_end,
        bonus_rate        = REFERRAL_BONUS_RATE,
        signup_source     = "referral_link",
        click_id          = click_id,
    )
    db.add(referral)
    link.total_signups   += 1
    link.active_referrals += 1

    # Tag the click as converted
    if click_id:
        click = db.query(ReferralClick).filter(ReferralClick.id == click_id).first()
        if click:
            click.converted = True

    db.commit()


# ─────────────────────────────────────────────
# PROCESS REFERRAL BONUS (called from ad revenue processing)
# ─────────────────────────────────────────────

def process_referral_bonus(db: Session, viewer_id: str, platform_revenue_cents: int):
    """
    Called every time an ad impression is recorded.
    Checks if the viewer was referred — if so, pays bonus to referring creator.
    platform_revenue_cents = FlintX's 20% share from this impression.

    Bonus = 5% of FlintX's share.
    Comes from FlintX's revenue — does NOT reduce creator's 80%.
    """
    if not viewer_id or not platform_revenue_cents:
        return

    referral = db.query(Referral).filter(
        Referral.referred_user == viewer_id,
        Referral.status        == ReferralStatus.active,
        Referral.window_ends   >= datetime.utcnow(),
    ).first()

    if not referral:
        return

    bonus = int(platform_revenue_cents * referral.bonus_rate)
    if bonus <= 0:
        return

    # Credit referring creator's wallet
    creator = db.query(User).filter(User.id == referral.referring_creator).first()
    if not creator:
        return

    creator.wallet_balance      += bonus
    referral.total_bonus_earned += bonus
    referral.impressions_generated += 1

    link = db.query(ReferralLink).filter(ReferralLink.id == referral.link_id).first()
    if link:
        link.total_earned += bonus

    db.add(Transaction(
        user_id      = creator.id,
        type         = TxnType.ad_revenue,
        amount       = bonus,
        balance_after = creator.wallet_balance,
        description  = f"Referral bonus — referred viewer ad impression",
        reference    = referral.id,
    ))

    # Check if window has ended
    if datetime.utcnow() >= referral.window_ends:
        referral.status = ReferralStatus.completed
        if link:
            link.active_referrals = max(0, link.active_referrals - 1)

    db.commit()


# ─────────────────────────────────────────────
# MY REFERRALS
# ─────────────────────────────────────────────

@router.get("/referrals")
def get_my_referrals(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    link = db.query(ReferralLink).filter(ReferralLink.creator_id == user.id).first()
    if not link:
        return {"referrals": [], "total_earned": CurrencyService.to_display(0, currency)}

    referrals = db.query(Referral).filter(
        Referral.link_id == link.id
    ).order_by(desc(Referral.created_at)).all()

    total = sum(r.total_bonus_earned for r in referrals)

    return {
        "currency":     currency,
        "total_earned": CurrencyService.to_display(total, currency),
        "total_count":  len(referrals),
        "active_count": sum(1 for r in referrals if r.status == ReferralStatus.active),
        "referrals":    [_referral_dict(r, currency) for r in referrals],
    }


@router.get("/earnings")
def referral_earnings(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Full earnings breakdown: referral bonuses + badge credits."""
    link   = db.query(ReferralLink).filter(ReferralLink.creator_id == user.id).first()
    badges = db.query(CreatorBadge).filter(
        CreatorBadge.creator_id == user.id,
        CreatorBadge.status == BadgeStatus.active,
    ).all()

    referral_total = link.total_earned if link else 0
    badge_total    = sum(b.total_earned_cents for b in badges)
    combined_total = referral_total + badge_total

    monthly_badge  = sum(b.monthly_credit_cents for b in badges)
    active_refs    = link.active_referrals if link else 0
    projected_monthly_referral = int(active_refs * 10 * 30 * 0.006 * 0.20 * 0.05 * 100)

    return {
        "currency": currency,
        "totals": {
            "referral_bonuses": CurrencyService.to_display(referral_total, currency),
            "badge_credits":    CurrencyService.to_display(badge_total, currency),
            "combined":         CurrencyService.to_display(combined_total, currency),
        },
        "monthly_projection": {
            "badge_credits":    CurrencyService.to_display(monthly_badge, currency),
            "referral_bonuses": CurrencyService.to_display(projected_monthly_referral, currency),
            "total":            CurrencyService.to_display(monthly_badge + projected_monthly_referral, currency),
            "note":             "Referral projection based on 10 daily views per referred user at average CPM.",
        },
        "active_badges":    len(badges),
        "active_referrals": active_refs,
    }


@router.get("/leaderboard")
def referral_leaderboard(db: Session = Depends(get_db)):
    """Top 20 referrers by total signups. Public."""
    top = db.query(ReferralLink).order_by(
        desc(ReferralLink.total_signups)
    ).limit(20).all()

    return {
        "leaderboard": [
            {
                "rank":       i + 1,
                "code":       link.code,
                "signups":    link.total_signups,
                "active":     link.active_referrals,
            }
            for i, link in enumerate(top) if link.total_signups > 0
        ]
    }


# ─────────────────────────────────────────────
# BADGE PROGRAMME
# ─────────────────────────────────────────────

@router.post("/badge", status_code=201)
def enrol_badge(
    req: CreateBadgeRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Enrol in the FlintX badge programme."""
    # Check for duplicate badge on same platform
    existing = db.query(CreatorBadge).filter(
        CreatorBadge.creator_id == user.id,
        CreatorBadge.platform   == ExternalPlatform(req.platform),
        CreatorBadge.status     != BadgeStatus.expired,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"You already have an active badge on {req.platform}")

    credit_amount = _badge_credit_amount(req.reported_reach)
    next_credit   = datetime.utcnow() + timedelta(days=30)

    badge = CreatorBadge(
        creator_id        = user.id,
        badge_type        = BadgeType(req.badge_type),
        platform          = ExternalPlatform(req.platform),
        platform_url      = req.platform_url,
        platform_handle   = req.platform_handle,
        reported_reach    = req.reported_reach,
        monthly_credit_cents = credit_amount,
        next_credit_at    = next_credit,
    )
    db.add(badge)
    db.commit()
    db.refresh(badge)

    return {
        **_badge_dict(badge),
        "message":         f"Badge enrolled. ${credit_amount/100:.2f}/month will be credited to your wallet once verified.",
        "verification":    "Our team reviews badge submissions within 48 hours. You'll receive an email when verified.",
        "how_to_display":  _badge_instructions(req.badge_type, req.platform),
    }


def _badge_instructions(badge_type: str, platform: str) -> dict:
    """Platform-specific instructions for displaying the badge."""
    instructions = {
        "youtube": {
            "video":       "Add 'Made with FlintX (flintx.tv)' to your video description and end cards.",
            "channel":     "Add the FlintX badge to your channel banner and About section.",
            "end_card":    "Use the FlintX end card template (download from badge assets) on every video.",
        },
        "tiktok": {
            "bio":         "Add 'FlintX Creator | flintx.tv' to your TikTok bio.",
            "video":       "Add 'Made with FlintX' text overlay to your videos.",
        },
        "instagram": {
            "bio":         "Add 'FlintX Creator 🔥 flintx.tv' to your Instagram bio.",
            "stories":     "Use the FlintX sticker on your Stories.",
        },
        "twitter": {
            "bio":         "Add 'FlintX Creator | flintx.tv' to your Twitter/X bio.",
            "pinned":      "Pin a tweet about your FlintX channel.",
        },
        "website": {
            "footer":      "Add the FlintX badge to your website footer.",
            "about":       "Mention FlintX in your About page.",
        },
    }
    return instructions.get(platform, {"general": "Display the FlintX badge prominently where your audience can see it."})


@router.get("/badge")
def get_my_badges(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    badges = db.query(CreatorBadge).filter(
        CreatorBadge.creator_id == user.id
    ).order_by(desc(CreatorBadge.created_at)).all()

    total_monthly = sum(b.monthly_credit_cents for b in badges if b.status == BadgeStatus.active)
    total_earned  = sum(b.total_earned_cents for b in badges)

    return {
        "currency":      currency,
        "badges":        [_badge_dict(b, currency) for b in badges],
        "total_monthly": CurrencyService.to_display(total_monthly, currency),
        "total_earned":  CurrencyService.to_display(total_earned, currency),
        "programme_info": {
            "how_it_works":    "Display the FlintX badge on your external content. Get paid monthly for the exposure you give FlintX.",
            "credit_schedule": "Credits paid on the 1st of each month to your FlintX wallet.",
            "tiers": [
                {"reach": "Any",    "monthly": "$5.00"},
                {"reach": "100K+",  "monthly": "$10.00"},
                {"reach": "500K+",  "monthly": "$25.00"},
                {"reach": "1M+",    "monthly": "$50.00"},
            ],
        },
    }


@router.patch("/badge/{badge_id}")
def update_badge(
    badge_id: str,
    req: UpdateBadgeRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    badge = db.query(CreatorBadge).filter(
        CreatorBadge.id == badge_id, CreatorBadge.creator_id == user.id
    ).first()
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    if req.status:
        badge.status = BadgeStatus(req.status)
    if req.platform_url:
        badge.platform_url = req.platform_url
    if req.platform_handle:
        badge.platform_handle = req.platform_handle
    if req.reported_reach is not None:
        badge.reported_reach = req.reported_reach
        badge.monthly_credit_cents = _badge_credit_amount(req.reported_reach)

    db.commit()
    return _badge_dict(badge)


@router.delete("/badge/{badge_id}")
def remove_badge(
    badge_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    badge = db.query(CreatorBadge).filter(
        CreatorBadge.id == badge_id, CreatorBadge.creator_id == user.id
    ).first()
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")
    badge.status = BadgeStatus.expired
    db.commit()
    return {"removed": True}


@router.get("/badge/assets")
def badge_assets():
    """Links to downloadable badge assets (SVG, PNG, dark/light variants)."""
    import os
    base = os.getenv("FRONTEND_URL", "https://flintx.tv")
    return {
        "assets": [
            {"name": "Made with FlintX — Dark",  "format": "SVG", "url": f"{base}/badge/made-with-flintx-dark.svg"},
            {"name": "Made with FlintX — Light",  "format": "SVG", "url": f"{base}/badge/made-with-flintx-light.svg"},
            {"name": "FlintX Creator — Dark",     "format": "PNG", "url": f"{base}/badge/flintx-creator-dark.png"},
            {"name": "FlintX Creator — Light",    "format": "PNG", "url": f"{base}/badge/flintx-creator-light.png"},
            {"name": "End Card Template",          "format": "PSD", "url": f"{base}/badge/end-card-template.psd"},
            {"name": "End Card Template",          "format": "CANVA", "url": "https://canva.com/flintx-end-card"},
        ],
        "usage_guidelines": f"{base}/badge/guidelines",
    }


# ─────────────────────────────────────────────
# STUDIO EXTERNAL USE
# ─────────────────────────────────────────────

@router.post("/external")
def log_external_use(
    req: LogExternalRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Log when Studio-produced content is published externally."""
    plan  = _get_creator_plan(user, db)
    month = datetime.utcnow().strftime("%Y-%m")

    # Check quota
    quota = EXTERNAL_QUOTAS.get(plan, EXTERNAL_QUOTAS["none"])
    tool_quota = quota.get(req.tool, 0)

    if tool_quota == 0:
        raise HTTPException(
            status_code=403,
            detail=f"Your current plan doesn't include external publishing of {req.tool}. Upgrade to Basic or higher."
        )

    if tool_quota > 0:  # -1 = unlimited
        used = db.query(func.count(StudioExternalUse.id)).filter(
            StudioExternalUse.creator_id == user.id,
            StudioExternalUse.tool       == req.tool,
            StudioExternalUse.month      == month,
        ).scalar() or 0

        if used >= tool_quota:
            raise HTTPException(
                status_code=429,
                detail=f"Monthly external {req.tool} limit reached ({tool_quota}/month on {plan} plan). Upgrade to Pro for 50/month or Agency for unlimited."
            )

    use = StudioExternalUse(
        creator_id      = user.id,
        tool            = req.tool,
        platform        = ExternalPlatform(req.platform),
        platform_url    = req.platform_url,
        content_title   = req.content_title,
        external_views  = req.external_views,
        external_revenue = req.external_revenue,
        month           = month,
    )
    db.add(use)
    db.commit()

    return {
        "logged": True,
        "tool":   req.tool,
        "platform": req.platform,
        "quota_used": used + 1 if tool_quota > 0 else "unlimited",
        "quota_total": tool_quota if tool_quota > 0 else "unlimited",
    }


@router.get("/external/quota")
def get_external_quota(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Remaining external use quota this month."""
    plan  = _get_creator_plan(user, db)
    month = datetime.utcnow().strftime("%Y-%m")
    quota = EXTERNAL_QUOTAS.get(plan, EXTERNAL_QUOTAS["none"])

    result = {}
    for tool, limit in quota.items():
        if limit == -1:
            result[tool] = {"used": 0, "limit": "unlimited", "remaining": "unlimited"}
        else:
            used = db.query(func.count(StudioExternalUse.id)).filter(
                StudioExternalUse.creator_id == user.id,
                StudioExternalUse.tool       == tool,
                StudioExternalUse.month      == month,
            ).scalar() or 0
            result[tool] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}

    return {
        "plan":  plan,
        "month": month,
        "quota": result,
        "upgrade_message": "Upgrade to Pro for 50 external scripts/month, or Agency for unlimited." if plan in ("none","basic") else None,
    }


@router.get("/external")
def get_external_history(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    uses = db.query(StudioExternalUse).filter(
        StudioExternalUse.creator_id == user.id
    ).order_by(desc(StudioExternalUse.created_at)).limit(50).all()

    return {
        "history": [
            {
                "id":        u.id,
                "tool":      u.tool,
                "platform":  u.platform.value,
                "title":     u.content_title,
                "url":       u.platform_url,
                "views":     u.external_views,
                "month":     u.month,
                "created_at": u.created_at.isoformat(),
            }
            for u in uses
        ]
    }


# ─────────────────────────────────────────────
# ADMIN — BADGE VERIFICATION
# ─────────────────────────────────────────────

@router.get("/admin/badges")
def admin_pending_badges(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    pending = db.query(CreatorBadge).filter(
        CreatorBadge.verified == False,
        CreatorBadge.status   == BadgeStatus.active,
    ).order_by(CreatorBadge.created_at).all()

    return {
        "count": len(pending),
        "badges": [
            {
                "id":           b.id,
                "creator_id":   b.creator_id,
                "badge_type":   b.badge_type.value,
                "platform":     b.platform.value,
                "platform_url": b.platform_url,
                "handle":       b.platform_handle,
                "reach":        b.reported_reach,
                "credit":       f"${b.monthly_credit_cents/100:.2f}/mo",
                "created_at":   b.created_at.isoformat(),
            }
            for b in pending
        ]
    }


@router.post("/admin/badge/{badge_id}/verify")
def admin_verify_badge(
    badge_id: str,
    approved: bool = True,
    note: str = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    badge = db.query(CreatorBadge).filter(CreatorBadge.id == badge_id).first()
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    if approved:
        badge.verified          = True
        badge.verified_at       = datetime.utcnow()
        badge.last_checked_at   = datetime.utcnow()
        badge.verification_note = note or "Verified by FlintX team"
        # Issue first credit immediately
        _issue_badge_credit(db, badge)
    else:
        badge.status            = BadgeStatus.expired
        badge.verification_note = note or "Badge not verified — content not found"

    db.commit()
    return {"verified": approved, "badge_id": badge_id}


def _issue_badge_credit(db: Session, badge: CreatorBadge):
    """Issue monthly wallet credit for an active badge."""
    creator = db.query(User).filter(User.id == badge.creator_id).first()
    if not creator:
        return

    amount = badge.monthly_credit_cents
    creator.wallet_balance    += amount
    badge.total_earned_cents  += amount
    badge.last_credit_at       = datetime.utcnow()
    badge.next_credit_at       = datetime.utcnow() + timedelta(days=30)

    period = datetime.utcnow().strftime("%Y-%m")
    db.add(BadgeCredit(badge_id=badge.id, creator_id=creator.id, amount=amount, period=period))
    db.add(Transaction(
        user_id      = creator.id,
        type         = TxnType.ad_revenue,
        amount       = amount,
        balance_after = creator.wallet_balance,
        description  = f"FlintX Badge credit — {badge.platform.value} ({badge.badge_type.value})",
        reference    = badge.id,
    ))


@router.get("/admin/stats")
def admin_referral_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    total_links    = db.query(func.count(ReferralLink.id)).scalar() or 0
    total_referrals = db.query(func.count(Referral.id)).scalar() or 0
    active_refs    = db.query(func.count(Referral.id)).filter(Referral.status == ReferralStatus.active).scalar() or 0
    total_bonus    = db.query(func.coalesce(func.sum(ReferralLink.total_earned), 0)).scalar() or 0
    active_badges  = db.query(func.count(CreatorBadge.id)).filter(CreatorBadge.status == BadgeStatus.active).scalar() or 0
    verified_badges = db.query(func.count(CreatorBadge.id)).filter(CreatorBadge.verified == True).scalar() or 0

    return {
        "referral": {
            "total_links":     total_links,
            "total_referrals": total_referrals,
            "active_referrals": active_refs,
            "total_bonus_paid": CurrencyService.format(total_bonus, "USD"),
        },
        "badges": {
            "total_active":    active_badges,
            "verified":        verified_badges,
            "pending_verify":  active_badges - verified_badges,
            "monthly_credit_liability": CurrencyService.format(verified_badges * BADGE_MONTHLY_CREDIT, "USD"),
        },
    }


# ─────────────────────────────────────────────
# BACKGROUND JOB — monthly badge credits
# ─────────────────────────────────────────────

def run_monthly_badge_credits():
    """
    Run on the 1st of each month.
    Issues wallet credits to all creators with active verified badges.
    Add to APScheduler: scheduler.add_job(run_monthly_badge_credits, 'cron', day=1, hour=0)
    """
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        due_badges = db.query(CreatorBadge).filter(
            CreatorBadge.status   == BadgeStatus.active,
            CreatorBadge.verified == True,
            CreatorBadge.next_credit_at <= datetime.utcnow(),
        ).all()

        paid = 0
        for badge in due_badges:
            _issue_badge_credit(db, badge)
            paid += 1

        db.commit()
        print(f"[BADGE CREDITS] Issued {paid} monthly credits")
    except Exception as e:
        print(f"[BADGE CREDITS ERROR] {e}")
    finally:
        db.close()
