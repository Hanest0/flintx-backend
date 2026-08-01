"""
Flint — Video API Routes

POST /api/videos/upload-url       — get S3 presigned upload URL
POST /api/videos/process          — trigger MediaConvert after upload
GET  /api/videos/feed             — home feed (personalised)
GET  /api/videos/shorts           — FlintX Clips feed
GET  /api/videos/{id}             — single video + playback URL
GET  /api/videos/{id}/related     — related videos
POST /api/videos/{id}/view        — record a view + ad impression
POST /api/videos/{id}/like        — toggle like
PATCH /api/videos/{id}            — update title/description
DELETE /api/videos/{id}           — delete video
GET  /api/videos/channel/{handle} — videos for a channel
GET  /api/videos/studio           — creator's own videos (studio view)
GET  /api/videos/{id}/earnings    — earnings for a specific video
"""

import json
import hashlib
from datetime import datetime
from typing import Optional, Literal

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    Video, VideoView, VideoStatus, VideoType, ModerationStatus,
    User, CreatorProfile, AlgoScore, AdImpression, AdCampaign,
    CampaignStatus, Transaction, TxnType,
)
from ..auth.routes import get_current_user, require_verified
from ..storage.aws_video import create_upload_url, start_transcoding, get_stream_url

router = APIRouter(prefix="/videos", tags=["Videos"])


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class UploadURLRequest(BaseModel):
    filename:     str
    content_type: str
    video_type:   Literal["long", "short"] = "long"

class ProcessRequest(BaseModel):
    video_id:    str
    s3_key:      str
    title:       str
    description: str = ""
    category:    str = "general"
    video_type:  Literal["long", "short"] = "long"
    tags:        list[str] = []

class UpdateVideoRequest(BaseModel):
    title:       Optional[str] = None
    description: Optional[str] = None
    category:    Optional[str] = None

class ViewRequest(BaseModel):
    watched_pct:    float = 0.0    # 0.0–1.0, how far they watched
    ad_completed:   bool = False

class FeedRequest(BaseModel):
    category: Optional[str] = None
    page:     int = 1
    limit:    int = 20


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _video_dict(video: Video, include_stream_url: bool = False) -> dict:
    d = {
        "id":            video.id,
        "title":         video.title,
        "description":   video.description or "",
        "category":      video.category or "",
        "video_type":    video.video_type.value,
        "status":        video.status.value,
        "thumbnail_url": video.thumbnail_url,
        "duration_s":    video.duration_s,
        "views":         video.view_count,
        "likes":         video.like_count,
        "creator_id":    video.creator_id,
        "channel_name":  video.creator.channel_name if video.creator else "",
        "channel_handle": video.creator.channel_handle if video.creator else "",
        "algo_score":    video.algo_score,
        "created_at":    video.created_at.isoformat(),
        "published_at":  video.published_at.isoformat() if video.published_at else None,
        "ad_revenue":    video.ad_revenue_total,
        "creator_earnings": video.creator_earnings,
    }
    if include_stream_url and video.hls_url:
        d["hls_url"]     = video.hls_url
        d["hls_1080_url"] = video.hls_1080_url
        d["hls_720_url"]  = video.hls_720_url
        d["hls_360_url"]  = video.hls_360_url
    return d


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────
# UPLOAD — get presigned URL
# ─────────────────────────────────────────────

@router.post("/upload-url")
def get_upload_url(
    req: UploadURLRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Returns a presigned S3 URL. The client uploads directly to S3.
    No video data passes through the FlintX backend server.
    """
    if user.role not in ("creator", "both", "admin"):
        raise HTTPException(status_code=403, detail="Creator account required")

    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator profile not found")

    import uuid
    video_id = str(uuid.uuid4())
    s3_key   = f"uploads/{creator.id}/{video_id}/{req.filename}"

    presigned_url = create_upload_url(s3_key, req.content_type)

    # Create a Video record in uploading state
    video = Video(
        id         = video_id,
        creator_id = creator.id,
        title      = req.filename,
        video_type = VideoType(req.video_type),
        status     = VideoStatus.uploading,
        s3_key_raw = s3_key,
    )
    db.add(video)
    db.commit()

    return {
        "video_id":     video_id,
        "upload_url":   presigned_url,
        "s3_key":       s3_key,
        "expires_in_s": 3600,
    }


# ─────────────────────────────────────────────
# PROCESS — trigger MediaConvert after upload
# ─────────────────────────────────────────────

@router.post("/process")
def process_video(
    req: ProcessRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Called by the frontend after the upload to S3 completes.
    Triggers MediaConvert transcoding and queues moderation.
    """
    video = db.query(Video).filter(Video.id == req.video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Update metadata
    video.title       = req.title
    video.description = req.description
    video.category    = req.category
    video.tags        = json.dumps(req.tags)
    video.status      = VideoStatus.processing

    db.commit()

    # Start transcoding in background
    background_tasks.add_task(_transcode_and_moderate, video.id, req.s3_key)

    return {"message": "Processing started", "video_id": video.id}


def _transcode_and_moderate(video_id: str, s3_key: str):
    """Background task: transcode → moderation queue."""
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return

        # Start AWS MediaConvert job
        job_id, hls_path = start_transcoding(s3_key, video_id)

        video.mediaconvert_job = job_id
        video.s3_key_hls       = hls_path
        video.status           = VideoStatus.mod_pending
        video.mod_status       = ModerationStatus.pending
        db.commit()

        # Automated moderation runs separately — see moderation/routes.py
        # It reads videos with mod_status=pending and processes them

    except Exception as e:
        print(f"[VIDEO PROCESSING ERROR] video_id={video_id} error={e}")
        db = SessionLocal()
        v = db.query(Video).filter(Video.id == video_id).first()
        if v:
            v.status = VideoStatus.mod_pending
            db.commit()
    finally:
        db.close()


# ─────────────────────────────────────────────
# FEED — home feed (personalised by algo score)
# ─────────────────────────────────────────────

@router.get("/feed")
def get_feed(
    category: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
    request: Request = None,
):
    """
    Returns published videos sorted by algorithm score.
    Category filter optional. Paginated.
    """
    limit = min(limit, 50)
    offset = (page - 1) * limit

    q = db.query(Video).filter(Video.status == VideoStatus.published)
    if category and category != "All":
        q = q.filter(Video.category == category)

    videos = q.order_by(desc(Video.algo_score)).offset(offset).limit(limit).all()
    total  = q.count()

    return {
        "videos":    [_video_dict(v) for v in videos],
        "total":     total,
        "page":      page,
        "pages":     (total // limit) + 1,
    }


# ─────────────────────────────────────────────
# SHORTS FEED
# ─────────────────────────────────────────────

@router.get("/shorts")
def get_shorts(page: int = 1, limit: int = 10, db: Session = Depends(get_db)):
    limit  = min(limit, 20)
    offset = (page - 1) * limit
    videos = db.query(Video).filter(
        Video.status     == VideoStatus.published,
        Video.video_type == VideoType.short,
    ).order_by(desc(Video.algo_score)).offset(offset).limit(limit).all()
    return {"videos": [_video_dict(v) for v in videos]}


# ─────────────────────────────────────────────
# SINGLE VIDEO
# ─────────────────────────────────────────────

@router.get("/{video_id}")
def get_video(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video or video.status == VideoStatus.deleted:
        raise HTTPException(status_code=404, detail="Video not found")

    d = _video_dict(video, include_stream_url=True)

    # If video is published and has HLS, get fresh CloudFront URL
    if video.status == VideoStatus.published and video.s3_key_hls:
        d["hls_url"] = get_stream_url(video.s3_key_hls)

    return d


# ─────────────────────────────────────────────
# RELATED VIDEOS
# ─────────────────────────────────────────────

@router.get("/{video_id}/related")
def get_related(video_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    related = db.query(Video).filter(
        Video.status   == VideoStatus.published,
        Video.category == video.category,
        Video.id       != video_id,
    ).order_by(desc(Video.algo_score)).limit(8).all()

    return {"videos": [_video_dict(v) for v in related]}


# ─────────────────────────────────────────────
# RECORD A VIEW
# ─────────────────────────────────────────────

@router.post("/{video_id}/view")
def record_view(
    video_id: str,
    req: ViewRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Records a view event. If viewer is a Pass subscriber and completed the ad,
    issues them credits and bills the advertiser.
    """
    video = db.query(Video).filter(
        Video.id == video_id, Video.status == VideoStatus.published
    ).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Try to identify the viewer
    user    = None
    user_id = None
    try:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            from ..auth.security import decode_access_token
            payload = decode_access_token(auth.split(" ")[1])
            if payload:
                user    = db.query(User).filter(User.id == payload["sub"]).first()
                user_id = user.id if user else None
    except Exception:
        pass

    is_pass_viewer = user and user.pass_status.value in ("monthly", "annual")
    ip_hash        = _hash_ip(request.client.host)

    # Record the view
    view = VideoView(
        video_id       = video_id,
        user_id        = user_id,
        ip_hash        = ip_hash,
        watched_pct    = req.watched_pct,
        is_pass_viewer = is_pass_viewer,
        ad_completed   = req.ad_completed,
    )
    db.add(view)

    # Increment view count
    video.view_count += 1

    # Update completion rate (rolling average)
    video.completion_rate = (
        (video.completion_rate * (video.view_count - 1) + req.watched_pct)
        / video.view_count
    )

    db.commit()

    # Ad revenue processing in background
    if req.ad_completed:
        background_tasks.add_task(_process_ad_revenue, video_id, user_id, is_pass_viewer, db)

    return {"recorded": True}


def _process_ad_revenue(video_id: str, viewer_id: Optional[str], is_pass_viewer: bool, db):
    """
    Find an active ad campaign matching this video, bill it, pay the creator.
    """
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return

        # Find best matching campaign for this video's category + safety level
        campaign = db.query(AdCampaign).filter(
            AdCampaign.status == CampaignStatus.active,
            AdCampaign.budget_pence > AdCampaign.spent_pence,
        ).first()

        if not campaign:
            return  # No active campaigns — no ad revenue this view

        # CPM billing: charge per impression
        cpm      = campaign.cpm_pence
        cost     = cpm // 1000    # cost per single impression (CPM / 1000)

        # Get current phase — creator share varies by phase
        from ..payouts.models import PlatformState, PlatformPhase, CreatorCreditLedger, CreditStatus, FoundingCreator
        from ..payouts.routes import _get_state, PHASE_CONFIG, record_unique_viewer
        state = _get_state(db)
        phase = state.phase
        phase_config = PHASE_CONFIG[phase]
        creator_share_pct = phase_config["creator_share"] / 100

        creator_cut  = int(cost * creator_share_pct)
        platform_cut = cost - creator_cut
        viewer_credit = int(platform_cut * 0.15) if is_pass_viewer else 0

        # Record impression
        impression = AdImpression(
            campaign_id    = campaign.id,
            video_id       = video_id,
            viewer_id      = viewer_id,
            is_pass_viewer = is_pass_viewer,
            completed      = True,
            cpm_charged    = cpm,
            creator_cut    = creator_cut,
            platform_cut   = platform_cut,
            viewer_credit  = viewer_credit,
        )
        db.add(impression)

        # Update campaign spend
        campaign.spent_pence  += cost
        campaign.impressions  += 1
        campaign.completions  += 1

        # Update video earnings
        video.ad_revenue_total += cost
        video.creator_earnings += creator_cut

        # Check for collab split on this video
        from ..workspace.models import CollabSplit, CollabStatus
        collab = db.query(CollabSplit).filter(
            CollabSplit.video_id == video_id,
            CollabSplit.status   == CollabStatus.active,
        ).first()

        if collab:
            # Distribute creator_cut according to agreed split percentages
            splits = [
                (collab.creator_a_id, collab.creator_a_share, "creator_a_earned"),
                (collab.creator_b_id, collab.creator_b_share, "creator_b_earned"),
            ]
            if collab.creator_c_id:
                splits.append((collab.creator_c_id, collab.creator_c_share, "creator_c_earned"))
            if collab.creator_d_id:
                splits.append((collab.creator_d_id, collab.creator_d_share, "creator_d_earned"))

            collab.total_earned += creator_cut

            for creator_id, share_pct, earned_field in splits:
                if not creator_id or share_pct <= 0:
                    continue
                split_amount = int(creator_cut * share_pct / 100)
                setattr(collab, earned_field, getattr(collab, earned_field, 0) + split_amount)

                creator_user = db.query(User).filter(User.id == creator_id).first()
                if creator_user:
                    creator_user.wallet_balance += split_amount
                    db.add(Transaction(
                        user_id      = creator_user.id,
                        type         = TxnType.ad_revenue,
                        amount       = split_amount,
                        balance_after = creator_user.wallet_balance,
                        description  = f"Collab revenue ({share_pct}%) — {video.title[:50]}",
                        reference    = campaign.id,
                        video_id     = video_id,
                    ))
        else:
            # Standard single-creator payment via phased payout system
            creator_user = db.query(User).join(CreatorProfile).filter(
                CreatorProfile.id == video.creator_id
            ).first()
            if creator_user:
                creator_profile = db.query(CreatorProfile).filter(
                    CreatorProfile.id == video.creator_id
                ).first()
                if creator_profile:
                    creator_profile.pending_payout += creator_cut
                    creator_profile.total_earnings  += creator_cut

                # Phase-aware credit split
                cash_pct   = phase_config["cash_portion"] / 100
                cash_now   = int(creator_cut * cash_pct)
                lock_now   = creator_cut - cash_now

                is_founding = bool(db.query(FoundingCreator).filter(
                    FoundingCreator.user_id == creator_user.id
                ).first())

                ledger_entry = CreatorCreditLedger(
                    creator_id       = creator_user.id,
                    amount_cents     = creator_cut,
                    phase_earned     = phase,
                    status           = CreditStatus.locked if lock_now == creator_cut else CreditStatus.partial if lock_now > 0 else CreditStatus.available,
                    source           = "ad_revenue",
                    video_id         = video_id,
                    cash_portion     = cash_now,
                    credit_portion   = lock_now,
                    founding_creator = is_founding,
                )
                db.add(ledger_entry)

                if cash_now > 0:
                    creator_user.wallet_balance += cash_now
                if lock_now > 0:
                    state.total_locked_credits += lock_now

                db.add(Transaction(
                    user_id      = creator_user.id,
                    type         = TxnType.ad_revenue,
                    amount       = creator_cut,
                    balance_after = creator_user.wallet_balance,
                    description  = f"Ad revenue ({phase_config['creator_share']}% Phase {phase_config['phase_num']}) — {video.title[:50]}",
                    reference    = campaign.id,
                    video_id     = video_id,
                ))

        # Record unique viewer for milestone tracking
        if viewer_id:
            record_unique_viewer(db, viewer_id, 60)

        # Credit viewer wallet if Pass subscriber
        if is_pass_viewer and viewer_id and viewer_credit > 0:
            viewer_user = db.query(User).filter(User.id == viewer_id).first()
            if viewer_user:
                viewer_user.wallet_balance += viewer_credit
                db.add(Transaction(
                    user_id     = viewer_id,
                    type        = TxnType.viewer_credit,
                    amount      = viewer_credit,
                    balance_after = viewer_user.wallet_balance,
                    description = "FlintX Pass credit — ad completed",
                    video_id    = video_id,
                ))

        # Update advertiser spend
        advertiser = db.query(AdvertiserProfile).filter(
            AdvertiserProfile.id == campaign.advertiser_id
        ).first()
        if advertiser:
            advertiser.budget_balance -= cost
            advertiser.total_spent    += cost

        # Process referral bonus if viewer was referred
        from ..referral.routes import process_referral_bonus
        process_referral_bonus(db, viewer_id, platform_cut)

        db.commit()

    except Exception as e:
        print(f"[AD REVENUE ERROR] {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
# LIKE / UNLIKE
# ─────────────────────────────────────────────

@router.post("/{video_id}/like")
def toggle_like(video_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    # Simple toggle — production would use a likes join table
    video.like_count += 1
    db.commit()
    return {"likes": video.like_count}


# ─────────────────────────────────────────────
# UPDATE VIDEO
# ─────────────────────────────────────────────

@router.patch("/{video_id}")
def update_video(
    video_id: str,
    req: UpdateVideoRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    video   = db.query(Video).filter(Video.id == video_id, Video.creator_id == creator.id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found or not yours")
    if req.title:
        video.title = req.title
    if req.description is not None:
        video.description = req.description
    if req.category:
        video.category = req.category
    db.commit()
    return _video_dict(video)


# ─────────────────────────────────────────────
# DELETE VIDEO
# ─────────────────────────────────────────────

@router.delete("/{video_id}")
def delete_video(
    video_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    video   = db.query(Video).filter(Video.id == video_id, Video.creator_id == creator.id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found or not yours")
    video.status = VideoStatus.deleted
    db.commit()
    return {"deleted": True}


# ─────────────────────────────────────────────
# CHANNEL VIDEOS
# ─────────────────────────────────────────────

@router.get("/channel/{handle}")
def channel_videos(handle: str, page: int = 1, limit: int = 20, db: Session = Depends(get_db)):
    creator = db.query(CreatorProfile).filter(CreatorProfile.channel_handle == handle).first()
    if not creator:
        raise HTTPException(status_code=404, detail="Channel not found")

    limit  = min(limit, 50)
    offset = (page - 1) * limit

    videos = db.query(Video).filter(
        Video.creator_id == creator.id,
        Video.status     == VideoStatus.published,
    ).order_by(desc(Video.created_at)).offset(offset).limit(limit).all()

    return {
        "channel": {
            "id":     creator.id,
            "name":   creator.channel_name,
            "handle": creator.channel_handle,
            "bio":    creator.bio,
            "subs":   creator.subscriber_count,
        },
        "videos": [_video_dict(v) for v in videos],
    }


# ─────────────────────────────────────────────
# STUDIO — creator's own videos
# ─────────────────────────────────────────────

@router.get("/studio/my-videos")
def studio_my_videos(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    if not creator:
        raise HTTPException(status_code=403, detail="Creator account required")

    videos = db.query(Video).filter(
        Video.creator_id != VideoStatus.deleted,
        Video.creator_id == creator.id,
    ).order_by(desc(Video.created_at)).limit(50).all()

    return {
        "videos": [_video_dict(v, include_stream_url=False) for v in videos],
        "stats": {
            "total_views":    creator.total_views,
            "total_earnings": creator.total_earnings,
            "pending_payout": creator.pending_payout,
            "video_count":    creator.video_count,
        }
    }


# ─────────────────────────────────────────────
# VIDEO EARNINGS BREAKDOWN
# ─────────────────────────────────────────────

@router.get("/{video_id}/earnings")
def video_earnings(
    video_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    video   = db.query(Video).filter(Video.id == video_id, Video.creator_id == creator.id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found or not yours")

    impressions = db.query(AdImpression).filter(AdImpression.video_id == video_id).count()
    completions = db.query(AdImpression).filter(
        AdImpression.video_id == video_id, AdImpression.completed == True
    ).count()

    return {
        "video_id":          video_id,
        "ad_revenue_gross":  video.ad_revenue_total,
        "creator_earnings":  video.creator_earnings,
        "platform_cut":      video.ad_revenue_total - video.creator_earnings,
        "impressions":       impressions,
        "completions":       completions,
        "completion_rate":   round(completions / impressions, 3) if impressions else 0,
        "view_count":        video.view_count,
    }
