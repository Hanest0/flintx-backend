"""
Flint — Complete Database Models
Every table the platform needs, in one file.
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Float,
    ForeignKey, Enum as SAEnum, Integer, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, DeclarativeBase
import enum


class Base(DeclarativeBase):
    pass


def new_uuid():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class UserRole(str, enum.Enum):
    viewer     = "viewer"
    creator    = "creator"
    both       = "both"
    advertiser = "advertiser"
    admin      = "admin"

class AccountStatus(str, enum.Enum):
    pending   = "pending"
    active    = "active"
    suspended = "suspended"
    banned    = "banned"

class PassStatus(str, enum.Enum):
    none    = "none"
    monthly = "monthly"
    annual  = "annual"

class StudioPlan(str, enum.Enum):
    none   = "none"
    basic  = "basic"    # £29/mo
    pro    = "pro"      # £59/mo
    agency = "agency"   # £149/mo

class VideoStatus(str, enum.Enum):
    uploading    = "uploading"
    processing   = "processing"
    mod_pending  = "mod_pending"
    mod_approved = "mod_approved"
    mod_rejected = "mod_rejected"
    published    = "published"
    unpublished  = "unpublished"
    deleted      = "deleted"

class VideoType(str, enum.Enum):
    long  = "long"
    short = "short"    # FlintX Clips

class SafetyLevel(str, enum.Enum):
    safe_for_all = "safe_for_all"
    standard     = "standard"
    mature_ok    = "mature_ok"
    limited_ads  = "limited_ads"
    no_ads       = "no_ads"

class ModerationStatus(str, enum.Enum):
    pending       = "pending"
    auto_approved = "auto_approved"
    flagged       = "flagged"
    approved      = "approved"
    rejected      = "rejected"
    appealed      = "appealed"
    appeal_approved = "appeal_approved"
    appeal_rejected = "appeal_rejected"

class TxnType(str, enum.Enum):
    ad_revenue      = "ad_revenue"
    course_sale     = "course_sale"
    tip             = "tip"
    studio_sub      = "studio_sub"
    pass_sub        = "pass_sub"
    viewer_credit   = "viewer_credit"
    payout_wise     = "payout_wise"
    payout_paypal   = "payout_paypal"
    advertiser_topup = "advertiser_topup"
    ad_spend        = "ad_spend"
    refund          = "refund"

class AdFormat(str, enum.Enum):
    preroll   = "preroll"
    midroll   = "midroll"
    display   = "display"
    sponsored = "sponsored"

class CampaignStatus(str, enum.Enum):
    pending = "pending"
    active  = "active"
    paused  = "paused"
    ended   = "ended"


# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    full_name     = Column(String(200))
    country       = Column(String(100))
    role          = Column(SAEnum(UserRole), default=UserRole.viewer, nullable=False)
    status        = Column(SAEnum(AccountStatus), default=AccountStatus.pending)

    # Email verification
    email_verified        = Column(Boolean, default=False)
    email_verify_token    = Column(Text)
    email_verify_expires  = Column(DateTime)

    # Password reset
    password_reset_token   = Column(Text)
    password_reset_expires = Column(DateTime)

    # Pass subscription
    pass_status    = Column(SAEnum(PassStatus), default=PassStatus.none)
    pass_expires   = Column(DateTime)
    pass_paypal_id = Column(Text)   # PayPal subscription ID

    # Wallet (stored in pence — integer)
    wallet_balance = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime)

    # Relationships
    creator_profile    = relationship("CreatorProfile", back_populates="user", uselist=False)
    viewer_profile     = relationship("ViewerProfile",  back_populates="user", uselist=False)
    advertiser_profile = relationship("AdvertiserProfile", back_populates="user", uselist=False)
    refresh_tokens     = relationship("RefreshToken", back_populates="user")
    transactions       = relationship("Transaction", back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    token_hash = Column(Text, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    revoked    = Column(Boolean, default=False)
    user_agent = Column(Text)
    ip_address = Column(String(45))
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="refresh_tokens")


# ─────────────────────────────────────────────
# CREATOR PROFILE
# ─────────────────────────────────────────────

class CreatorProfile(Base):
    __tablename__ = "creator_profiles"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id        = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True)
    channel_name   = Column(String(100))
    channel_handle = Column(String(50), unique=True, index=True)
    category       = Column(String(50))
    bio            = Column(Text)
    avatar_url     = Column(Text)
    banner_url     = Column(Text)

    # Studio plan
    studio_plan        = Column(SAEnum(StudioPlan), default=StudioPlan.none)
    studio_plan_expires = Column(DateTime)
    studio_paypal_id   = Column(Text)

    # Tools unlocked (comma-separated)
    tools_active = Column(Text, default="script,voice,predictor")

    # Revenue
    monthly_ad_revenue = Column(Integer, default=0)   # pence
    total_earnings     = Column(Integer, default=0)   # pence
    pending_payout     = Column(Integer, default=0)   # pence

    # Wise payout details
    wise_account_id  = Column(Text)
    wise_recipient_id = Column(Text)

    # Stats
    subscriber_count = Column(Integer, default=0)
    video_count      = Column(Integer, default=0)
    total_views      = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    user   = relationship("User", back_populates="creator_profile")
    videos = relationship("Video", back_populates="creator")


# ─────────────────────────────────────────────
# VIEWER PROFILE
# ─────────────────────────────────────────────

class ViewerProfile(Base):
    __tablename__ = "viewer_profiles"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id          = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True)
    interests        = Column(Text)           # comma-separated categories
    credits_earned   = Column(Integer, default=0)   # pence
    credits_paid_out = Column(Integer, default=0)   # pence

    # PayPal for cashout
    paypal_email = Column(String(255))

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="viewer_profile")


# ─────────────────────────────────────────────
# ADVERTISER PROFILE
# ─────────────────────────────────────────────

class AdvertiserProfile(Base):
    __tablename__ = "advertiser_profiles"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True)
    company_name = Column(String(200))
    website      = Column(String(500))
    industry     = Column(String(100))
    description  = Column(Text)
    approved     = Column(Boolean, default=False)
    approved_at  = Column(DateTime)

    # Budget
    budget_balance = Column(Integer, default=0)   # pence — pre-funded
    total_spent    = Column(Integer, default=0)   # pence

    # Contact
    contact_email = Column(String(255))

    created_at = Column(DateTime, default=datetime.utcnow)

    user      = relationship("User", back_populates="advertiser_profile")
    campaigns = relationship("AdCampaign", back_populates="advertiser")


# ─────────────────────────────────────────────
# VIDEOS
# ─────────────────────────────────────────────

class Video(Base):
    __tablename__ = "videos"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("creator_profiles.id"), nullable=False)
    title       = Column(String(500), nullable=False)
    description = Column(Text)
    category    = Column(String(50))
    tags        = Column(Text)              # JSON array stored as text
    video_type  = Column(SAEnum(VideoType), default=VideoType.long)

    # Storage
    s3_key_raw      = Column(Text)           # original upload
    s3_key_hls      = Column(Text)           # transcoded HLS path
    thumbnail_url   = Column(Text)
    duration_s      = Column(Integer)        # seconds
    file_size_mb    = Column(Float)

    # URLs (served via CloudFront)
    hls_url         = Column(Text)           # playback URL
    hls_1080_url    = Column(Text)
    hls_720_url     = Column(Text)
    hls_360_url     = Column(Text)

    # Status
    status          = Column(SAEnum(VideoStatus), default=VideoStatus.uploading)
    mediaconvert_job = Column(Text)          # AWS job ID

    # Moderation
    mod_status      = Column(SAEnum(ModerationStatus), default=ModerationStatus.pending)
    safety_level    = Column(SAEnum(SafetyLevel), default=SafetyLevel.standard)
    mod_flags       = Column(Text)           # JSON — flags from automated checks
    mod_reviewed_by = Column(Text)           # admin user ID
    mod_reviewed_at = Column(DateTime)
    mod_notes       = Column(Text)

    # Appeal
    appeal_statement = Column(Text)
    appeal_at        = Column(DateTime)

    # Stats
    view_count       = Column(Integer, default=0)
    like_count       = Column(Integer, default=0)
    comment_count    = Column(Integer, default=0)
    share_count      = Column(Integer, default=0)
    completion_rate  = Column(Float, default=0.0)   # 0.0–1.0

    # Revenue
    ad_revenue_total  = Column(Integer, default=0)  # pence total gross
    creator_earnings  = Column(Integer, default=0)  # pence — 80%

    # Algorithm score (cached, recalculated hourly)
    algo_score = Column(Float, default=0.0)

    created_at   = Column(DateTime, default=datetime.utcnow)
    published_at = Column(DateTime)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator  = relationship("CreatorProfile", back_populates="videos")
    views    = relationship("VideoView", back_populates="video")
    ad_impressions = relationship("AdImpression", back_populates="video")

    __table_args__ = (
        Index("ix_videos_creator_status", "creator_id", "status"),
        Index("ix_videos_category_status", "category", "status"),
        Index("ix_videos_algo_score", "algo_score"),
    )


class VideoView(Base):
    """Every view event — used for algorithm scoring and ad billing."""
    __tablename__ = "video_views"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    video_id       = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=False)
    user_id        = Column(UUID(as_uuid=False), ForeignKey("users.id"))   # null = anonymous
    ip_hash        = Column(String(64))      # hashed for uniqueness without storing IP
    watched_pct    = Column(Float, default=0.0)   # 0.0–1.0
    is_pass_viewer = Column(Boolean, default=False)  # Pass subscriber = opted-in ad viewer
    ad_completed   = Column(Boolean, default=False)
    created_at     = Column(DateTime, default=datetime.utcnow)

    video = relationship("Video", back_populates="views")

    __table_args__ = (
        Index("ix_views_video_id", "video_id"),
        Index("ix_views_created_at", "created_at"),
    )


# ─────────────────────────────────────────────
# COURSES
# ─────────────────────────────────────────────

class Course(Base):
    __tablename__ = "courses"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("creator_profiles.id"))
    title       = Column(String(500), nullable=False)
    description = Column(Text)
    price_pence = Column(Integer, nullable=False)
    thumbnail_url = Column(Text)
    lesson_count  = Column(Integer, default=0)
    status        = Column(String(20), default="draft")   # draft | published
    sales_count   = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)


class CourseSale(Base):
    __tablename__ = "course_sales"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    course_id     = Column(UUID(as_uuid=False), ForeignKey("courses.id"))
    buyer_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    amount_pence  = Column(Integer)
    creator_cut   = Column(Integer)   # 80%
    platform_cut  = Column(Integer)   # 20%
    paypal_txn_id = Column(Text)
    created_at    = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# WALLET & TRANSACTIONS
# ─────────────────────────────────────────────

class Transaction(Base):
    """
    Single ledger for all money movement on the platform.
    Positive = money IN to user. Negative = money OUT.
    All amounts in pence (integer).
    """
    __tablename__ = "transactions"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    type        = Column(SAEnum(TxnType), nullable=False)
    amount      = Column(Integer, nullable=False)   # pence, signed
    balance_after = Column(Integer)                 # wallet balance after this txn
    description = Column(Text)
    reference   = Column(Text)    # PayPal txn ID, Wise transfer ID, video ID, etc.
    video_id    = Column(UUID(as_uuid=False), ForeignKey("videos.id"))
    created_at  = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")

    __table_args__ = (
        Index("ix_txns_user_id", "user_id"),
        Index("ix_txns_type", "type"),
        Index("ix_txns_created_at", "created_at"),
    )


class PayoutRequest(Base):
    """Creator requests to cash out their wallet balance."""
    __tablename__ = "payout_requests"

    id            = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id       = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    amount        = Column(Integer)       # pence
    method        = Column(String(20))    # wise | paypal
    status        = Column(String(20), default="pending")   # pending|processing|paid|failed
    wise_transfer_id = Column(Text)
    paypal_txn_id    = Column(Text)
    requested_at  = Column(DateTime, default=datetime.utcnow)
    paid_at       = Column(DateTime)
    notes         = Column(Text)


# ─────────────────────────────────────────────
# ADVERTISING
# ─────────────────────────────────────────────

class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    advertiser_id   = Column(UUID(as_uuid=False), ForeignKey("advertiser_profiles.id"))
    name            = Column(String(500))
    format          = Column(SAEnum(AdFormat), default=AdFormat.preroll)
    target_niches   = Column(Text)   # JSON array — ["finance","technology"]
    goal            = Column(String(100))
    status          = Column(SAEnum(CampaignStatus), default=CampaignStatus.pending)
    safety_required = Column(SAEnum(SafetyLevel), default=SafetyLevel.safe_for_all)

    # Budget
    budget_pence    = Column(Integer, default=0)   # total budget
    spent_pence     = Column(Integer, default=0)   # amount consumed

    # Creative
    creative_url    = Column(Text)   # S3 URL to ad video/image
    click_url       = Column(Text)   # where clicking the ad goes
    cpm_pence       = Column(Integer, default=480)   # bid CPM in pence

    # Stats
    impressions     = Column(Integer, default=0)
    clicks          = Column(Integer, default=0)
    completions     = Column(Integer, default=0)

    created_at      = Column(DateTime, default=datetime.utcnow)
    starts_at       = Column(DateTime)
    ends_at         = Column(DateTime)

    advertiser   = relationship("AdvertiserProfile", back_populates="campaigns")
    impressions_ = relationship("AdImpression", back_populates="campaign")


class AdImpression(Base):
    """
    One row per ad view. This is the billing record.
    When is_pass_viewer=True, the viewer earns credits.
    """
    __tablename__ = "ad_impressions"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    campaign_id    = Column(UUID(as_uuid=False), ForeignKey("ad_campaigns.id"))
    video_id       = Column(UUID(as_uuid=False), ForeignKey("videos.id"))
    viewer_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    is_pass_viewer = Column(Boolean, default=False)
    completed      = Column(Boolean, default=False)
    clicked        = Column(Boolean, default=False)

    # Billing
    cpm_charged    = Column(Integer)   # pence — actual CPM this impression used
    creator_cut    = Column(Integer)   # pence — 80%
    platform_cut   = Column(Integer)   # pence — 20%
    viewer_credit  = Column(Integer, default=0)  # pence — credit issued if Pass viewer

    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("AdCampaign", back_populates="impressions_")
    video    = relationship("Video", back_populates="ad_impressions")


# ─────────────────────────────────────────────
# ALGORITHM SCORES (cached)
# ─────────────────────────────────────────────

class AlgoScore(Base):
    """
    Cached algorithm score per video.
    Recalculated by background job every hour.
    """
    __tablename__ = "algo_scores"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    video_id        = Column(UUID(as_uuid=False), ForeignKey("videos.id"), unique=True)
    total_score     = Column(Float, default=0.0)
    completion_score = Column(Float, default=0.0)
    like_score      = Column(Float, default=0.0)
    recency_score   = Column(Float, default=0.0)
    creator_score   = Column(Float, default=0.0)
    share_score     = Column(Float, default=0.0)
    new_creator_boost = Column(Boolean, default=False)
    calculated_at   = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# TIPS
# ─────────────────────────────────────────────

class Tip(Base):
    __tablename__ = "tips"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    from_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    to_creator_id = Column(UUID(as_uuid=False), ForeignKey("creator_profiles.id"))
    video_id     = Column(UUID(as_uuid=False), ForeignKey("videos.id"))
    amount_pence = Column(Integer)
    message      = Column(Text)
    paypal_txn   = Column(Text)
    created_at   = Column(DateTime, default=datetime.utcnow)
