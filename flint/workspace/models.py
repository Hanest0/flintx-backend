"""
FlintX — Workspace & Multi-Channel Models

The FlintX moat: one login, unlimited channels, unified intelligence layer.

Hierarchy:
  User
    └── Workspace          (your creator brand — "Kai Media")
          ├── Channel[]    (Finance With Kai, Kai Tech, Kai Eats)
          ├── AudienceBridge[]   (cross-channel subscriber campaigns)
          ├── ContentPassport[]  (one video → multiple channels)
          └── CollabSplit[]      (revenue split on joint videos)
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

class WorkspaceRole(str, enum.Enum):
    owner   = "owner"     # full control
    manager = "manager"   # can upload, edit, view analytics
    editor  = "editor"    # can upload and edit only
    analyst = "analyst"   # view analytics only

class ChannelStatus(str, enum.Enum):
    active    = "active"
    paused    = "paused"
    suspended = "suspended"

class BridgeStatus(str, enum.Enum):
    draft     = "draft"
    scheduled = "scheduled"
    sent      = "sent"
    cancelled = "cancelled"

class PassportStatus(str, enum.Enum):
    active    = "active"
    paused    = "paused"
    ended     = "ended"

class CollabStatus(str, enum.Enum):
    pending   = "pending"    # waiting for other creator to accept
    active    = "active"     # live — splits happening
    completed = "completed"
    declined  = "declined"

class LendStatus(str, enum.Enum):
    active  = "active"
    ended   = "ended"
    expired = "expired"


# ─────────────────────────────────────────────
# WORKSPACE
# ─────────────────────────────────────────────

class Workspace(Base):
    """
    Top-level creator brand. One user can own one workspace.
    Managers/editors/analysts can be invited to collaborate.
    """
    __tablename__ = "workspaces"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    owner_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, unique=True)
    name         = Column(String(200), nullable=False)      # "Kai Media"
    handle       = Column(String(50), unique=True, index=True)  # @kaimedia
    description  = Column(Text)
    logo_url     = Column(Text)
    website      = Column(Text)
    total_channels = Column(Integer, default=0)

    # Aggregated stats (cached, updated hourly)
    total_subscribers = Column(Integer, default=0)
    total_views       = Column(Integer, default=0)
    total_earnings    = Column(Integer, default=0)   # USD cents lifetime
    monthly_earnings  = Column(Integer, default=0)   # USD cents current month

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    channels = relationship("Channel", back_populates="workspace")
    members  = relationship("WorkspaceMember", back_populates="workspace")
    bridges  = relationship("AudienceBridge", back_populates="workspace")
    passports = relationship("ContentPassport", back_populates="workspace")


class WorkspaceMember(Base):
    """
    Team members invited to a workspace.
    Owner invites managers, editors, analysts.
    """
    __tablename__ = "workspace_members"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    workspace_id = Column(UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False)
    user_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    role         = Column(SAEnum(WorkspaceRole), default=WorkspaceRole.editor)
    invited_by   = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    accepted     = Column(Boolean, default=False)
    invite_token = Column(Text)
    joined_at    = Column(DateTime)
    created_at   = Column(DateTime, default=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="members")

    __table_args__ = (
        Index("ix_ws_members_workspace_user", "workspace_id", "user_id", unique=True),
    )


# ─────────────────────────────────────────────
# CHANNEL
# ─────────────────────────────────────────────

class Channel(Base):
    """
    A channel within a workspace. Each has its own niche,
    subscriber base, video library, and earnings — but all
    roll up to the workspace owner's single wallet.
    """
    __tablename__ = "channels"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    workspace_id = Column(UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False)
    owner_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    # Identity
    name         = Column(String(200), nullable=False)   # "Finance With Kai"
    handle       = Column(String(50), unique=True, index=True)  # @financewithkai
    niche        = Column(String(50))
    description  = Column(Text)
    avatar_url   = Column(Text)
    banner_url   = Column(Text)
    status       = Column(SAEnum(ChannelStatus), default=ChannelStatus.active)

    # Stats (updated in real time)
    subscriber_count = Column(Integer, default=0)
    video_count      = Column(Integer, default=0)
    total_views      = Column(Integer, default=0)
    avg_completion   = Column(Float, default=0.0)

    # Revenue (USD cents)
    total_earnings   = Column(Integer, default=0)
    monthly_earnings = Column(Integer, default=0)
    pending_payout   = Column(Integer, default=0)

    # Channel Lending
    is_lending       = Column(Boolean, default=False)
    lending_expires  = Column(DateTime)

    # CPM data (actual earned, not estimated — this is the FlintX data moat)
    actual_cpm_cents = Column(Integer, default=0)   # real CPM from this channel's ad data
    cpm_sample_count = Column(Integer, default=0)   # how many impressions in the sample

    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace    = relationship("Workspace", back_populates="channels")
    subscribers  = relationship("ChannelSubscriber", back_populates="channel")
    lending_logs = relationship("ChannelLend", foreign_keys="ChannelLend.from_channel_id", back_populates="from_channel")

    __table_args__ = (
        Index("ix_channels_workspace", "workspace_id"),
        Index("ix_channels_niche", "niche"),
    )


class ChannelSubscriber(Base):
    """
    Who subscribes to each channel.
    This is the data that powers Audience Bridge.
    """
    __tablename__ = "channel_subscribers"

    id         = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    channel_id = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)
    user_id    = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    notif_on   = Column(Boolean, default=True)   # notifications enabled
    subscribed_at = Column(DateTime, default=datetime.utcnow)

    channel = relationship("Channel", back_populates="subscribers")

    __table_args__ = (
        Index("ix_subs_channel_user", "channel_id", "user_id", unique=True),
        Index("ix_subs_user", "user_id"),
    )


# ─────────────────────────────────────────────
# AUDIENCE BRIDGE
# ─────────────────────────────────────────────

class AudienceBridge(Base):
    """
    FlintX exclusive: notify subscribers from Channel A about Channel B.
    Creators can grow a new channel using an existing audience.
    YouTube cannot do this across Brand Accounts.

    Example: "Finance With Kai" has 500K subs.
    Creator launches "Kai Tech". Sends a Bridge to 500K Finance subs:
    "Hey — I just launched a tech channel. Check it out."
    Result: new channel gets instant warm audience instead of starting at zero.
    """
    __tablename__ = "audience_bridges"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    workspace_id    = Column(UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False)
    from_channel_id = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)
    to_channel_id   = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)

    # Message shown to subscribers
    subject         = Column(String(200))
    message         = Column(Text)
    cta_label       = Column(String(100), default="Subscribe now →")

    # Targeting: send to all subs, or filter by engagement
    target_all      = Column(Boolean, default=True)
    min_watch_pct   = Column(Float, default=0.0)   # only subs who watched >X% of videos

    status          = Column(SAEnum(BridgeStatus), default=BridgeStatus.draft)
    scheduled_at    = Column(DateTime)
    sent_at         = Column(DateTime)

    # Results
    recipients      = Column(Integer, default=0)
    opened          = Column(Integer, default=0)
    clicked         = Column(Integer, default=0)
    converted       = Column(Integer, default=0)   # actually subscribed to new channel

    created_at      = Column(DateTime, default=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="bridges")

    __table_args__ = (
        Index("ix_bridge_workspace", "workspace_id"),
    )


# ─────────────────────────────────────────────
# CONTENT PASSPORT
# ─────────────────────────────────────────────

class ContentPassport(Base):
    """
    FlintX exclusive: publish one video across multiple channels
    with different titles, thumbnails, and descriptions.

    Example: Creator uploads "How I made $10K this month"
    - Finance channel: "How I made $10K this month" — finance thumbnail
    - Tech channel: "The tools that made me $10K" — tech thumbnail
    - Lifestyle channel: "My income breakdown this month" — lifestyle thumbnail

    One upload. Three audiences. Triple the ad revenue.
    No competitor has this.
    """
    __tablename__ = "content_passports"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    workspace_id = Column(UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False)
    source_video_id = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=False)
    status       = Column(SAEnum(PassportStatus), default=PassportStatus.active)
    created_at   = Column(DateTime, default=datetime.utcnow)

    workspace    = relationship("Workspace", back_populates="passports")
    distributions = relationship("PassportDistribution", back_populates="passport")


class PassportDistribution(Base):
    """
    One row per channel a video is distributed to via Content Passport.
    Each distribution has its own title, thumbnail, description.
    """
    __tablename__ = "passport_distributions"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    passport_id  = Column(UUID(as_uuid=False), ForeignKey("content_passports.id"), nullable=False)
    channel_id   = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)
    video_id     = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=False)

    # Channel-specific metadata (overrides source video)
    custom_title       = Column(String(500))
    custom_description = Column(Text)
    custom_thumbnail   = Column(Text)
    custom_tags        = Column(Text)   # JSON array

    # Per-distribution stats
    views         = Column(Integer, default=0)
    earnings      = Column(Integer, default=0)   # USD cents from this distribution

    published_at  = Column(DateTime)
    created_at    = Column(DateTime, default=datetime.utcnow)

    passport = relationship("ContentPassport", back_populates="distributions")


# ─────────────────────────────────────────────
# COLLAB SPLIT
# ─────────────────────────────────────────────

class CollabSplit(Base):
    """
    FlintX exclusive: automatic revenue split on collab videos.

    Creator A and Creator B make a video together.
    They agree: 60% to A, 40% to B.
    FlintX handles the split on every ad impression — forever.
    No invoices, no manual PayPal transfers, no disputes.

    Both creators must accept the split before it goes live.
    """
    __tablename__ = "collab_splits"

    id               = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    video_id         = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=False, unique=True)

    # Creator A (initiator)
    creator_a_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    creator_a_share  = Column(Integer, nullable=False)   # percentage e.g. 60

    # Creator B (invited)
    creator_b_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    creator_b_share  = Column(Integer, nullable=False)   # percentage e.g. 40

    # Optional: up to 4 creators total
    creator_c_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    creator_c_share  = Column(Integer, default=0)
    creator_d_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    creator_d_share  = Column(Integer, default=0)

    status           = Column(SAEnum(CollabStatus), default=CollabStatus.pending)
    accepted_at      = Column(DateTime)
    message          = Column(Text)   # message from creator A to B when proposing

    # Earnings tracking (USD cents)
    total_earned     = Column(Integer, default=0)
    creator_a_earned = Column(Integer, default=0)
    creator_b_earned = Column(Integer, default=0)
    creator_c_earned = Column(Integer, default=0)
    creator_d_earned = Column(Integer, default=0)

    created_at       = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_collab_creator_a", "creator_a_id"),
        Index("ix_collab_creator_b", "creator_b_id"),
    )


# ─────────────────────────────────────────────
# CHANNEL LENDING
# ─────────────────────────────────────────────

class ChannelLend(Base):
    """
    FlintX exclusive: a creator with an established audience
    lends their reach to boost a new channel for 30 days.

    How it works:
    - Channel A (500K subs) agrees to "lend" to Channel B (new)
    - For 30 days, Channel B videos appear in Channel A's feed
      as "Recommended by [Channel A]"
    - Channel B gets warm-audience exposure impossible on any other platform
    - Channel A earns a lending fee (flat or % of Channel B's new revenue)

    This kills the cold-start problem for new creators.
    """
    __tablename__ = "channel_lends"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    from_channel_id = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)
    to_channel_id   = Column(UUID(as_uuid=False), ForeignKey("channels.id"), nullable=False)

    # Terms
    duration_days   = Column(Integer, default=30)
    fee_type        = Column(String(20), default="flat")   # flat | revenue_share
    fee_flat_cents  = Column(Integer, default=0)           # USD cents one-time
    fee_revenue_pct = Column(Integer, default=0)           # % of to_channel revenue during period

    status          = Column(SAEnum(LendStatus), default=LendStatus.active)
    starts_at       = Column(DateTime, default=datetime.utcnow)
    ends_at         = Column(DateTime)

    # Results
    impressions_delivered = Column(Integer, default=0)
    new_subs_generated    = Column(Integer, default=0)
    revenue_generated     = Column(Integer, default=0)   # USD cents earned by to_channel during lend
    fee_earned            = Column(Integer, default=0)   # USD cents earned by from_channel

    created_at      = Column(DateTime, default=datetime.utcnow)

    from_channel = relationship("Channel", foreign_keys="ChannelLend.from_channel_id", back_populates="lending_logs")

    __table_args__ = (
        Index("ix_lend_from", "from_channel_id"),
        Index("ix_lend_to", "to_channel_id"),
        Index("ix_lend_status", "status"),
    )


# ─────────────────────────────────────────────
# WORKSPACE ANALYTICS SNAPSHOT
# ─────────────────────────────────────────────

class WorkspaceSnapshot(Base):
    """
    Daily snapshot of workspace-level aggregated analytics.
    Powers the unified dashboard — all channels side by side.
    Stored daily so you can chart trends over time.
    """
    __tablename__ = "workspace_snapshots"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    workspace_id = Column(UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False)
    date         = Column(String(10), nullable=False)   # YYYY-MM-DD

    # Aggregated across all channels
    total_views      = Column(Integer, default=0)
    total_watch_mins = Column(Integer, default=0)
    total_earnings   = Column(Integer, default=0)   # USD cents
    new_subscribers  = Column(Integer, default=0)

    # Per-channel breakdown (JSON: {channel_id: {views, earnings, subs}})
    channel_breakdown = Column(Text)

    # Best performing channel today
    top_channel_id    = Column(UUID(as_uuid=False), ForeignKey("channels.id"))
    top_channel_views = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_snapshot_workspace_date", "workspace_id", "date", unique=True),
    )
