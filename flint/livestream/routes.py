"""
FlintX — Live Streaming API Routes

Stream setup:
  POST /api/live/setup              — create/get stream channel + stream key
  GET  /api/live/channel            — get my stream channel
  PATCH /api/live/channel           — update channel settings
  POST /api/live/channel/rotate-key — regenerate stream key

Stream lifecycle (called by ingest server webhooks):
  POST /api/live/webhook/start      — ingest server fires when creator goes live
  POST /api/live/webhook/end        — ingest server fires when stream ends
  POST /api/live/webhook/health     — periodic health update from ingest server

Discovery:
  GET  /api/live/directory          — all live streams (paginated + filtered)
  GET  /api/live/featured           — featured/promoted live streams
  GET  /api/live/{stream_id}        — single stream + playback URL
  GET  /api/live/channel/{handle}   — stream channel by creator handle

Viewer actions:
  POST /api/live/{stream_id}/view   — record a viewer joining
  POST /api/live/{stream_id}/leave  — record a viewer leaving
  POST /api/live/{stream_id}/tip    — send a tip during stream
  POST /api/live/{stream_id}/sub    — subscribe to creator

Chat:
  GET  /api/live/{stream_id}/chat   — recent chat messages (poll fallback)
  POST /api/live/{stream_id}/chat   — send a chat message
  DELETE /api/live/{stream_id}/chat/{msg_id} — delete message (mod/creator)

VOD:
  GET  /api/live/vods               — my past streams as VODs
  GET  /api/live/vods/{stream_id}   — single VOD

Creator Quality Score:
  GET  /api/live/quality            — my quality score + tier
  GET  /api/live/quality/breakdown  — what's needed to reach next tier

Admin:
  GET  /api/live/admin/live         — all currently live streams
  POST /api/live/admin/suspend/{id} — suspend a stream
"""

import os
import secrets
import json
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    User, CreatorProfile, Transaction, TxnType, Video, VideoStatus,
)
from ..auth.routes import require_verified, require_admin
from ..currency.service import CurrencyService
from .models import (
    StreamChannel, LiveStream, ChatMessage, StreamSub,
    CreatorQualityScore, StreamStatus, StreamCategory,
    ChatMsgType, QualityTier,
)

router = APIRouter(prefix="/live", tags=["Live Streaming"])

INGEST_BASE   = os.getenv("RTMP_INGEST_URL", "rtmp://ingest.flintx.tv/live")
CDN_BASE      = os.getenv("CDN_URL", "https://cdn.flintx.tv")
WEBHOOK_SECRET = os.getenv("LIVESTREAM_WEBHOOK_SECRET", "change-this-in-production")

# Quality tier thresholds
TIER_THRESHOLDS = {
    QualityTier.active:      {"videos": 1,  "completion": 0.0},
    QualityTier.established: {"videos": 5,  "completion": 0.40},
    QualityTier.premium:     {"videos": 20, "completion": 0.60},
}

# Ad eligible tiers (provisional = no ads — protects advertisers)
AD_ELIGIBLE_TIERS = {
    QualityTier.active:      ["standard"],
    QualityTier.established: ["standard", "finance", "technology", "education"],
    QualityTier.premium:     ["standard", "finance", "technology", "education",
                               "travel", "fitness", "food", "gaming", "music"],
}


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class UpdateChannelRequest(BaseModel):
    channel_title:    Optional[str] = None
    channel_category: Optional[str] = None
    channel_tags:     Optional[list[str]] = None
    is_mature:        Optional[bool] = None
    sub_price_cents:  Optional[int] = None

class StartStreamRequest(BaseModel):
    title:    str
    category: str = "other"
    tags:     list[str] = []
    is_mature: bool = False

class TipRequest(BaseModel):
    amount_cents: int
    message:      str = ""

class ChatRequest(BaseModel):
    content: str

class WebhookRequest(BaseModel):
    stream_key:   str
    event:        str    # start | end | health
    viewers:      int = 0
    bitrate_kbps: int = 0
    fps:          int = 0
    ingest_health: float = 1.0


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _require_creator(user: User, db: Session) -> CreatorProfile:
    if user.role not in ("creator", "both", "admin"):
        raise HTTPException(status_code=403, detail="Creator account required")
    profile = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Creator profile not found")
    return profile


def _get_or_create_channel(user: User, db: Session) -> StreamChannel:
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        key = secrets.token_urlsafe(32)
        channel = StreamChannel(
            creator_id  = user.id,
            stream_key  = key,
            ingest_url  = INGEST_BASE,
            quality_tier = QualityTier.provisional,
        )
        db.add(channel)
        db.commit()
        db.refresh(channel)
    return channel


def _channel_dict(ch: StreamChannel, include_key: bool = False) -> dict:
    d = {
        "id":             ch.id,
        "creator_id":     ch.creator_id,
        "status":         ch.status.value,
        "channel_title":  ch.channel_title,
        "channel_category": ch.channel_category.value if ch.channel_category else "other",
        "is_mature":      ch.is_mature,
        "quality_tier":   ch.quality_tier.value,
        "quality_score":  ch.quality_score,
        "sub_price":      CurrencyService.format(ch.sub_price_cents, "USD"),
        "sub_price_cents": ch.sub_price_cents,
        "total_streams":  ch.total_streams,
        "total_hours":    round(ch.total_hours, 1),
        "peak_viewers":   ch.peak_viewers,
        "total_earnings": CurrencyService.format(ch.total_earnings, "USD"),
    }
    if include_key:
        d["stream_key"] = ch.stream_key
        d["ingest_url"] = ch.ingest_url
        d["rtmp_url"]   = f"{ch.ingest_url}/{ch.stream_key}"
        d["setup_instructions"] = {
            "obs": {
                "server": ch.ingest_url,
                "key":    ch.stream_key,
                "bitrate": "4500–6000 Kbps for 1080p60",
                "encoder": "x264 or NVENC",
            },
            "streamlabs": {
                "server": ch.ingest_url,
                "key":    ch.stream_key,
            },
            "note": "Never share your stream key. Rotate it immediately if compromised.",
        }
    return d


def _stream_dict(stream: LiveStream, currency: str = "USD") -> dict:
    return {
        "id":              stream.id,
        "channel_id":      stream.channel_id,
        "creator_id":      stream.creator_id,
        "title":           stream.title or "Live Stream",
        "category":        stream.category.value if stream.category else "other",
        "thumbnail_url":   stream.thumbnail_url,
        "is_mature":       stream.is_mature,
        "status":          stream.status.value,
        "playback_url":    stream.playback_url,
        "current_viewers": stream.current_viewers,
        "peak_viewers":    stream.peak_viewers,
        "total_views":     stream.total_views,
        "ad_revenue":      CurrencyService.to_display(stream.ad_revenue, currency),
        "creator_earnings": CurrencyService.to_display(stream.creator_earnings, currency),
        "tip_earnings":    CurrencyService.to_display(stream.tip_earnings, currency),
        "sub_earnings":    CurrencyService.to_display(stream.sub_earnings, currency),
        "duration_s":      stream.duration_s,
        "started_at":      stream.started_at.isoformat() if stream.started_at else None,
        "ended_at":        stream.ended_at.isoformat() if stream.ended_at else None,
        "is_collab":       stream.is_collab,
        "vod_video_id":    stream.vod_video_id,
    }


def _update_quality_tier(db: Session, creator_id: str):
    """Recalculate quality score and tier after any content change."""
    profile = db.query(CreatorProfile).filter(
        CreatorProfile.user_id == creator_id
    ).first()
    if not profile:
        return

    score_record = db.query(CreatorQualityScore).filter(
        CreatorQualityScore.creator_id == creator_id
    ).first()
    if not score_record:
        score_record = CreatorQualityScore(creator_id=creator_id)
        db.add(score_record)

    approved = score_record.videos_approved
    completion = profile.video_count and (
        db.query(func.avg(Video.completion_rate)).filter(
            Video.creator_id == profile.id,
            Video.status == VideoStatus.published,
        ).scalar() or 0
    )

    # Determine tier
    tier = QualityTier.provisional
    if approved >= 20 and completion >= 0.60:
        tier = QualityTier.premium
    elif approved >= 5 and completion >= 0.40:
        tier = QualityTier.established
    elif approved >= 1:
        tier = QualityTier.active

    # Composite score (0–100)
    score = min(100, int(
        (approved / 20 * 30) +
        (float(completion) * 25) +
        (max(0, score_record.moderation_record) * 20) +
        (float(score_record.engagement_rate) * 15) +
        (float(score_record.content_originality) * 10)
    ))

    score_record.total_score   = score
    score_record.quality_tier  = tier
    score_record.last_updated  = datetime.utcnow()

    # Update stream channel tier too
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == creator_id
    ).first()
    if channel:
        channel.quality_tier  = tier
        channel.quality_score = score

    db.commit()


# ─────────────────────────────────────────────
# STREAM SETUP
# ─────────────────────────────────────────────

@router.post("/setup", status_code=201)
def setup_stream(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Create or return the creator's stream channel with their RTMP key."""
    _require_creator(user, db)
    channel = _get_or_create_channel(user, db)
    return {
        **_channel_dict(channel, include_key=True),
        "message": "Your stream channel is ready. Use the RTMP URL and stream key in OBS or Streamlabs.",
    }


@router.get("/channel")
def get_my_channel(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="No stream channel. POST /api/live/setup first.")
    return _channel_dict(channel, include_key=True)


@router.patch("/channel")
def update_channel(
    req: UpdateChannelRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="No stream channel found")

    if req.channel_title is not None:
        channel.channel_title = req.channel_title
    if req.channel_category:
        channel.channel_category = StreamCategory(req.channel_category)
    if req.channel_tags is not None:
        channel.channel_tags = json.dumps(req.channel_tags)
    if req.is_mature is not None:
        channel.is_mature = req.is_mature
    if req.sub_price_cents is not None:
        if req.sub_price_cents < 199:
            raise HTTPException(status_code=422, detail="Minimum sub price is $1.99")
        channel.sub_price_cents = req.sub_price_cents

    db.commit()
    return _channel_dict(channel, include_key=True)


@router.post("/channel/rotate-key")
def rotate_stream_key(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Generate a new stream key. Old key stops working immediately."""
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="No stream channel found")
    if channel.status == StreamStatus.live:
        raise HTTPException(status_code=400, detail="Cannot rotate key while live. End your stream first.")

    channel.stream_key = secrets.token_urlsafe(32)
    db.commit()

    return {
        "new_key":   channel.stream_key,
        "rtmp_url":  f"{channel.ingest_url}/{channel.stream_key}",
        "message":   "Old key is now invalid. Update OBS/Streamlabs with the new key.",
    }


# ─────────────────────────────────────────────
# GO LIVE (creator triggers from dashboard)
# ─────────────────────────────────────────────

@router.post("/go-live")
def go_live(
    req: StartStreamRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Creator clicks 'Go Live' in the FlintX dashboard.
    Creates a LiveStream record. Actual broadcast starts when
    OBS/Streamlabs connects to the RTMP endpoint.
    """
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="No stream channel. Run setup first.")
    if channel.status == StreamStatus.live:
        raise HTTPException(status_code=400, detail="Already live")

    stream = LiveStream(
        channel_id  = channel.id,
        creator_id  = user.id,
        title       = req.title,
        category    = StreamCategory(req.category),
        tags        = json.dumps(req.tags),
        is_mature   = req.is_mature,
        status      = StreamStatus.live,
        playback_url = f"{CDN_BASE}/live/{channel.id}/index.m3u8",
        hls_1080_url = f"{CDN_BASE}/live/{channel.id}/1080p/index.m3u8",
        hls_720_url  = f"{CDN_BASE}/live/{channel.id}/720p/index.m3u8",
        hls_360_url  = f"{CDN_BASE}/live/{channel.id}/360p/index.m3u8",
        started_at  = datetime.utcnow(),
    )
    db.add(stream)

    channel.status = StreamStatus.live
    channel.current_stream_id = stream.id
    channel.total_streams += 1

    db.commit()
    db.refresh(stream)

    return {
        **_stream_dict(stream),
        "stream_key": channel.stream_key,
        "rtmp_url":   f"{channel.ingest_url}/{channel.stream_key}",
        "message":    "Stream created. Connect OBS/Streamlabs to go live.",
    }


@router.post("/end-stream")
def end_stream(
    background_tasks: BackgroundTasks,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Creator ends their stream."""
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id,
        StreamChannel.status == StreamStatus.live,
    ).first()
    if not channel:
        raise HTTPException(status_code=400, detail="No active stream")

    # Try current_stream_id first, fall back to most recent live stream for this channel
    stream = None
    if channel.current_stream_id:
        stream = db.query(LiveStream).filter(LiveStream.id == channel.current_stream_id).first()
    if not stream:
        stream = db.query(LiveStream).filter(
            LiveStream.channel_id == channel.id,
            LiveStream.status == StreamStatus.live,
        ).order_by(desc(LiveStream.started_at)).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    stream.status   = StreamStatus.ending
    stream.ended_at = datetime.utcnow()
    stream.duration_s = int((stream.ended_at - stream.started_at).total_seconds())

    channel.status = StreamStatus.idle
    channel.current_stream_id = None
    channel.total_hours += stream.duration_s / 3600

    if stream.peak_viewers > channel.peak_viewers:
        channel.peak_viewers = stream.peak_viewers

    db.commit()

    # Save as VOD in background
    background_tasks.add_task(_save_vod, stream.id)

    return {
        "ended": True,
        "duration_s":      stream.duration_s,
        "peak_viewers":    stream.peak_viewers,
        "total_earnings":  CurrencyService.format(
            stream.creator_earnings + stream.tip_earnings + stream.sub_earnings, "USD"
        ),
        "message": "Stream ended. VOD will be ready within 15 minutes.",
    }


def _save_vod(stream_id: str):
    """Background task: convert ended stream to a VOD video record."""
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
        if not stream:
            return

        creator = db.query(User).filter(User.id == stream.creator_id).first()
        profile = db.query(CreatorProfile).filter(
            CreatorProfile.user_id == stream.creator_id
        ).first()
        if not creator or not profile:
            return

        # Create a Video record for the VOD
        from ..database.models import VideoType, ModerationStatus, SafetyLevel
        vod = Video(
            creator_id      = profile.id,
            title           = f"[VOD] {stream.title or 'Live Stream'}",
            description     = f"Recording of live stream from {stream.started_at.strftime('%B %d, %Y')}",
            category        = stream.category.value if stream.category else "other",
            video_type      = VideoType.long,
            status          = VideoStatus.published,
            mod_status      = ModerationStatus.auto_approved,
            safety_level    = SafetyLevel.standard,
            hls_url         = stream.playback_url,
            duration_s      = stream.duration_s,
            published_at    = stream.ended_at,
            creator_earnings = stream.creator_earnings,
        )
        db.add(vod)
        db.flush()

        stream.vod_video_id = vod.id
        stream.status       = StreamStatus.vod_ready
        db.commit()

    except Exception as e:
        print(f"[VOD SAVE ERROR] {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
# INGEST SERVER WEBHOOKS
# ─────────────────────────────────────────────

@router.post("/webhook/start")
def webhook_stream_start(req: WebhookRequest, db: Session = Depends(get_db)):
    """Called by nginx-rtmp/AWS IVS when a creator's encoder connects."""
    _verify_webhook(req)
    channel = db.query(StreamChannel).filter(
        StreamChannel.stream_key == req.stream_key
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Unknown stream key")

    channel.status = StreamStatus.live
    db.commit()
    return {"allowed": True}


@router.post("/webhook/end")
def webhook_stream_end(req: WebhookRequest, db: Session = Depends(get_db)):
    """Called by ingest server when encoder disconnects."""
    channel = db.query(StreamChannel).filter(
        StreamChannel.stream_key == req.stream_key
    ).first()
    if channel:
        channel.status = StreamStatus.idle
        db.commit()
    return {"ok": True}


@router.post("/webhook/health")
def webhook_health(req: WebhookRequest, db: Session = Depends(get_db)):
    """Periodic health update — viewer count, bitrate, fps."""
    stream = db.query(LiveStream).join(StreamChannel).filter(
        StreamChannel.stream_key == req.stream_key,
        LiveStream.status == StreamStatus.live,
    ).first()
    if stream:
        stream.current_viewers = req.viewers
        stream.bitrate_kbps    = req.bitrate_kbps
        stream.fps             = req.fps
        stream.ingest_health   = req.ingest_health
        if req.viewers > stream.peak_viewers:
            stream.peak_viewers = req.viewers
        db.commit()
    return {"ok": True}


def _verify_webhook(req: WebhookRequest):
    """Verify webhook is from our ingest server. Skip in dev."""
    if os.getenv("ENVIRONMENT") == "development":
        return
    # In production: verify HMAC signature in request headers


# ─────────────────────────────────────────────
# LIVE DIRECTORY
# ─────────────────────────────────────────────

@router.get("/directory")
def get_live_directory(
    category: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """All currently live streams, sorted by viewer count."""
    limit  = min(limit, 50)
    offset = (page - 1) * limit

    q = db.query(LiveStream).filter(LiveStream.status == StreamStatus.live)
    if category and category != "all":
        q = q.filter(LiveStream.category == StreamCategory(category))

    streams = q.order_by(desc(LiveStream.current_viewers)).offset(offset).limit(limit).all()
    total   = q.count()

    return {
        "streams": [_stream_dict(s) for s in streams],
        "total":   total,
        "page":    page,
        "pages":   (total // limit) + 1,
    }


@router.get("/featured")
def get_featured(db: Session = Depends(get_db)):
    """Top 6 live streams by viewer count for homepage."""
    streams = db.query(LiveStream).filter(
        LiveStream.status == StreamStatus.live
    ).order_by(desc(LiveStream.current_viewers)).limit(6).all()
    return {"streams": [_stream_dict(s) for s in streams]}


@router.get("/channel/{handle}")
def get_channel_by_handle(handle: str, db: Session = Depends(get_db)):
    """Public stream channel page."""
    profile = db.query(CreatorProfile).filter(
        CreatorProfile.channel_handle == handle
    ).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == profile.user_id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Stream channel not set up")

    # Get current live stream if any
    live = None
    if channel.status == StreamStatus.live and channel.current_stream_id:
        live = db.query(LiveStream).filter(
            LiveStream.id == channel.current_stream_id
        ).first()

    # Recent VODs
    vods = db.query(LiveStream).filter(
        LiveStream.channel_id == channel.id,
        LiveStream.status     == StreamStatus.vod_ready,
    ).order_by(desc(LiveStream.ended_at)).limit(6).all()

    return {
        "channel":    _channel_dict(channel),
        "is_live":    channel.status == StreamStatus.live,
        "live_stream": _stream_dict(live) if live else None,
        "recent_vods": [_stream_dict(v) for v in vods],
    }


@router.get("/{stream_id}")
def get_stream(stream_id: str, db: Session = Depends(get_db)):
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    return _stream_dict(stream)


# ─────────────────────────────────────────────
# VIEWER ACTIONS
# ─────────────────────────────────────────────

@router.post("/{stream_id}/view")
def join_stream(
    stream_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    stream = db.query(LiveStream).filter(
        LiveStream.id == stream_id,
        LiveStream.status == StreamStatus.live,
    ).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found or not live")
    stream.current_viewers += 1
    stream.total_views     += 1
    if stream.current_viewers > stream.peak_viewers:
        stream.peak_viewers = stream.current_viewers
    db.commit()
    return {"viewers": stream.current_viewers}


@router.post("/{stream_id}/leave")
def leave_stream(stream_id: str, db: Session = Depends(get_db)):
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if stream and stream.current_viewers > 0:
        stream.current_viewers -= 1
        db.commit()
    return {"ok": True}


@router.post("/{stream_id}/tip")
def send_tip(
    stream_id: str,
    req: TipRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Viewer tips a creator during their live stream."""
    if req.amount_cents < 50:
        raise HTTPException(status_code=422, detail="Minimum tip is $0.50")
    if user.wallet_balance < req.amount_cents:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    stream = db.query(LiveStream).filter(
        LiveStream.id == stream_id,
        LiveStream.status == StreamStatus.live,
    ).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found or not live")

    # Deduct from tipper
    user.wallet_balance -= req.amount_cents

    # Credit creator (100% of tips — no platform cut on tips)
    creator = db.query(User).filter(User.id == stream.creator_id).first()
    if creator:
        creator.wallet_balance += req.amount_cents
        stream.tip_earnings    += req.amount_cents

    # Record chat message for the tip
    tip_msg = ChatMessage(
        stream_id  = stream_id,
        user_id    = user.id,
        type       = ChatMsgType.tip,
        content    = req.message or f"Tipped {CurrencyService.format(req.amount_cents, 'USD')}!",
        amount     = req.amount_cents,
        pinned     = req.amount_cents >= 1000,  # pin tips of $10+
    )
    db.add(tip_msg)

    db.add(Transaction(
        user_id     = user.id,
        type        = TxnType.tip,
        amount      = -req.amount_cents,
        balance_after = user.wallet_balance,
        description = f"Tip to {stream.title}",
        video_id    = stream_id,
    ))

    db.commit()
    return {
        "tipped":  True,
        "amount":  CurrencyService.format(req.amount_cents, "USD"),
        "pinned":  req.amount_cents >= 1000,
    }


# ─────────────────────────────────────────────
# LIVE CHAT
# ─────────────────────────────────────────────

@router.get("/{stream_id}/chat")
def get_chat(
    stream_id: str,
    after:    Optional[str] = None,   # message ID — get messages after this
    limit:    int = 50,
    db: Session = Depends(get_db),
):
    """
    Poll-based chat fallback. In production use WebSocket for real-time.
    Frontend polls this every 2 seconds.
    """
    limit = min(limit, 100)
    q = db.query(ChatMessage).filter(
        ChatMessage.stream_id == stream_id,
        ChatMessage.deleted   == False,
    )
    if after:
        anchor = db.query(ChatMessage).filter(ChatMessage.id == after).first()
        if anchor:
            q = q.filter(ChatMessage.created_at > anchor.created_at)

    messages = q.order_by(ChatMessage.created_at).limit(limit).all()

    return {
        "messages": [
            {
                "id":        m.id,
                "user_id":   m.user_id,
                "type":      m.type.value,
                "content":   m.content,
                "amount":    m.amount,
                "pinned":    m.pinned,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]
    }


@router.post("/{stream_id}/chat")
def send_chat(
    stream_id: str,
    req: ChatRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    if not req.content.strip():
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    if len(req.content) > 500:
        raise HTTPException(status_code=422, detail="Message too long (max 500 chars)")

    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream or stream.status != StreamStatus.live:
        raise HTTPException(status_code=400, detail="Stream is not live")

    msg = ChatMessage(
        stream_id = stream_id,
        user_id   = user.id,
        type      = ChatMsgType.message,
        content   = req.content.strip(),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    return {
        "id":        msg.id,
        "content":   msg.content,
        "created_at": msg.created_at.isoformat(),
    }


@router.delete("/{stream_id}/chat/{msg_id}")
def delete_chat_message(
    stream_id: str,
    msg_id:    str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Creator or moderator deletes a chat message."""
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Only creator or admin can delete
    if stream.creator_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorised to delete messages")

    msg = db.query(ChatMessage).filter(
        ChatMessage.id == msg_id, ChatMessage.stream_id == stream_id
    ).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg.deleted = True
    db.commit()
    return {"deleted": True}


# ─────────────────────────────────────────────
# SUBSCRIPTIONS
# ─────────────────────────────────────────────

@router.post("/{stream_id}/sub")
def subscribe_to_channel(
    stream_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Subscribe to a creator's channel during their live stream."""
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    channel = db.query(StreamChannel).filter(
        StreamChannel.id == stream.channel_id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Check wallet balance
    if user.wallet_balance < channel.sub_price_cents:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Sub costs {CurrencyService.format(channel.sub_price_cents, 'USD')}"
        )

    creator_cut = int(channel.sub_price_cents * 0.80)

    # Deduct from subscriber
    user.wallet_balance -= channel.sub_price_cents

    # Credit creator
    creator = db.query(User).filter(User.id == channel.creator_id).first()
    if creator:
        creator.wallet_balance += creator_cut
        stream.sub_earnings    += creator_cut

    # Create sub record
    sub = StreamSub(
        channel_id    = channel.id,
        subscriber_id = user.id,
        price_cents   = channel.sub_price_cents,
        creator_cut   = creator_cut,
        expires_at    = datetime.utcnow() + timedelta(days=31),
    )
    db.add(sub)

    # Sub notification in chat
    db.add(ChatMessage(
        stream_id = stream_id,
        user_id   = user.id,
        type      = ChatMsgType.sub,
        content   = f"Just subscribed! 🔥",
    ))

    db.add(Transaction(
        user_id     = user.id,
        type        = TxnType.pass_sub,
        amount      = -channel.sub_price_cents,
        balance_after = user.wallet_balance,
        description = f"Stream subscription",
    ))

    db.commit()
    return {
        "subscribed":   True,
        "amount":       CurrencyService.format(channel.sub_price_cents, "USD"),
        "expires_at":   sub.expires_at.isoformat(),
        "message":      "Subscribed! Your sub appears in chat.",
    }


# ─────────────────────────────────────────────
# VODs
# ─────────────────────────────────────────────

@router.get("/vods/mine")
def get_my_vods(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    channel = db.query(StreamChannel).filter(
        StreamChannel.creator_id == user.id
    ).first()
    if not channel:
        return {"vods": []}

    vods = db.query(LiveStream).filter(
        LiveStream.channel_id == channel.id,
        LiveStream.status.in_([StreamStatus.vod_ready, StreamStatus.ending]),
    ).order_by(desc(LiveStream.started_at)).limit(20).all()

    return {"vods": [_stream_dict(v, currency) for v in vods]}


# ─────────────────────────────────────────────
# CREATOR QUALITY SCORE
# ─────────────────────────────────────────────

@router.get("/quality/mine")
def get_quality_score(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    score = db.query(CreatorQualityScore).filter(
        CreatorQualityScore.creator_id == user.id
    ).first()

    if not score:
        return {
            "tier":    "provisional",
            "score":   0,
            "message": "Publish your first video to start building your Quality Score.",
            "ad_eligibility": "none",
        }

    # What's needed for next tier
    next_tier_info = _next_tier_info(score)

    return {
        "tier":            score.quality_tier.value,
        "score":           round(score.total_score, 1),
        "videos_approved": score.videos_approved,
        "videos_flagged":  score.videos_flagged,
        "moderation_health": f"{round(score.moderation_record * 100)}%",
        "completion_rate": f"{round(score.completion_score * 100, 1)}%",
        "ad_eligibility":  AD_ELIGIBLE_TIERS.get(score.quality_tier, []),
        "next_tier":       next_tier_info,
        "vs_youtube": {
            "youtube":  "Requires 1,000 subscribers AND 4,000 watch hours before earning $1",
            "flintx":   "Earn from video 1. Quality Score gates premium campaigns, not basic earnings.",
            "advantage": "New FlintX creators earn immediately. YouTube creators wait months or years.",
        },
    }


def _next_tier_info(score: CreatorQualityScore) -> dict:
    tier = score.quality_tier

    if tier == QualityTier.premium:
        return {"achieved": True, "message": "Maximum tier — eligible for all campaigns including exclusivity deals."}

    if tier == QualityTier.established:
        needed_videos = max(0, 20 - score.videos_approved)
        return {
            "tier": "premium",
            "videos_needed": needed_videos,
            "completion_target": "60% average completion rate",
            "unlocks": "Category exclusivity ad deals — highest CPM campaigns",
        }

    if tier == QualityTier.active:
        needed_videos = max(0, 5 - score.videos_approved)
        return {
            "tier": "established",
            "videos_needed": needed_videos,
            "completion_target": "40% average completion rate",
            "unlocks": "Finance, Technology, Education campaigns — premium CPM niches",
        }

    return {
        "tier": "active",
        "videos_needed": 1,
        "completion_target": "any",
        "unlocks": "Standard ad campaigns — start earning immediately",
    }


# ─────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────

@router.get("/admin/live")
def admin_live_streams(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    streams = db.query(LiveStream).filter(
        LiveStream.status == StreamStatus.live
    ).order_by(desc(LiveStream.current_viewers)).all()

    return {
        "count":   len(streams),
        "viewers": sum(s.current_viewers for s in streams),
        "streams": [_stream_dict(s) for s in streams],
    }


@router.post("/admin/suspend/{stream_id}")
def admin_suspend_stream(
    stream_id: str,
    reason: str = "Violated community guidelines",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    stream = db.query(LiveStream).filter(LiveStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    stream.status = StreamStatus.suspended
    stream.ended_at = datetime.utcnow()

    channel = db.query(StreamChannel).filter(
        StreamChannel.id == stream.channel_id
    ).first()
    if channel:
        channel.status = StreamStatus.suspended

    db.commit()
    return {"suspended": True, "reason": reason}
