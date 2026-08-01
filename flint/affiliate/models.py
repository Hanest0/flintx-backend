"""
FlintX — Affiliate Marketing Models

Two revenue streams:

1. CREATOR AFFILIATE PROGRAMME
   Creators join affiliate networks and promote products in their videos.
   FlintX tracks clicks via its own redirect (flintx.tv/go/CODE).
   FlintX earns 15% of every commission the creator earns through FlintX.
   Creator keeps 85%.

   Why 15% and not more:
   The creator did the work. They built the audience. They made the recommendation.
   FlintX provides the tracking infrastructure and the audience trust layer.
   15% is fair. 30% would push creators to use direct affiliate links instead.

2. FLINTX PLATFORM AFFILIATE (contextual)
   FlintX joins Amazon Associates, ShareASale, CJ Affiliate, Impact.
   Relevant product links appear automatically in:
   - Video descriptions (below the fold)
   - "Products mentioned" panels on video pages
   - Category browse pages (e.g. Finance page shows relevant FinTech tools)
   All revenue from these links goes 100% to FlintX.

3. ADSENSE DISPLAY ADS
   Google AdSense display units on:
   - Video watch pages (sidebar, below video)
   - Browse/discover pages
   - Channel pages
   - Search results
   Tracked separately from video pre-roll ads.
   Revenue 100% to FlintX. Fills inventory direct advertisers haven't bought.

Combined, these add 3 new income streams on top of the existing 8.
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


class AffiliateNetwork(str, enum.Enum):
    amazon        = "amazon"          # Amazon Associates — broad product range
    shareasale    = "shareasale"      # SaaS, finance tools, lifestyle
    cj_affiliate  = "cj_affiliate"   # Large brand catalogue
    impact        = "impact"          # Tech and fintech brands
    awin          = "awin"            # European brands
    clickbank     = "clickbank"       # Digital products, courses
    partnerstack  = "partnerstack"    # SaaS products
    custom        = "custom"          # Direct brand affiliate deal


class AffiliateProgramStatus(str, enum.Enum):
    pending   = "pending"    # applied, awaiting approval
    active    = "active"     # approved and tracking
    paused    = "paused"     # creator paused
    suspended = "suspended"  # violations


class AffiliateClickStatus(str, enum.Enum):
    clicked    = "clicked"
    converted  = "converted"    # purchase made
    cancelled  = "cancelled"    # returned/refunded


class AdSensePlacement(str, enum.Enum):
    watch_sidebar        = "watch_sidebar"       # beside video player
    watch_below          = "watch_below"         # below video player
    browse_banner        = "browse_banner"       # top of browse page
    browse_inline        = "browse_inline"       # between video rows
    channel_page         = "channel_page"        # creator channel pages
    search_results       = "search_results"      # beside search results
    live_sidebar         = "live_sidebar"        # beside live stream


# ─────────────────────────────────────────────
# CREATOR AFFILIATE PROGRAMME
# ─────────────────────────────────────────────

class AffiliateProgram(Base):
    """
    A creator's affiliate programme account.
    One per creator — they can add multiple affiliate links within it.
    """
    __tablename__ = "affiliate_programs"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    creator_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"), unique=True, nullable=False)

    status       = Column(SAEnum(AffiliateProgramStatus), default=AffiliateProgramStatus.active)

    # Payout split: creator keeps 85%, FlintX earns 15%
    creator_share_pct   = Column(Integer, default=85)
    platform_share_pct  = Column(Integer, default=15)

    # Lifetime earnings
    total_clicks     = Column(BigInteger, default=0)
    total_conversions = Column(BigInteger, default=0)
    total_earned_cents = Column(BigInteger, default=0)   # USD cents — creator's 85%
    platform_earned_cents = Column(BigInteger, default=0)  # USD cents — FlintX's 15%

    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    links = relationship("AffiliateLink", back_populates="program")

    __table_args__ = (
        Index("ix_affiliate_program_creator", "creator_id"),
    )


class AffiliateLink(Base):
    """
    An individual affiliate link a creator has created.
    FlintX wraps it: flintx.tv/go/{short_code} → original affiliate URL.

    The redirect:
    1. Records the click (viewer, creator, video context)
    2. Sets a FlintX cookie for attribution
    3. Redirects to the affiliate URL

    If the viewer buys, the affiliate network notifies FlintX via webhook.
    FlintX splits the commission: 85% to creator, 15% to platform.
    """
    __tablename__ = "affiliate_links"

    id           = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    program_id   = Column(UUID(as_uuid=False), ForeignKey("affiliate_programs.id"), nullable=False)
    creator_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    # Link identity
    name         = Column(String(200))          # "Hostinger hosting deal"
    short_code   = Column(String(20), unique=True, nullable=False, index=True)  # flintx.tv/go/{code}
    original_url = Column(Text, nullable=False)  # the actual affiliate URL
    display_url  = Column(Text)                  # shown to creator in dashboard
    network      = Column(SAEnum(AffiliateNetwork), default=AffiliateNetwork.custom)

    # Product details (shown in the FlintX "Products mentioned" panel)
    product_name  = Column(String(200))
    product_image = Column(Text)
    product_price = Column(String(50))           # display only, e.g. "$9.99/mo"
    product_description = Column(Text)
    product_category = Column(String(100))       # maps to FlintX niches

    # Commission structure
    commission_type  = Column(String(20), default="percentage")  # percentage | flat
    commission_rate  = Column(Float, default=0.0)    # e.g. 0.05 = 5%
    commission_flat  = Column(Integer, default=0)    # USD cents flat fee per conversion

    # Stats
    total_clicks      = Column(Integer, default=0)
    total_conversions = Column(Integer, default=0)
    total_revenue_cents = Column(Integer, default=0)  # gross commission earned
    creator_earned_cents = Column(Integer, default=0)  # 85% of gross
    platform_earned_cents = Column(Integer, default=0)  # 15% of gross

    # Tracking
    active       = Column(Boolean, default=True)
    expires_at   = Column(DateTime)    # some deals are time-limited

    created_at   = Column(DateTime, default=datetime.utcnow)

    program = relationship("AffiliateProgram", back_populates="links")
    clicks  = relationship("AffiliateClick", back_populates="link")

    __table_args__ = (
        Index("ix_affiliate_links_creator", "creator_id"),
        Index("ix_affiliate_links_short", "short_code"),
    )


class AffiliateClick(Base):
    """
    Every click through a FlintX affiliate redirect.
    Records full context: which creator, which video, which viewer.
    Used for attribution when a conversion webhook arrives.
    """
    __tablename__ = "affiliate_clicks"

    id          = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    link_id     = Column(UUID(as_uuid=False), ForeignKey("affiliate_links.id"), nullable=False)
    creator_id  = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    viewer_id   = Column(UUID(as_uuid=False), ForeignKey("users.id"))   # null if not logged in
    video_id    = Column(UUID(as_uuid=False), ForeignKey("videos.id"))  # which video drove the click

    status      = Column(SAEnum(AffiliateClickStatus), default=AffiliateClickStatus.clicked)

    # Attribution
    ip_hash     = Column(String(64))
    user_agent  = Column(Text)
    referrer    = Column(Text)

    # Conversion data (filled when webhook arrives)
    order_value_cents  = Column(Integer)     # what the viewer spent
    commission_cents   = Column(Integer)     # gross commission from network
    creator_cut_cents  = Column(Integer)     # 85% to creator
    platform_cut_cents = Column(Integer)     # 15% to FlintX
    converted_at       = Column(DateTime)
    network_order_id   = Column(String(200)) # affiliate network's order reference

    session_id  = Column(String(64))         # for cookie-based attribution
    created_at  = Column(DateTime, default=datetime.utcnow)

    link = relationship("AffiliateLink", back_populates="clicks")

    __table_args__ = (
        Index("ix_aff_clicks_link", "link_id"),
        Index("ix_aff_clicks_viewer", "viewer_id"),
        Index("ix_aff_clicks_session", "session_id"),
        Index("ix_aff_clicks_status", "status"),
    )


# ─────────────────────────────────────────────
# FLINTX PLATFORM CONTEXTUAL AFFILIATE
# ─────────────────────────────────────────────

class PlatformAffiliateProduct(Base):
    """
    Products FlintX itself promotes as a platform affiliate.
    These appear in the "Products mentioned" panel, category pages,
    and contextual placements — without creator involvement.
    All revenue goes to FlintX (no creator share).

    Examples:
    - Finance niche pages: Trading 212, Public.com, NordVPN
    - Tech niche pages: Hostinger, NordVPN, various SaaS tools
    - Education pages: Brilliant.org, Skillshare, Coursera
    """
    __tablename__ = "platform_affiliate_products"

    id             = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name           = Column(String(200), nullable=False)
    network        = Column(SAEnum(AffiliateNetwork))
    affiliate_url  = Column(Text, nullable=False)
    short_code     = Column(String(20), unique=True, index=True)  # flintx.tv/go/p/{code}
    image_url      = Column(Text)
    description    = Column(Text)
    price_display  = Column(String(50))

    # Which niches to show this on
    target_niches  = Column(Text)   # JSON array of niche names

    # Commission
    commission_type = Column(String(20), default="percentage")
    commission_rate = Column(Float, default=0.0)

    # Stats
    total_clicks       = Column(Integer, default=0)
    total_conversions  = Column(Integer, default=0)
    total_earned_cents = Column(Integer, default=0)   # all to FlintX

    active     = Column(Boolean, default=True)
    priority   = Column(Integer, default=0)   # higher = shown first

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_plat_aff_active", "active"),
        Index("ix_plat_aff_priority", "priority"),
    )


# ─────────────────────────────────────────────
# ADSENSE DISPLAY ADS
# ─────────────────────────────────────────────

class AdSenseConfig(Base):
    """
    FlintX AdSense configuration.
    Single row — global AdSense settings.
    """
    __tablename__ = "adsense_config"

    id              = Column(Integer, primary_key=True, default=1)
    publisher_id    = Column(String(100))   # ca-pub-XXXXXXXXXXXXXXXXX
    enabled         = Column(Boolean, default=False)
    mode            = Column(String(20), default="auto")   # auto | manual

    # Auto ads: Google decides placement
    auto_ads_enabled = Column(Boolean, default=True)

    # Manual placements: specific ad unit IDs per placement
    unit_watch_sidebar    = Column(String(100))
    unit_watch_below      = Column(String(100))
    unit_browse_banner    = Column(String(100))
    unit_browse_inline    = Column(String(100))
    unit_channel_page     = Column(String(100))
    unit_search_results   = Column(String(100))
    unit_live_sidebar     = Column(String(100))

    # Revenue tracking (pulled from AdSense API daily)
    total_earned_cents    = Column(BigInteger, default=0)
    monthly_earned_cents  = Column(BigInteger, default=0)
    last_synced_at        = Column(DateTime)

    # Page RPM targets by placement
    target_rpm_watch      = Column(Integer, default=150)   # $1.50 RPM
    target_rpm_browse     = Column(Integer, default=80)    # $0.80 RPM

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdSenseSnapshot(Base):
    """
    Daily AdSense revenue snapshots.
    Pulled from AdSense API each morning.
    Used for the admin dashboard revenue chart.
    """
    __tablename__ = "adsense_snapshots"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    date            = Column(String(10), nullable=False, unique=True)   # YYYY-MM-DD
    impressions     = Column(Integer, default=0)
    clicks          = Column(Integer, default=0)
    ctr             = Column(Float, default=0.0)    # click-through rate
    rpm_cents       = Column(Integer, default=0)    # page RPM in USD cents
    earned_cents    = Column(Integer, default=0)    # total earned today
    created_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_adsense_date", "date"),
    )


# ─────────────────────────────────────────────
# AFFILIATE EARNINGS LEDGER
# ─────────────────────────────────────────────

class AffiliateEarning(Base):
    """
    Every affiliate commission earned and split.
    One row per conversion event.
    """
    __tablename__ = "affiliate_earnings"

    id              = Column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    click_id        = Column(UUID(as_uuid=False), ForeignKey("affiliate_clicks.id"))
    link_id         = Column(UUID(as_uuid=False), ForeignKey("affiliate_links.id"))
    creator_id      = Column(UUID(as_uuid=False), ForeignKey("users.id"))

    gross_cents         = Column(Integer, nullable=False)   # total commission from network
    creator_cents       = Column(Integer, nullable=False)   # 85% to creator
    platform_cents      = Column(Integer, nullable=False)   # 15% to FlintX
    order_value_cents   = Column(Integer)                   # what the buyer spent

    paid_to_creator     = Column(Boolean, default=False)
    paid_at             = Column(DateTime)

    network             = Column(SAEnum(AffiliateNetwork))
    network_order_id    = Column(String(200))

    earned_at           = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_aff_earn_creator", "creator_id"),
        Index("ix_aff_earn_paid", "paid_to_creator"),
        Index("ix_aff_earn_date", "earned_at"),
    )
