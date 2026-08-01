"""
FlintX — Live Streaming Models

Architecture:
  Creator broadcasts via RTMP → FlintX ingest server (nginx-rtmp or AWS IVS)
  Server transcodes to HLS in real time → CloudFront delivers to viewers
  WebSocket server handles live chat and viewer count
  Ad impressions fire every 8 minutes for opted-in Pass viewers

FlintX live streaming differentiators vs Kick/Twitch:
  - 80% of ad revenue to streamer (Kick = 95% subs only, no ad share)
  - Pass viewers earn credits watching live ads (unique to FlintX)
  - Collab streams: split revenue with co-streamers automatically
  - Multi-channel streaming: stream appears on multiple FlintX channels
  - VOD auto-saved and published after stream ends
  - Creator Quality Score gates ad eligibility (not subscriber count)
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


class StreamStatus(str, enum.Enum):
    idle      = "idle"       # stream key exists, not live
    live      = "live"       # currently broadcasting
    ending    = "ending"     # stream ended, VOD processing
    vod_ready = "vod_ready"  # VOD published and watchable
    suspended = "suspended"  # violates guidelines


class StreamCategory(str, enum.Enum):
    # Finance & Business
    personal_finance   = "personal_finance"
    investing          = "investing"
    cryptocurrency     = "cryptocurrency"
    entrepreneurship   = "entrepreneurship"
    real_estate        = "real_estate"
    tax_accounting     = "tax_accounting"
    # Technology
    ai_ml              = "ai_ml"
    software_dev       = "software_dev"
    cybersecurity      = "cybersecurity"
    gaming             = "gaming"
    hardware           = "hardware"
    web_dev            = "web_dev"
    data_science       = "data_science"
    # Education
    online_learning    = "online_learning"
    language_learning  = "language_learning"
    science            = "science"
    history            = "history"
    mathematics        = "mathematics"
    psychology         = "psychology"
    law_legal          = "law_legal"
    # Health & Lifestyle
    fitness            = "fitness"
    nutrition          = "nutrition"
    mental_health      = "mental_health"
    yoga_meditation    = "yoga_meditation"
    medicine           = "medicine"
    parenting          = "parenting"
    # Creative Arts
    music              = "music"
    art_drawing        = "art_drawing"
    photography        = "photography"
    video_production   = "video_production"
    graphic_design     = "graphic_design"
    animation          = "animation"
    writing            = "writing"
    podcasting         = "podcasting"
    # Food & Drink
    cooking            = "cooking"
    baking             = "baking"
    restaurants        = "restaurants"
    wine_spirits       = "wine_spirits"
    coffee             = "coffee"
    # Travel & Outdoors
    travel             = "travel"
    adventure_hiking   = "adventure_hiking"
    luxury_travel      = "luxury_travel"
    camping_survival   = "camping_survival"
    # Entertainment
    comedy             = "comedy"
    movie_reviews      = "movie_reviews"
    anime              = "anime"
    sports             = "sports"
    true_crime         = "true_crime"
    news_politics      = "news_politics"
    book_reviews       = "book_reviews"
    # Fashion & Beauty
    fashion            = "fashion"
    beauty_makeup      = "beauty_makeup"
    skincare           = "skincare"
    luxury_lifestyle   = "luxury_lifestyle"
    # Home & Family
    home_improvement   = "home_improvement"
    interior_design    = "interior_design"
    diy_crafts         = "diy_crafts"
    pets_animals       = "pets_animals"
    sustainability     = "sustainability"
    # Cars & Transport
    cars_automotive    = "cars_automotive"
    electric_vehicles  = "electric_vehicles"
    motorcycles        = "motorcycles"
    # General
    irl                = "irl"
    talk               = "talk"
    other              = "other"


class ChatMsgType(str, enum.Enum):
    message   = "message"
    tip       = "tip"
    sub       = "sub"
    system    = "system"
    pin       = "pin"


class QualityTier(str, enum.Enum):
    provisional  = "provisional"   # 0 videos — no ads
    active       = "active"        # 1+ approved videos — standard ads
    established  = "established"   # 5+ videos, >40% completion — all ads
    premium      = "premium"       # 20+ videos, >60% completion — exclusivity deals


# ─────────────────────────────────────────────
# STREAM CHANNEL (one per creator)
# ─────────────────────────────────────────────

class StreamChannel(Base):
    """
    Each creator has one stream channel.
    Contains their RTMP stream key and current live status.
    Linked to their creator profile.
    """
    __tablename__ = "stream_channels"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True, nullable=False)

    # RTMP ingest
    stream_key   = Column(String(64), unique=True, nullable=False, index=True)
    ingest_url   = Column(Text)   # rtmp://ingest.flintx.tv/live

    # Current stream
    status       = Column(SAEnum(StreamStatus), default=StreamStatus.idle)
    current_stream_id = Column(UUID(as_uuid=False), ForeignKey("live_streams.id"), nullable=True)

    # Channel settings
    channel_title    = Column(String(200))
    channel_category = Column(SAEnum(StreamCategory), default=StreamCategory.other)
    channel_tags     = Column(Text)   # JSON array
    is_mature        = Column(Boolean, default=False)

    # Quality tier (determines ad eligibility)
    quality_tier     = Column(SAEnum(QualityTier), default=QualityTier.provisional)
    quality_score    = Column(Float, default=0.0)   # 0–100

    # Stats (all-time)
    total_streams    = Column(Integer, default=0)
    total_hours      = Column(Float, default=0.0)
    peak_viewers     = Column(Integer, default=0)
    total_views      = Column(Integer, default=0)
    total_earnings   = Column(Integer, default=0)   # USD cents

    # Sub price
    sub_price_cents  = Column(Integer, default=499)   # $4.99

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    streams = relationship("LiveStream", back_populates="channel",
                          foreign_keys="LiveStream.channel_id")
    subs    = relationship("StreamSub", back_populates="channel")

    __table_args__ = (
        Index("ix_stream_channels_creator", "creator_id"),
        Index("ix_stream_channels_status", "status"),
    )


# ─────────────────────────────────────────────
# LIVE STREAM (one per broadcast session)
# ─────────────────────────────────────────────

class LiveStream(Base):
    """
    One row per broadcast session.
    Created when the creator goes live, updated throughout, finalised when they end.
    """
    __tablename__ = "live_streams"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    channel_id   = Column(UUID(as_uuid=False), ForeignKey("stream_channels.id"), nullable=False)
    creator_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    title        = Column(String(500))
    category     = Column(SAEnum(StreamCategory), default=StreamCategory.other)
    tags         = Column(Text)
    thumbnail_url = Column(Text)
    is_mature    = Column(Boolean, default=False)
    status       = Column(SAEnum(StreamStatus), default=StreamStatus.live)

    # HLS playback
    playback_url = Column(Text)    # https://cdn.flintx.tv/live/{stream_id}/index.m3u8
    hls_1080_url = Column(Text)
    hls_720_url  = Column(Text)
    hls_360_url  = Column(Text)

    # Ingest health
    ingest_health = Column(Float, default=1.0)   # 0.0–1.0
    bitrate_kbps  = Column(Integer, default=0)
    fps           = Column(Integer, default=0)

    # Viewers
    current_viewers = Column(Integer, default=0)
    peak_viewers    = Column(Integer, default=0)
    total_views     = Column(Integer, default=0)

    # Revenue
    ad_revenue      = Column(Integer, default=0)   # USD cents gross
    creator_earnings = Column(Integer, default=0)  # USD cents 80%
    tip_earnings    = Column(Integer, default=0)   # USD cents from tips
    sub_earnings    = Column(Integer, default=0)   # USD cents from subs

    # VOD
    vod_video_id    = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=True)
    duration_s      = Column(Integer, default=0)

    # Collab
    is_collab       = Column(Boolean, default=False)
    collab_split_id = Column(UUID(as_uuid=False), ForeignKey("collab_splits.id"), nullable=True)

    started_at  = Column(DateTime, default=datetime.utcnow)
    ended_at    = Column(DateTime)

    channel = relationship("StreamChannel", back_populates="streams",
                          foreign_keys=[channel_id])
    chat    = relationship("ChatMessage", back_populates="stream")

    __table_args__ = (
        Index("ix_streams_channel", "channel_id"),
        Index("ix_streams_status", "status"),
        Index("ix_streams_category", "category"),
        Index("ix_streams_started", "started_at"),
    )


# ─────────────────────────────────────────────
# LIVE CHAT
# ─────────────────────────────────────────────

class ChatMessage(Base):
    """
    Chat messages during a live stream.
    In production: stored in Redis for live delivery, written to PostgreSQL for VOD replay.
    """
    __tablename__ = "chat_messages"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    stream_id   = Column(UUID(as_uuid=False), ForeignKey("live_streams.id"), nullable=False)
    user_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    type        = Column(SAEnum(ChatMsgType), default=ChatMsgType.message)
    content     = Column(Text, nullable=False)
    amount      = Column(Integer, default=0)    # USD cents for tips
    pinned      = Column(Boolean, default=False)
    deleted     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    stream = relationship("LiveStream", back_populates="chat")

    __table_args__ = (
        Index("ix_chat_stream", "stream_id"),
        Index("ix_chat_created", "created_at"),
    )


# ─────────────────────────────────────────────
# STREAM SUBSCRIPTIONS
# ─────────────────────────────────────────────

class StreamSub(Base):
    """
    A viewer subscribes to a creator's stream channel.
    Creator gets 80% of sub revenue (not 95% like Kick — but they also get ad revenue,
    which Kick creators don't have). Total creator earnings on FlintX exceed Kick
    once ad revenue is factored in.
    """
    __tablename__ = "stream_subs"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    channel_id  = Column(UUID(as_uuid=False), ForeignKey("stream_channels.id"))
    subscriber_id = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    price_cents = Column(Integer)      # price paid
    creator_cut = Column(Integer)      # 80% of price
    gift        = Column(Boolean, default=False)   # gifted sub
    gifted_by   = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=True)
    paypal_sub_id = Column(Text)
    active      = Column(Boolean, default=True)
    expires_at  = Column(DateTime)
    created_at  = Column(DateTime, default=datetime.utcnow)

    channel = relationship("StreamChannel", back_populates="subs")

    __table_args__ = (
        Index("ix_stream_subs_channel", "channel_id"),
        Index("ix_stream_subs_subscriber", "subscriber_id"),
    )


# ─────────────────────────────────────────────
# CREATOR QUALITY SCORE
# ─────────────────────────────────────────────

class CreatorQualityScore(Base):
    """
    FlintX's alternative to YouTube's subscriber threshold.
    Content-based quality assessment — not audience-size gate.
    Determines which ad campaigns a creator is eligible for.
    Updated after every new approved video.
    """
    __tablename__ = "creator_quality_scores"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True)

    # Score components (0.0–1.0 each)
    content_consistency = Column(Float, default=0.0)   # uploads regularly
    completion_rate     = Column(Float, default=0.0)   # viewers finish videos
    moderation_record   = Column(Float, default=1.0)   # clean = 1.0, flags reduce it
    engagement_rate     = Column(Float, default=0.0)   # likes + comments / views
    content_originality = Column(Float, default=0.5)   # manual assessment

    # Composite score and tier
    total_score  = Column(Float, default=0.0)   # 0–100
    quality_tier = Column(SAEnum(QualityTier), default=QualityTier.provisional)

    # Thresholds met
    videos_approved  = Column(Integer, default=0)
    videos_flagged   = Column(Integer, default=0)
    videos_rejected  = Column(Integer, default=0)

    last_updated = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_quality_creator", "creator_id"),
        Index("ix_quality_tier", "quality_tier"),
    )
