"""
FlintX — Phased Payout System Models

The four-phase launch protection model.
Protects FlintX cash flow during the audience-building phase
while keeping creators engaged through visible locked credit accumulation.

PHASES (collective unique registered viewers):
  Phase 1 — Foundation:  0 – 25,000 viewers
    Creator share: 40% (locked credits, no cash)
    Viewer Pass cashout: blocked
    Studio subs: unaffected — paid to FlintX operating revenue immediately

  Phase 2 — Momentum:   25,001 – 75,000 viewers
    Creator share: 60% (50% cash / 50% credits)
    Viewer Pass cashout: unlocked at $10 minimum
    Partial payouts active

  Phase 3 — Full Launch: 75,001 – 150,000 viewers
    Creator share: 70%
    Viewer Pass cashout: $20 minimum
    All Phase 1/2 locked credits convert to cash automatically
    Minimum payout: $30

  Phase 4 — Standard:   150,001+ viewers
    Creator share: 80% (full model)
    Viewer Pass cashout: $20 minimum
    Minimum payout: $50

UNIQUE VIEWER DEFINITION (precise, public, bot-proof):
  A unique viewer is one registered FlintX account that has watched
  a minimum of 60 seconds of content. Anonymous views excluded.
  One account = one count regardless of how many videos watched.

FOUNDING CREATOR BONUS:
  Creators who join during Phase 1 receive a 10% bonus on their
  first cash withdrawal. Their locked credits convert at 110 cents
  per 100 cents locked. Badge: "FlintX Founding Creator."
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Float,
    ForeignKey, Integer, Enum as SAEnum, Index, BigInteger
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from ..database.models import Base


def new_uuid():
    return str(uuid.uuid4())


class PlatformPhase(str, enum.Enum):
    foundation  = "foundation"   # Phase 1: 0–25K
    momentum    = "momentum"     # Phase 2: 25K–75K
    full_launch = "full_launch"  # Phase 3: 75K–150K
    standard    = "standard"     # Phase 4: 150K+


class CreditStatus(str, enum.Enum):
    locked    = "locked"     # Phase 1 — visible but not withdrawable
    partial   = "partial"    # Phase 2 — 50% convertible to cash
    available = "available"  # Phase 3/4 — fully withdrawable
    paid_out  = "paid_out"   # withdrawn


class MilestoneType(str, enum.Enum):
    phase_1_start  = "phase_1_start"
    phase_2_unlock = "phase_2_unlock"   # 25,000 viewers
    phase_3_unlock = "phase_3_unlock"   # 75,000 viewers
    phase_4_unlock = "phase_4_unlock"   # 150,000 viewers
    custom         = "custom"


# ─────────────────────────────────────────────
# PLATFORM STATE (single row — global state)
# ─────────────────────────────────────────────

class PlatformState(Base):
    """
    Single-row table tracking the platform's current phase and
    all collective viewer counts. Updated in real time.
    """
    __tablename__ = "platform_state"

    id              = Column(Integer, primary_key=True, default=1)

    # Current phase
    phase           = Column(SAEnum(PlatformPhase), default=PlatformPhase.foundation)
    phase_activated_at = Column(DateTime, default=datetime.utcnow)

    # Collective viewer count — unique registered accounts, 60s+ watched
    collective_viewers = Column(BigInteger, default=0)

    # Phase thresholds (USD cents, configurable)
    phase_2_threshold  = Column(Integer, default=25_000)    # viewers
    phase_3_threshold  = Column(Integer, default=75_000)    # viewers
    phase_4_threshold  = Column(Integer, default=150_000)   # viewers

    # Creator revenue share per phase (basis points — 4000 = 40%)
    phase_1_creator_bps = Column(Integer, default=4000)   # 40%
    phase_2_creator_bps = Column(Integer, default=6000)   # 60%
    phase_3_creator_bps = Column(Integer, default=7000)   # 70%
    phase_4_creator_bps = Column(Integer, default=8000)   # 80%

    # Payout minimums per phase (USD cents)
    phase_2_min_payout  = Column(Integer, default=1000)   # $10.00
    phase_3_min_payout  = Column(Integer, default=3000)   # $30.00
    phase_4_min_payout  = Column(Integer, default=5000)   # $50.00

    # Viewer Pass cashout minimums per phase (USD cents)
    phase_2_viewer_min  = Column(Integer, default=1000)   # $10.00
    phase_3_viewer_min  = Column(Integer, default=2000)   # $20.00
    phase_4_viewer_min  = Column(Integer, default=2000)   # $20.00

    # Total locked credits liability (USD cents)
    total_locked_credits = Column(BigInteger, default=0)

    # Total cash paid out all time (USD cents)
    total_paid_out      = Column(BigInteger, default=0)

    # Founding creator bonus rate (basis points — 1000 = 10%)
    founding_bonus_bps  = Column(Integer, default=1000)   # 10%

    # Platform health
    total_creators      = Column(Integer, default=0)
    total_viewers       = Column(Integer, default=0)
    total_videos        = Column(Integer, default=0)
    total_ad_revenue    = Column(BigInteger, default=0)   # USD cents gross

    last_updated        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    launched_at         = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# VIEWER MILESTONE LOG
# ─────────────────────────────────────────────

class ViewerMilestone(Base):
    """
    Log of every unique viewer count milestone reached.
    Immutable record — cannot be deleted or edited.
    Used for trust/transparency: creators can see the history.
    """
    __tablename__ = "viewer_milestones"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    milestone_type  = Column(SAEnum(MilestoneType), nullable=False)
    viewer_count    = Column(BigInteger, nullable=False)
    phase_triggered = Column(SAEnum(PlatformPhase))
    description     = Column(Text)
    celebrated      = Column(Boolean, default=False)   # has the milestone been announced?
    achieved_at     = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_milestones_type", "milestone_type"),
        Index("ix_milestones_achieved", "achieved_at"),
    )


# ─────────────────────────────────────────────
# UNIQUE VIEWER TRACKING
# ─────────────────────────────────────────────

class UniqueViewerRecord(Base):
    """
    One row per registered user who has watched 60+ seconds of content.
    Used to calculate the collective viewer count accurately.
    Bot-proof: requires registered account.
    """
    __tablename__ = "unique_viewer_records"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id     = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True, nullable=False)
    first_watch = Column(DateTime, default=datetime.utcnow)
    total_watch_s = Column(Integer, default=0)   # total seconds watched across all content

    __table_args__ = (
        Index("ix_unique_viewers_user", "user_id"),
        Index("ix_unique_viewers_first", "first_watch"),
    )


# ─────────────────────────────────────────────
# CREATOR CREDIT LEDGER
# ─────────────────────────────────────────────

class CreatorCreditLedger(Base):
    """
    Every credit earned by a creator, with its phase status.
    Separate from the main Transaction table — this tracks
    the lock/unlock lifecycle of earnings.

    When Phase 3 activates, all locked credits are converted
    to available cash automatically via a background job.
    """
    __tablename__ = "creator_credit_ledger"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    amount_cents    = Column(Integer, nullable=False)    # USD cents
    phase_earned    = Column(SAEnum(PlatformPhase), nullable=False)
    status          = Column(SAEnum(CreditStatus), default=CreditStatus.locked)

    # Source of the credit
    source          = Column(String(50))    # ad_revenue | viewer_credit | tip | sub
    video_id        = Column(UUID(as_uuid=False), ForeignKey("videos.id"), nullable=True)
    stream_id       = Column(UUID(as_uuid=False), ForeignKey("live_streams.id"), nullable=True)

    # Phase 2: partial conversion tracking
    cash_portion    = Column(Integer, default=0)     # USD cents already converted to cash
    credit_portion  = Column(Integer, default=0)     # USD cents still locked

    # Phase 3 conversion
    converted_at    = Column(DateTime)
    conversion_bonus = Column(Integer, default=0)    # founding creator bonus cents

    # Whether this creator was a founding creator (joined in Phase 1)
    founding_creator = Column(Boolean, default=False)

    earned_at       = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ledger_creator", "creator_id"),
        Index("ix_ledger_status", "status"),
        Index("ix_ledger_phase", "phase_earned"),
        Index("ix_ledger_earned", "earned_at"),
    )


# ─────────────────────────────────────────────
# FOUNDING CREATOR RECORD
# ─────────────────────────────────────────────

class FoundingCreator(Base):
    """
    Creators who joined during Phase 1 receive special status:
    - 10% bonus on first withdrawal (locked credits convert at 110%)
    - "FlintX Founding Creator" badge
    - Priority in advertiser category exclusivity deals at Phase 4
    - Early access to new features
    """
    __tablename__ = "founding_creators"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    user_id         = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True)
    joined_at       = Column(DateTime, default=datetime.utcnow)
    viewer_count_at_join = Column(Integer, default=0)   # how many viewers when they joined

    # Bonus tracking
    bonus_applied   = Column(Boolean, default=False)
    bonus_amount    = Column(Integer, default=0)    # USD cents bonus paid
    bonus_paid_at   = Column(DateTime)

    # Stats at time of first payout
    videos_by_phase_2  = Column(Integer, default=0)
    earnings_locked    = Column(Integer, default=0)   # USD cents locked in Phase 1

    __table_args__ = (
        Index("ix_founding_creators_user", "user_id"),
    )


# ─────────────────────────────────────────────
# PHASE TRANSITION LOG
# ─────────────────────────────────────────────

class PhaseTransitionLog(Base):
    """
    Immutable record of every phase transition.
    Used for transparency, auditing, and creator trust.
    """
    __tablename__ = "phase_transition_logs"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    from_phase      = Column(SAEnum(PlatformPhase))
    to_phase        = Column(SAEnum(PlatformPhase), nullable=False)
    viewer_count    = Column(BigInteger, nullable=False)
    total_creators  = Column(Integer, default=0)
    locked_credits_converted = Column(BigInteger, default=0)   # USD cents unlocked
    bonus_paid      = Column(BigInteger, default=0)            # USD cents in founding bonuses
    transitioned_at = Column(DateTime, default=datetime.utcnow)
    notes           = Column(Text)
