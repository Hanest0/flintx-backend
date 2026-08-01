"""
Flint — Recommendation Algorithm Scorer

Runs as a background job (every hour via APScheduler or Celery).
Scores every published video and caches the result in algo_scores table.
The feed endpoint reads algo_score directly — no real-time computation.

Weights (must sum to 100):
  completion_rate     30% — most important: did viewers actually finish?
  like_rate           15% — % of viewers who liked
  category_affinity   15% — viewer's preference for this content category
  recency             12% — 14-day half-life decay
  creator_retention    8% — does the creator's audience come back?
  share_rate           8% — strongest virality signal
  consistency          7% — creator uploads regularly
  new_creator_boost    5% — first 10 videos get +40% score

Algorithm for a given video:
  base_score = (
    (completion_rate * 0.30) +
    (like_rate * 0.15) +
    (recency_score * 0.12) +
    (creator_retention * 0.08) +
    (share_rate * 0.08) +
    (consistency_score * 0.07)
  ) * 100

  If new creator (video count <= 10): score *= 1.40
  Final score capped at 100.
"""

import math
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from ..database.connection import SessionLocal
from ..database.models import Video, VideoView, VideoStatus, AlgoScore, CreatorProfile


# ─────────────────────────────────────────────
# SCORE ONE VIDEO
# ─────────────────────────────────────────────

def score_video(video: Video, db: Session) -> float:
    """Calculate and return the algorithm score for a single video (0.0–100.0)."""

    # Skip unpublished/deleted
    if video.status != VideoStatus.published:
        return 0.0

    view_count = max(video.view_count, 1)

    # ── Completion Rate (30%) ─────────────────
    # Rolling average stored on the video record
    completion = min(video.completion_rate or 0.0, 1.0)

    # ── Like Rate (15%) ──────────────────────
    like_rate = min((video.like_count or 0) / view_count, 1.0)

    # ── Share Rate (8%) ──────────────────────
    share_rate = min((video.share_count or 0) / view_count, 1.0)

    # ── Recency (12%) — 14-day half-life ─────
    if video.published_at:
        days_old = (datetime.utcnow() - video.published_at).days
        recency  = math.pow(0.5, days_old / 14)   # halves every 14 days
    else:
        recency  = 0.0

    # ── Creator Retention (8%) ───────────────
    # Approximation: creator's avg completion rate across all videos
    creator_retention = _creator_retention(video.creator_id, db)

    # ── Creator Consistency (7%) ─────────────
    # Did the creator upload consistently? Score based on video count + recency of uploads
    consistency = _creator_consistency(video.creator_id, db)

    # ── Base score ───────────────────────────
    base = (
        completion      * 0.30 +
        like_rate       * 0.15 +
        recency         * 0.12 +
        creator_retention * 0.08 +
        share_rate      * 0.08 +
        consistency     * 0.07
    ) * 100

    # ── New creator boost ─────────────────────
    creator = db.query(CreatorProfile).filter(CreatorProfile.id == video.creator_id).first()
    is_new_creator = creator and creator.video_count <= 10
    if is_new_creator:
        base *= 1.40

    return min(round(base, 2), 100.0)


def _creator_retention(creator_id: str, db: Session) -> float:
    """Average completion rate across all creator's published videos."""
    from sqlalchemy import func
    result = db.query(func.avg(Video.completion_rate)).filter(
        Video.creator_id == creator_id,
        Video.status     == VideoStatus.published,
    ).scalar()
    return min(float(result or 0), 1.0)


def _creator_consistency(creator_id: str, db: Session) -> float:
    """
    Consistency score: 1.0 if creator has published 2+ videos in last 30 days.
    0.5 if 1 video in 30 days. 0.1 if nothing recent.
    """
    recent = db.query(Video).filter(
        Video.creator_id  == creator_id,
        Video.status      == VideoStatus.published,
        Video.published_at >= datetime.utcnow() - timedelta(days=30),
    ).count()

    if recent >= 8:  return 1.0
    if recent >= 4:  return 0.85
    if recent >= 2:  return 0.65
    if recent == 1:  return 0.40
    return 0.10


# ─────────────────────────────────────────────
# SCORE ALL VIDEOS (background job)
# ─────────────────────────────────────────────

def score_all_videos():
    """
    Recalculate algo_score for every published video.
    Call this every hour via APScheduler or Celery beat.

    Add to your scheduler:
        scheduler.add_job(score_all_videos, 'interval', hours=1)
    """
    db = SessionLocal()
    try:
        videos = db.query(Video).filter(Video.status == VideoStatus.published).all()
        updated = 0
        for video in videos:
            score = score_video(video, db)
            video.algo_score = score

            # Update or create AlgoScore record
            record = db.query(AlgoScore).filter(AlgoScore.video_id == video.id).first()
            if record:
                record.total_score      = score
                record.completion_score = video.completion_rate or 0
                record.recency_score    = math.pow(0.5, (datetime.utcnow() - video.published_at).days / 14) if video.published_at else 0
                record.calculated_at    = datetime.utcnow()
            else:
                db.add(AlgoScore(
                    video_id        = video.id,
                    total_score     = score,
                    completion_score = video.completion_rate or 0,
                    calculated_at   = datetime.utcnow(),
                ))

            updated += 1
            if updated % 100 == 0:
                db.commit()   # commit in batches

        db.commit()
        print(f"[ALGORITHM] Scored {updated} videos")

    except Exception as e:
        print(f"[ALGORITHM ERROR] {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
# AD MATCHING — which campaign matches this video?
# ─────────────────────────────────────────────

def match_ad_campaign(video: Video, db: Session):
    """
    Find the best active campaign for a video.
    Priority:
      1. Category match
      2. Safety level compatibility
      3. Highest remaining budget
    """
    from ..database.models import AdCampaign, CampaignStatus, SafetyLevel

    # Safety level ordering (higher = more restrictive = fewer ads)
    safety_order = {
        "safe_for_all": 0,
        "standard":     1,
        "mature_ok":    2,
        "limited_ads":  3,
        "no_ads":       4,
    }
    video_safety_level = safety_order.get(video.safety_level.value if video.safety_level else "standard", 1)

    campaigns = db.query(AdCampaign).filter(
        AdCampaign.status == CampaignStatus.active,
        AdCampaign.budget_pence > AdCampaign.spent_pence,
    ).all()

    best = None
    best_budget = 0

    for c in campaigns:
        # Check safety compatibility — campaign's required level must be <= video's level
        campaign_safety = safety_order.get(c.safety_required.value if c.safety_required else "safe_for_all", 0)
        if campaign_safety > video_safety_level:
            continue   # video too risky for this advertiser

        # Category match check
        target_niches = json.loads(c.target_niches) if c.target_niches else []
        if target_niches and video.category and video.category.lower() not in [n.lower() for n in target_niches]:
            continue   # category doesn't match

        remaining = c.budget_pence - c.spent_pence
        if remaining > best_budget:
            best        = c
            best_budget = remaining

    return best


import json
