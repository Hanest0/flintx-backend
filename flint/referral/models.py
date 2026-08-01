"""
FlintX — Referral & Badge Models

Two systems that capture value from cross-platform activity:

1. REFERRAL FLYWHEEL
   Creator shares a unique link (flintx.tv/join/HANDLE).
   When someone signs up via that link, they're tagged as referred.
   For 12 months, the referring creator earns a bonus on every
   ad impression that referred user generates by watching on FlintX.
   Bonus comes from FlintX's 20% platform share — creator 80% is untouched.

2. FLINTX BADGE PROGRAMME
   Creator opts in. Their content carries "Made with FlintX" branding.
   In exchange: $5/month wallet credit per active badge.
   FlintX gets brand exposure on external platforms.
   Creator gets paid for marketing FlintX.
   No punitive mechanics. Pure opt-in incentive.

3. STUDIO EXTERNAL USE TRACKING
   Tracks when Studio tools are used for content published externally.
   Powers the tiered pricing model (internal unlimited, external capped by plan).
   No blocking — just correct pricing of the value FlintX provides.
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Float,
    ForeignKey, Integer, Enum as SAEnum, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from ..database.models import Base


def new_uuid():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class ReferralStatus(str, enum.Enum):
    active    = "active"     # referred user is active, bonuses flowing
    completed = "completed"  # 12-month window ended
    paused    = "paused"     # referred user inactive

class BadgeType(str, enum.Enum):
    made_with  = "made_with"   # "Made with FlintX" on content
    powered_by = "powered_by"  # "Powered by FlintX" on website/profile
    creator    = "creator"     # "FlintX Creator" badge on social profiles

class BadgeStatus(str, enum.Enum):
    active    = "active"
    paused    = "paused"    # creator paused — no credit but no penalty
    expired   = "expired"

class ExternalPlatform(str, enum.Enum):
    youtube   = "youtube"
    tiktok    = "tiktok"
    instagram = "instagram"
    twitter   = "twitter"
    twitch    = "twitch"
    website   = "website"
    other     = "other"


# ─────────────────────────────────────────────
# REFERRAL LINK
# ─────────────────────────────────────────────

class ReferralLink(Base):
    """
    One unique referral link per creator.
    URL: flintx.tv/join/{code}

    The code is their channel handle by default (memorable),
    with a random fallback if handle is taken.

    Analytics: clicks, signups, conversion rate, earnings generated.
    """
    __tablename__ = "referral_links"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True, nullable=False)
    code         = Column(String(50), unique=True, nullable=False, index=True)

    # Stats (updated in real time)
    total_clicks    = Column(Integer, default=0)
    total_signups   = Column(Integer, default=0)
    active_referrals = Column(Integer, default=0)   # currently in 12-month window
    total_earned    = Column(Integer, default=0)     # USD cents lifetime bonus

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    referrals = relationship("Referral", back_populates="link")

    __table_args__ = (
        Index("ix_referral_links_creator", "creator_id"),
    )


class ReferralClick(Base):
    """
    Every click on a referral link — for conversion analytics.
    Stored separately to avoid bloating the referral link record.
    """
    __tablename__ = "referral_clicks"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    link_id     = Column(UUID(as_uuid=False), ForeignKey("referral_links.id"), nullable=False)
    ip_hash     = Column(String(64))
    user_agent  = Column(Text)
    source      = Column(String(100))   # youtube, twitter, tiktok, direct, etc.
    converted   = Column(Boolean, default=False)   # did they sign up?
    created_at  = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ref_clicks_link", "link_id"),
        Index("ix_ref_clicks_date", "created_at"),
    )


class Referral(Base):
    """
    One row per referred user.
    Created when someone signs up via a referral link.
    Tracks the 12-month bonus window.

    Bonus rate: 5% of FlintX's platform share (20%) on every ad impression
    the referred user generates.

    Example:
      - Referred viewer watches a video
      - Ad impression: $0.006 total (per impression at $6 CPM)
      - Creator gets 80%: $0.0048
      - FlintX gets 20%: $0.0012
      - Referral bonus (5% of FlintX share): $0.00006 → to referring creator

    Tiny per impression, significant at scale.
    1,000 referred active viewers × 10 daily views × 365 days
    = 3,650,000 impressions × $0.00006 = $219/year passive bonus.
    """
    __tablename__ = "referrals"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    link_id          = Column(UUID(as_uuid=False), ForeignKey("referral_links.id"), nullable=False)
    referring_creator = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    referred_user    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, unique=True)

    status           = Column(SAEnum(ReferralStatus), default=ReferralStatus.active)

    # 12-month bonus window
    window_starts    = Column(DateTime, default=datetime.utcnow)
    window_ends      = Column(DateTime)   # set to +12 months on creation

    # Bonus tracking
    bonus_rate       = Column(Float, default=0.05)    # 5% of FlintX's share
    total_bonus_earned = Column(Integer, default=0)   # USD cents paid to referring creator
    impressions_generated = Column(Integer, default=0)   # total ad impressions by referred user

    # Source tracking
    signup_source    = Column(String(100))   # which platform the click came from
    click_id         = Column(UUID(as_uuid=False), ForeignKey("referral_clicks.id"))

    created_at       = Column(DateTime, default=datetime.utcnow)

    link = relationship("ReferralLink", back_populates="referrals")

    __table_args__ = (
        Index("ix_referrals_creator", "referring_creator"),
        Index("ix_referrals_user", "referred_user"),
        Index("ix_referrals_status", "status"),
    )


class ReferralBonus(Base):
    """
    Individual bonus payments to referring creators.
    One row per ad impression that generates a referral bonus.
    Batched in practice — written every 100 impressions to reduce DB load.
    """
    __tablename__ = "referral_bonuses"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    referral_id    = Column(UUID(as_uuid=False), ForeignKey("referrals.id"), nullable=False)
    creator_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    amount_cents   = Column(Integer, nullable=False)    # USD cents
    impressions    = Column(Integer, default=1)         # number of impressions in this batch
    period_start   = Column(DateTime)
    period_end     = Column(DateTime)
    paid           = Column(Boolean, default=False)     # included in a payout
    created_at     = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ref_bonus_creator", "creator_id"),
        Index("ix_ref_bonus_paid", "paid"),
    )


# ─────────────────────────────────────────────
# FLINTX BADGE PROGRAMME
# ─────────────────────────────────────────────

class CreatorBadge(Base):
    """
    Creator opts into the FlintX badge programme.
    They display "Made with FlintX" on their content or profile.
    FlintX pays $5/month credit per active badge to their wallet.

    Verification is honour-based with periodic spot checks.
    In production: creator submits a link to content with badge visible.
    """
    __tablename__ = "creator_badges"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    badge_type     = Column(SAEnum(BadgeType), default=BadgeType.made_with)
    status         = Column(SAEnum(BadgeStatus), default=BadgeStatus.active)

    # Where the badge is displayed
    platform       = Column(SAEnum(ExternalPlatform), default=ExternalPlatform.youtube)
    platform_url   = Column(Text)    # link to the content/profile showing the badge
    platform_handle = Column(String(200))   # their handle on that platform

    # Verification
    verified         = Column(Boolean, default=False)
    verified_at      = Column(DateTime)
    last_checked_at  = Column(DateTime)
    verification_note = Column(Text)

    # Monthly credit
    monthly_credit_cents = Column(Integer, default=500)   # $5.00
    total_earned_cents   = Column(Integer, default=0)
    last_credit_at       = Column(DateTime)
    next_credit_at       = Column(DateTime)

    # Estimated reach (reported by creator, used for tier upgrades)
    reported_reach   = Column(Integer, default=0)   # monthly views/impressions

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_badges_creator", "creator_id"),
        Index("ix_badges_status", "status"),
        Index("ix_badges_next_credit", "next_credit_at"),
    )


class BadgeCredit(Base):
    """
    Monthly credit payment record for each active badge.
    Written by a background job that runs on the 1st of each month.
    """
    __tablename__ = "badge_credits"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    badge_id    = Column(UUID(as_uuid=False), ForeignKey("creator_badges.id"), nullable=False)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    amount      = Column(Integer, nullable=False)   # USD cents
    period      = Column(String(7), nullable=False)   # YYYY-MM
    paid        = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_badge_credits_creator", "creator_id"),
        Index("ix_badge_credits_period", "period"),
    )


# ─────────────────────────────────────────────
# STUDIO EXTERNAL USE TRACKING
# ─────────────────────────────────────────────

class StudioExternalUse(Base):
    """
    Tracks when a creator uses Studio tools and publishes externally.
    Creator self-reports (on publish) which platform the content went to.

    Used to enforce plan limits on external use and to measure
    the value FlintX provides to content published off-platform.

    Plan limits (external use per month):
      None:   0 external scripts
      Basic:  10 external scripts, 5 external voices
      Pro:    50 external scripts, 25 external voices (unlimited FlintX)
      Agency: Unlimited everywhere
    """
    __tablename__ = "studio_external_uses"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    tool        = Column(String(50))      # script | voice | opportunity
    platform    = Column(SAEnum(ExternalPlatform))
    platform_url = Column(Text)           # link to the published content
    content_title = Column(String(500))

    # Revenue data (creator self-reports or imports)
    external_views   = Column(Integer, default=0)
    external_revenue = Column(Integer, default=0)   # USD cents, if known

    month       = Column(String(7))   # YYYY-MM, for quota tracking
    created_at  = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ext_use_creator_month", "creator_id", "month"),
        Index("ix_ext_use_platform", "platform"),
    )
