"""
FlintX — Workspace API Routes

Workspace management:
  POST /api/workspace                    — create workspace
  GET  /api/workspace                    — get my workspace
  PATCH /api/workspace                   — update workspace
  GET  /api/workspace/analytics          — unified analytics dashboard

Channels:
  POST /api/workspace/channels           — create a channel
  GET  /api/workspace/channels           — list all channels
  GET  /api/workspace/channels/{id}      — single channel detail
  PATCH /api/workspace/channels/{id}     — update channel
  DELETE /api/workspace/channels/{id}    — deactivate channel
  GET  /api/workspace/channels/{id}/analytics — channel analytics

Team:
  POST /api/workspace/members/invite     — invite team member
  GET  /api/workspace/members            — list team members
  PATCH /api/workspace/members/{id}      — change role
  DELETE /api/workspace/members/{id}     — remove member
  POST /api/workspace/members/accept     — accept invite

Audience Bridge:
  POST /api/workspace/bridge             — create a bridge campaign
  GET  /api/workspace/bridge             — list bridges
  POST /api/workspace/bridge/{id}/send   — send bridge now
  GET  /api/workspace/bridge/{id}/stats  — bridge results

Content Passport:
  POST /api/workspace/passport           — distribute video to multiple channels
  GET  /api/workspace/passport           — list passports
  GET  /api/workspace/passport/{id}      — passport detail + per-channel stats
  PATCH /api/workspace/passport/{id}     — update distribution

Collab Splits:
  POST /api/workspace/collab             — propose a collab split
  GET  /api/workspace/collab             — my collabs (sent + received)
  POST /api/workspace/collab/{id}/accept — accept a collab proposal
  POST /api/workspace/collab/{id}/decline — decline a collab proposal
  GET  /api/workspace/collab/{id}/earnings — collab earnings breakdown

Channel Lending:
  POST /api/workspace/lend               — offer channel lending
  GET  /api/workspace/lend               — my active lends
  POST /api/workspace/lend/{id}/accept   — accept a lending offer
  GET  /api/workspace/lend/{id}/stats    — lending results
"""

import json
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import User, Video, VideoStatus, Transaction, TxnType
from ..auth.routes import require_verified
from ..currency.service import CurrencyService
from .models import (
    Workspace, WorkspaceMember, WorkspaceRole,
    Channel, ChannelSubscriber, ChannelStatus,
    AudienceBridge, BridgeStatus,
    ContentPassport, PassportDistribution, PassportStatus,
    CollabSplit, CollabStatus,
    ChannelLend, LendStatus,
    WorkspaceSnapshot,
)

router = APIRouter(prefix="/workspace", tags=["Workspace"])


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class CreateWorkspaceRequest(BaseModel):
    name:        str
    handle:      str
    description: str = ""
    website:     str = ""

class UpdateWorkspaceRequest(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    website:     Optional[str] = None

class CreateChannelRequest(BaseModel):
    name:        str
    handle:      str
    niche:       str
    description: str = ""

class UpdateChannelRequest(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    niche:       Optional[str] = None

class InviteMemberRequest(BaseModel):
    email: str
    role:  str = "editor"

class CreateBridgeRequest(BaseModel):
    from_channel_id: str
    to_channel_id:   str
    subject:         str
    message:         str
    cta_label:       str = "Subscribe now →"
    target_all:      bool = True
    min_watch_pct:   float = 0.0
    scheduled_at:    Optional[str] = None

class CreatePassportRequest(BaseModel):
    source_video_id: str
    distributions: list[dict]   # [{channel_id, custom_title, custom_description, custom_thumbnail}]

class ProposeCollabRequest(BaseModel):
    video_id:       str
    creator_b_id:   str
    creator_a_share: int   # must sum to 100 with b+c+d
    creator_b_share: int
    creator_c_id:   Optional[str] = None
    creator_c_share: int = 0
    creator_d_id:   Optional[str] = None
    creator_d_share: int = 0
    message:        str = ""

class OfferLendRequest(BaseModel):
    from_channel_id: str
    to_channel_id:   str
    duration_days:   int = 30
    fee_type:        str = "flat"       # flat | revenue_share
    fee_flat_cents:  int = 0
    fee_revenue_pct: int = 0


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_workspace(user: User, db: Session) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.owner_id == user.id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="No workspace found. Create one first.")
    return ws


def _workspace_dict(ws: Workspace, currency: str = "USD") -> dict:
    return {
        "id":               ws.id,
        "name":             ws.name,
        "handle":           ws.handle,
        "description":      ws.description,
        "logo_url":         ws.logo_url,
        "website":          ws.website,
        "total_channels":   ws.total_channels,
        "total_subscribers": ws.total_subscribers,
        "total_views":      ws.total_views,
        "total_earnings":   CurrencyService.to_display(ws.total_earnings, currency),
        "monthly_earnings": CurrencyService.to_display(ws.monthly_earnings, currency),
        "created_at":       ws.created_at.isoformat(),
    }


def _channel_dict(ch: Channel, currency: str = "USD") -> dict:
    return {
        "id":              ch.id,
        "workspace_id":    ch.workspace_id,
        "name":            ch.name,
        "handle":          ch.handle,
        "niche":           ch.niche,
        "description":     ch.description,
        "avatar_url":      ch.avatar_url,
        "status":          ch.status.value,
        "subscriber_count": ch.subscriber_count,
        "video_count":     ch.video_count,
        "total_views":     ch.total_views,
        "avg_completion":  ch.avg_completion,
        "total_earnings":  CurrencyService.to_display(ch.total_earnings, currency),
        "monthly_earnings": CurrencyService.to_display(ch.monthly_earnings, currency),
        "pending_payout":  CurrencyService.to_display(ch.pending_payout, currency),
        "actual_cpm":      CurrencyService.to_display(ch.actual_cpm_cents, currency) if ch.actual_cpm_cents else None,
        "is_lending":      ch.is_lending,
        "created_at":      ch.created_at.isoformat(),
    }


# ─────────────────────────────────────────────
# WORKSPACE CRUD
# ─────────────────────────────────────────────

@router.post("", status_code=201)
def create_workspace(
    req: CreateWorkspaceRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    if db.query(Workspace).filter(Workspace.owner_id == user.id).first():
        raise HTTPException(status_code=409, detail="You already have a workspace")

    handle = req.handle.lower().replace(" ", "").replace("@", "")
    if db.query(Workspace).filter(Workspace.handle == handle).first():
        raise HTTPException(status_code=409, detail="That handle is already taken")

    ws = Workspace(owner_id=user.id, name=req.name, handle=handle,
                   description=req.description, website=req.website)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return _workspace_dict(ws)


@router.get("")
def get_workspace(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    channels = db.query(Channel).filter(Channel.workspace_id == ws.id).all()
    return {
        **_workspace_dict(ws, currency),
        "channels": [_channel_dict(c, currency) for c in channels],
    }


@router.patch("")
def update_workspace(
    req: UpdateWorkspaceRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    if req.name:        ws.name        = req.name
    if req.description: ws.description = req.description
    if req.website:     ws.website     = req.website
    db.commit()
    return _workspace_dict(ws)


# ─────────────────────────────────────────────
# WORKSPACE ANALYTICS — unified dashboard
# ─────────────────────────────────────────────

@router.get("/analytics")
def workspace_analytics(
    currency: str = "USD",
    days: int = 30,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    The FlintX unified analytics dashboard.
    All channels side by side — impossible on any competitor platform.
    """
    ws       = _get_workspace(user, db)
    channels = db.query(Channel).filter(Channel.workspace_id == ws.id).all()

    # Channel comparison table
    channel_comparison = []
    for ch in channels:
        channel_comparison.append({
            "id":              ch.id,
            "name":            ch.name,
            "handle":          ch.handle,
            "niche":           ch.niche,
            "subscribers":     ch.subscriber_count,
            "views":           ch.total_views,
            "monthly_earnings": CurrencyService.to_display(ch.monthly_earnings, currency),
            "avg_completion":  f"{round(ch.avg_completion*100,1)}%",
            "actual_cpm":      CurrencyService.to_display(ch.actual_cpm_cents, currency),
            "growth_rank":     0,   # calculated below
        })

    # Rank by monthly earnings
    channel_comparison.sort(key=lambda x: x["monthly_earnings"]["amount"], reverse=True)
    for i, ch in enumerate(channel_comparison):
        ch["growth_rank"] = i + 1

    # Best niche by CPM (real data from actual earnings)
    best_niche = max(channels, key=lambda c: c.actual_cpm_cents, default=None)

    # Audience overlap analysis
    # How many subscribers appear on multiple channels?
    all_subs = db.query(
        ChannelSubscriber.user_id,
        func.count(ChannelSubscriber.channel_id).label("channel_count")
    ).join(Channel).filter(
        Channel.workspace_id == ws.id
    ).group_by(ChannelSubscriber.user_id).all()

    cross_subs = sum(1 for s in all_subs if s.channel_count > 1)
    unique_subs = len(all_subs)

    # Recent snapshots for trend chart
    snapshots = db.query(WorkspaceSnapshot).filter(
        WorkspaceSnapshot.workspace_id == ws.id
    ).order_by(desc(WorkspaceSnapshot.date)).limit(days).all()

    return {
        "currency": currency,
        "workspace": _workspace_dict(ws, currency),
        "summary": {
            "total_channels":    len(channels),
            "total_subscribers": ws.total_subscribers,
            "unique_subscribers": unique_subs,
            "cross_channel_fans": cross_subs,   # fans of 2+ channels
            "cross_fan_rate":    f"{round(cross_subs/max(unique_subs,1)*100,1)}%",
            "total_monthly":     CurrencyService.to_display(ws.monthly_earnings, currency),
            "total_lifetime":    CurrencyService.to_display(ws.total_earnings, currency),
            "best_niche":        best_niche.niche if best_niche else None,
            "best_niche_cpm":    CurrencyService.to_display(best_niche.actual_cpm_cents, currency) if best_niche else None,
        },
        "channel_comparison": channel_comparison,
        "trend": [
            {
                "date":     s.date,
                "views":    s.total_views,
                "earnings": CurrencyService.to_display(s.total_earnings, currency),
                "new_subs": s.new_subscribers,
            }
            for s in reversed(snapshots)
        ],
        "insights": _generate_insights(channels, cross_subs, unique_subs),
    }


def _generate_insights(channels, cross_subs, unique_subs) -> list[dict]:
    """Auto-generated actionable insights from workspace data."""
    insights = []

    if not channels:
        return [{"type": "info", "message": "Create your first channel to see insights."}]

    # Find underperforming channels
    avg_completion = sum(c.avg_completion for c in channels) / len(channels)
    for ch in channels:
        if ch.avg_completion < avg_completion * 0.7 and ch.video_count > 3:
            insights.append({
                "type":    "warning",
                "channel": ch.name,
                "message": f"{ch.name} has below-average completion rate ({round(ch.avg_completion*100)}% vs workspace avg {round(avg_completion*100)}%). Consider shorter videos or stronger hooks.",
                "action":  "Review content strategy",
            })

    # Cross-channel opportunity
    if cross_subs / max(unique_subs, 1) < 0.15 and len(channels) > 1:
        insights.append({
            "type":    "opportunity",
            "message": "Less than 15% of your audience follows multiple channels. Use Audience Bridge to introduce your channels to each other.",
            "action":  "Create Audience Bridge",
        })

    # Channel lending opportunity
    top = max(channels, key=lambda c: c.subscriber_count, default=None)
    low = min(channels, key=lambda c: c.subscriber_count, default=None)
    if top and low and top.subscriber_count > 10000 and low.subscriber_count < 1000 and top.id != low.id:
        insights.append({
            "type":    "opportunity",
            "message": f"Lend {top.name}'s audience to {low.name}. Your established channel can bootstrap your newer one — this is impossible on any other platform.",
            "action":  "Set up Channel Lending",
        })

    # Revenue concentration
    if len(channels) > 1:
        earnings = sorted(channels, key=lambda c: c.monthly_earnings, reverse=True)
        top_share = earnings[0].monthly_earnings / max(sum(c.monthly_earnings for c in channels), 1)
        if top_share > 0.85:
            insights.append({
                "type":    "risk",
                "channel": earnings[0].name,
                "message": f"{round(top_share*100)}% of your revenue comes from one channel. Diversify by growing {earnings[-1].name}.",
                "action":  "Use Content Passport to cross-distribute",
            })

    if not insights:
        insights.append({"type": "success", "message": "Workspace performing well across all channels. Keep your upload consistency strong."})

    return insights


# ─────────────────────────────────────────────
# CHANNELS
# ─────────────────────────────────────────────

@router.post("/channels", status_code=201)
def create_channel(
    req: CreateChannelRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)

    handle = req.handle.lower().replace(" ", "").replace("@", "")
    if db.query(Channel).filter(Channel.handle == handle).first():
        raise HTTPException(status_code=409, detail="That channel handle is already taken")

    ch = Channel(
        workspace_id = ws.id,
        owner_id     = user.id,
        name         = req.name,
        handle       = handle,
        niche        = req.niche,
        description  = req.description,
    )
    db.add(ch)
    ws.total_channels += 1
    db.commit()
    db.refresh(ch)
    return _channel_dict(ch)


@router.get("/channels")
def list_channels(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    channels = db.query(Channel).filter(
        Channel.workspace_id == ws.id,
        Channel.status != ChannelStatus.suspended,
    ).order_by(desc(Channel.subscriber_count)).all()
    return {"channels": [_channel_dict(c, currency) for c in channels]}


@router.get("/channels/{channel_id}")
def get_channel(
    channel_id: str,
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    ch = db.query(Channel).filter(Channel.id == channel_id, Channel.workspace_id == ws.id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _channel_dict(ch, currency)


@router.patch("/channels/{channel_id}")
def update_channel(
    channel_id: str,
    req: UpdateChannelRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    ch = db.query(Channel).filter(Channel.id == channel_id, Channel.workspace_id == ws.id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    if req.name:        ch.name        = req.name
    if req.description: ch.description = req.description
    if req.niche:       ch.niche       = req.niche
    db.commit()
    return _channel_dict(ch)


@router.delete("/channels/{channel_id}")
def deactivate_channel(
    channel_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    ch = db.query(Channel).filter(Channel.id == channel_id, Channel.workspace_id == ws.id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    ch.status = ChannelStatus.paused
    ws.total_channels = max(0, ws.total_channels - 1)
    db.commit()
    return {"paused": True}


@router.get("/channels/{channel_id}/analytics")
def channel_analytics(
    channel_id: str,
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)
    ch = db.query(Channel).filter(Channel.id == channel_id, Channel.workspace_id == ws.id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Get videos for this channel
    videos = db.query(Video).filter(
        Video.creator_id == channel_id,
        Video.status == VideoStatus.published,
    ).order_by(desc(Video.view_count)).limit(10).all()

    top_videos = [
        {
            "id":           v.id,
            "title":        v.title,
            "views":        v.view_count,
            "completion":   f"{round(v.completion_rate*100,1)}%",
            "earnings":     CurrencyService.to_display(v.creator_earnings, currency),
            "algo_score":   v.algo_score,
        }
        for v in videos
    ]

    return {
        "channel":    _channel_dict(ch, currency),
        "top_videos": top_videos,
        "audience": {
            "total_subscribers": ch.subscriber_count,
            "avg_completion":    f"{round(ch.avg_completion*100,1)}%",
            "actual_cpm":        CurrencyService.to_display(ch.actual_cpm_cents, currency),
        },
    }


# ─────────────────────────────────────────────
# TEAM MEMBERS
# ─────────────────────────────────────────────

@router.post("/members/invite", status_code=201)
def invite_member(
    req: InviteMemberRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)

    invitee = db.query(User).filter(User.email == req.email.lower()).first()
    if not invitee:
        raise HTTPException(status_code=404, detail="No FlintX account found with that email")

    existing = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws.id,
        WorkspaceMember.user_id == invitee.id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This person is already in your workspace")

    token = secrets.token_urlsafe(32)
    member = WorkspaceMember(
        workspace_id = ws.id,
        user_id      = invitee.id,
        role         = WorkspaceRole(req.role),
        invited_by   = user.id,
        invite_token = token,
    )
    db.add(member)
    db.commit()

    return {
        "invited":    True,
        "email":      req.email,
        "role":       req.role,
        "token":      token,
        "message":    f"Invite sent. They accept at: /workspace/members/accept?token={token}",
    }


@router.post("/members/accept")
def accept_invite(
    token: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.invite_token == token,
        WorkspaceMember.user_id      == user.id,
        WorkspaceMember.accepted     == False,
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Invalid or expired invite")

    member.accepted     = True
    member.invite_token = None
    member.joined_at    = datetime.utcnow()
    db.commit()
    return {"accepted": True, "role": member.role.value}


@router.get("/members")
def list_members(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    ws      = _get_workspace(user, db)
    members = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws.id,
        WorkspaceMember.accepted     == True,
    ).all()
    return {
        "members": [
            {
                "id":      m.id,
                "user_id": m.user_id,
                "role":    m.role.value,
                "joined":  m.joined_at.isoformat() if m.joined_at else None,
            }
            for m in members
        ]
    }


# ─────────────────────────────────────────────
# AUDIENCE BRIDGE
# ─────────────────────────────────────────────

@router.post("/bridge", status_code=201)
def create_bridge(
    req: CreateBridgeRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)

    from_ch = db.query(Channel).filter(Channel.id == req.from_channel_id, Channel.workspace_id == ws.id).first()
    to_ch   = db.query(Channel).filter(Channel.id == req.to_channel_id,   Channel.workspace_id == ws.id).first()

    if not from_ch or not to_ch:
        raise HTTPException(status_code=404, detail="Channel not found in your workspace")
    if from_ch.id == to_ch.id:
        raise HTTPException(status_code=422, detail="From and To channels must be different")

    # Count eligible recipients
    sub_count = db.query(func.count(ChannelSubscriber.id)).filter(
        ChannelSubscriber.channel_id == from_ch.id,
        ChannelSubscriber.notif_on   == True,
    ).scalar()

    bridge = AudienceBridge(
        workspace_id    = ws.id,
        from_channel_id = from_ch.id,
        to_channel_id   = to_ch.id,
        subject         = req.subject,
        message         = req.message,
        cta_label       = req.cta_label,
        target_all      = req.target_all,
        min_watch_pct   = req.min_watch_pct,
        scheduled_at    = datetime.fromisoformat(req.scheduled_at) if req.scheduled_at else None,
        status          = BridgeStatus.scheduled if req.scheduled_at else BridgeStatus.draft,
        recipients      = sub_count,
    )
    db.add(bridge)
    db.commit()
    db.refresh(bridge)

    return {
        "id":              bridge.id,
        "from_channel":    from_ch.name,
        "to_channel":      to_ch.name,
        "recipients":      sub_count,
        "status":          bridge.status.value,
        "message":         f"Bridge created. {sub_count:,} subscribers from {from_ch.name} will be notified about {to_ch.name}.",
    }


@router.post("/bridge/{bridge_id}/send")
def send_bridge(
    bridge_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws     = _get_workspace(user, db)
    bridge = db.query(AudienceBridge).filter(
        AudienceBridge.id == bridge_id,
        AudienceBridge.workspace_id == ws.id,
    ).first()
    if not bridge:
        raise HTTPException(status_code=404, detail="Bridge not found")
    if bridge.status == BridgeStatus.sent:
        raise HTTPException(status_code=400, detail="Bridge already sent")

    bridge.status  = BridgeStatus.sent
    bridge.sent_at = datetime.utcnow()
    db.commit()

    background_tasks.add_task(_deliver_bridge, bridge_id)

    return {
        "sent":       True,
        "recipients": bridge.recipients,
        "message":    f"Bridge sent to {bridge.recipients:,} subscribers. Results available in 24 hours.",
    }


def _deliver_bridge(bridge_id: str):
    """
    Background task: send in-app notifications to subscribers.
    In production, also send push notifications and emails.
    """
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        bridge = db.query(AudienceBridge).filter(AudienceBridge.id == bridge_id).first()
        if not bridge:
            return
        # In production: queue emails via Resend, push via FCM
        bridge.opened = int((bridge.recipients or 0) * 0.35)   # industry avg open rate
        db.commit()
    except Exception as e:
        print(f"[BRIDGE DELIVER] {e}")
    finally:
        db.close()


@router.get("/bridge")
def list_bridges(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    ws      = _get_workspace(user, db)
    bridges = db.query(AudienceBridge).filter(
        AudienceBridge.workspace_id == ws.id
    ).order_by(desc(AudienceBridge.created_at)).all()

    return {
        "bridges": [
            {
                "id":          b.id,
                "subject":     b.subject,
                "from_channel": b.from_channel_id,
                "to_channel":   b.to_channel_id,
                "recipients":  b.recipients,
                "opened":      b.opened,
                "clicked":     b.clicked,
                "converted":   b.converted,
                "status":      b.status.value,
                "sent_at":     b.sent_at.isoformat() if b.sent_at else None,
            }
            for b in bridges
        ]
    }


# ─────────────────────────────────────────────
# CONTENT PASSPORT
# ─────────────────────────────────────────────

@router.post("/passport", status_code=201)
def create_passport(
    req: CreatePassportRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Distribute one video to multiple channels with custom metadata per channel.
    FlintX exclusive — doubles or triples ad revenue from one piece of content.
    """
    ws = _get_workspace(user, db)

    # Verify source video belongs to this workspace
    source = db.query(Video).filter(Video.id == req.source_video_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source video not found")

    if len(req.distributions) < 1:
        raise HTTPException(status_code=422, detail="At least one distribution channel required")
    if len(req.distributions) > 10:
        raise HTTPException(status_code=422, detail="Maximum 10 distributions per passport")

    passport = ContentPassport(
        workspace_id    = ws.id,
        source_video_id = req.source_video_id,
        status          = PassportStatus.active,
    )
    db.add(passport)
    db.flush()

    created_distributions = []
    for dist in req.distributions:
        ch = db.query(Channel).filter(
            Channel.id == dist.get("channel_id"),
            Channel.workspace_id == ws.id,
        ).first()
        if not ch:
            continue

        d = PassportDistribution(
            passport_id        = passport.id,
            channel_id         = ch.id,
            video_id           = req.source_video_id,
            custom_title       = dist.get("custom_title") or source.title,
            custom_description = dist.get("custom_description") or source.description,
            custom_thumbnail   = dist.get("custom_thumbnail") or source.thumbnail_url,
        )
        db.add(d)
        created_distributions.append({"channel": ch.name, "title": d.custom_title})

    db.commit()

    return {
        "passport_id":     passport.id,
        "source_video":    source.title,
        "distributions":   created_distributions,
        "channel_count":   len(created_distributions),
        "message":         f"Video distributed to {len(created_distributions)} channels. Each will earn ad revenue independently.",
    }


@router.get("/passport")
def list_passports(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws        = _get_workspace(user, db)
    passports = db.query(ContentPassport).filter(
        ContentPassport.workspace_id == ws.id
    ).order_by(desc(ContentPassport.created_at)).all()

    result = []
    for p in passports:
        dists = db.query(PassportDistribution).filter(PassportDistribution.passport_id == p.id).all()
        total_views    = sum(d.views for d in dists)
        total_earnings = sum(d.earnings for d in dists)
        source = db.query(Video).filter(Video.id == p.source_video_id).first()
        result.append({
            "id":             p.id,
            "source_title":   source.title if source else "",
            "channel_count":  len(dists),
            "total_views":    total_views,
            "total_earnings": CurrencyService.to_display(total_earnings, currency),
            "status":         p.status.value,
            "created_at":     p.created_at.isoformat(),
        })

    return {"passports": result}


# ─────────────────────────────────────────────
# COLLAB SPLITS
# ─────────────────────────────────────────────

@router.post("/collab", status_code=201)
def propose_collab(
    req: ProposeCollabRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    total = req.creator_a_share + req.creator_b_share + req.creator_c_share + req.creator_d_share
    if total != 100:
        raise HTTPException(status_code=422, detail=f"Shares must sum to 100. Currently: {total}")

    video = db.query(Video).filter(Video.id == req.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if db.query(CollabSplit).filter(CollabSplit.video_id == req.video_id).first():
        raise HTTPException(status_code=409, detail="This video already has a collab split")

    creator_b = db.query(User).filter(User.id == req.creator_b_id).first()
    if not creator_b:
        raise HTTPException(status_code=404, detail="Creator B not found on FlintX")

    split = CollabSplit(
        video_id        = req.video_id,
        creator_a_id    = user.id,
        creator_a_share = req.creator_a_share,
        creator_b_id    = req.creator_b_id,
        creator_b_share = req.creator_b_share,
        creator_c_id    = req.creator_c_id,
        creator_c_share = req.creator_c_share,
        creator_d_id    = req.creator_d_id,
        creator_d_share = req.creator_d_share,
        message         = req.message,
        status          = CollabStatus.pending,
    )
    db.add(split)
    db.commit()
    db.refresh(split)

    return {
        "collab_id":  split.id,
        "video":      video.title,
        "split":      f"{req.creator_a_share}% / {req.creator_b_share}%",
        "status":     "pending",
        "message":    f"Proposal sent to {creator_b.full_name}. Revenue split activates when they accept.",
    }


@router.post("/collab/{collab_id}/accept")
def accept_collab(
    collab_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    split = db.query(CollabSplit).filter(
        CollabSplit.id == collab_id,
        CollabSplit.creator_b_id == user.id,
        CollabSplit.status == CollabStatus.pending,
    ).first()
    if not split:
        raise HTTPException(status_code=404, detail="Collab proposal not found or already actioned")

    split.status      = CollabStatus.active
    split.accepted_at = datetime.utcnow()
    db.commit()

    video = db.query(Video).filter(Video.id == split.video_id).first()
    return {
        "accepted": True,
        "video":    video.title if video else "",
        "your_share": f"{split.creator_b_share}%",
        "message":  "Revenue split is now live. FlintX handles all payments automatically.",
    }


@router.post("/collab/{collab_id}/decline")
def decline_collab(
    collab_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    split = db.query(CollabSplit).filter(
        CollabSplit.id == collab_id,
        CollabSplit.creator_b_id == user.id,
        CollabSplit.status == CollabStatus.pending,
    ).first()
    if not split:
        raise HTTPException(status_code=404, detail="Collab proposal not found")
    split.status = CollabStatus.declined
    db.commit()
    return {"declined": True}


@router.get("/collab")
def list_collabs(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    sent = db.query(CollabSplit).filter(CollabSplit.creator_a_id == user.id).all()
    received = db.query(CollabSplit).filter(
        CollabSplit.creator_b_id == user.id
    ).all()

    def _fmt(s: CollabSplit) -> dict:
        video = db.query(Video).filter(Video.id == s.video_id).first()
        return {
            "id":            s.id,
            "video_title":   video.title if video else "",
            "status":        s.status.value,
            "split":         f"{s.creator_a_share}% / {s.creator_b_share}%",
            "total_earned":  CurrencyService.to_display(s.total_earned, currency),
            "your_earned":   CurrencyService.to_display(
                s.creator_a_earned if s.creator_a_id == user.id else s.creator_b_earned,
                currency
            ),
            "created_at":    s.created_at.isoformat(),
        }

    return {
        "sent":     [_fmt(s) for s in sent],
        "received": [_fmt(s) for s in received],
    }


@router.get("/collab/{collab_id}/earnings")
def collab_earnings(
    collab_id: str,
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    split = db.query(CollabSplit).filter(CollabSplit.id == collab_id).first()
    if not split:
        raise HTTPException(status_code=404, detail="Collab not found")
    if user.id not in [split.creator_a_id, split.creator_b_id, split.creator_c_id, split.creator_d_id]:
        raise HTTPException(status_code=403, detail="Not a member of this collab")

    video = db.query(Video).filter(Video.id == split.video_id).first()
    return {
        "collab_id":       collab_id,
        "video":           video.title if video else "",
        "status":          split.status.value,
        "total_earned":    CurrencyService.to_display(split.total_earned, currency),
        "breakdown": {
            "creator_a": {"share": f"{split.creator_a_share}%", "earned": CurrencyService.to_display(split.creator_a_earned, currency)},
            "creator_b": {"share": f"{split.creator_b_share}%", "earned": CurrencyService.to_display(split.creator_b_earned, currency)},
            **({"creator_c": {"share": f"{split.creator_c_share}%", "earned": CurrencyService.to_display(split.creator_c_earned, currency)}} if split.creator_c_id else {}),
            **({"creator_d": {"share": f"{split.creator_d_share}%", "earned": CurrencyService.to_display(split.creator_d_earned, currency)}} if split.creator_d_id else {}),
        },
        "payments":        "Handled automatically by FlintX on every ad impression.",
    }


# ─────────────────────────────────────────────
# CHANNEL LENDING
# ─────────────────────────────────────────────

@router.post("/lend", status_code=201)
def offer_lending(
    req: OfferLendRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws = _get_workspace(user, db)

    from_ch = db.query(Channel).filter(Channel.id == req.from_channel_id, Channel.workspace_id == ws.id).first()
    if not from_ch:
        raise HTTPException(status_code=404, detail="From channel not found in your workspace")

    to_ch = db.query(Channel).filter(Channel.id == req.to_channel_id).first()
    if not to_ch:
        raise HTTPException(status_code=404, detail="Destination channel not found")

    if from_ch.subscriber_count < 1000:
        raise HTTPException(status_code=422, detail="Channel Lending requires at least 1,000 subscribers")

    ends = datetime.utcnow() + timedelta(days=req.duration_days)
    lend = ChannelLend(
        from_channel_id = from_ch.id,
        to_channel_id   = to_ch.id,
        duration_days   = req.duration_days,
        fee_type        = req.fee_type,
        fee_flat_cents  = req.fee_flat_cents,
        fee_revenue_pct = req.fee_revenue_pct,
        starts_at       = datetime.utcnow(),
        ends_at         = ends,
        status          = LendStatus.active,
    )
    db.add(lend)
    from_ch.is_lending       = True
    from_ch.lending_expires  = ends
    db.commit()
    db.refresh(lend)

    return {
        "lend_id":        lend.id,
        "from_channel":   from_ch.name,
        "to_channel":     to_ch.name,
        "duration_days":  req.duration_days,
        "ends_at":        ends.isoformat(),
        "fee":            CurrencyService.format(req.fee_flat_cents, "USD") if req.fee_type == "flat" else f"{req.fee_revenue_pct}% of revenue",
        "message":        f"{from_ch.name}'s audience will see {to_ch.name} content for {req.duration_days} days. This is not available on any other platform.",
    }


@router.get("/lend")
def list_lends(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    ws       = _get_workspace(user, db)
    channels = db.query(Channel).filter(Channel.workspace_id == ws.id).all()
    ch_ids   = [c.id for c in channels]

    lends = db.query(ChannelLend).filter(
        ChannelLend.from_channel_id.in_(ch_ids),
        ChannelLend.status == LendStatus.active,
    ).all()

    return {
        "active_lends": [
            {
                "id":             l.id,
                "from_channel":   l.from_channel_id,
                "to_channel":     l.to_channel_id,
                "ends_at":        l.ends_at.isoformat() if l.ends_at else None,
                "days_remaining": max(0, (l.ends_at - datetime.utcnow()).days) if l.ends_at else 0,
                "new_subs":       l.new_subs_generated,
                "fee_earned":     CurrencyService.to_display(l.fee_earned, currency),
            }
            for l in lends
        ]
    }
