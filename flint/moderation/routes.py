"""
Flint — Content Moderation

Four-layer pipeline:
  Layer 1: AWS Rekognition — thumbnail visual scan (~5 sec)
  Layer 2: OpenAI — title/description text check (~2 sec)
  Layer 3: Heuristic rules — keyword/spam patterns (instant)
  Layer 4: Human review queue — you in the Command Centre

Routes:
  POST /api/moderation/run/{video_id}   — run automated checks (called after transcoding)
  GET  /api/moderation/queue            — human review queue (admin only)
  POST /api/moderation/approve/{id}     — approve a video (admin only)
  POST /api/moderation/reject/{id}      — reject a video (admin only)
  POST /api/moderation/appeal/{id}      — creator submits appeal
  GET  /api/moderation/appeals          — appeals queue (admin only)
  POST /api/moderation/appeal/{id}/resolve — resolve appeal (admin only)
  GET  /api/moderation/stats            — platform moderation stats (admin only)
"""

import os
import json
import asyncio
import boto3
import httpx
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    Video, User, VideoStatus, ModerationStatus, SafetyLevel, CreatorProfile,
)
from ..auth.routes import require_verified, require_admin
from ..email.service import send_video_approved_email, send_video_rejected_email

router = APIRouter(prefix="/moderation", tags=["Moderation"])

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
AWS_REGION       = os.getenv("AWS_REGION", "eu-west-2")
S3_BUCKET        = os.getenv("S3_BUCKET", "flintx-videos")

# Confidence threshold: above this = auto-flag for human review
FLAG_THRESHOLD   = 0.60
# Confidence threshold: above this = auto-reject immediately
REJECT_THRESHOLD = 0.90

# Keyword heuristics — instant check, no API cost
BANNED_KEYWORDS = [
    "child porn", "cp ", " cp ", "loli", "jailbait", "snuff film",
    "how to make a bomb", "how to make explosives", "drug synthesis",
]
FLAG_KEYWORDS = [
    "suicide method", "self harm tutorial", "anorexia tips", "how to get away with",
    "doxxing", "hack into", "make meth", "darkweb", "scam tutorial",
]


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class ApproveRequest(BaseModel):
    safety_level: str = "safe_for_all"   # safe_for_all|standard|mature_ok|limited_ads|no_ads
    notes:        str = ""

class RejectRequest(BaseModel):
    reason: str
    notes:  str = ""

class AppealRequest(BaseModel):
    statement: str

class ResolveAppealRequest(BaseModel):
    decision: str   # approved | rejected
    notes:    str = ""


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _rekognition():
    return boto3.client(
        "rekognition",
        region_name           = AWS_REGION,
        aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def _check_thumbnail_rekognition(thumbnail_s3_key: str) -> list[dict]:
    """Layer 1: AWS Rekognition visual content moderation."""
    if not thumbnail_s3_key:
        return []
    try:
        result = _rekognition().detect_moderation_labels(
            Image={"S3Object": {"Bucket": S3_BUCKET, "Name": thumbnail_s3_key}},
            MinConfidence=50,
        )
        return [
            {
                "type":       label["Name"].lower().replace(" ", "_"),
                "confidence": label["Confidence"] / 100,
                "source":     "Rekognition",
            }
            for label in result.get("ModerationLabels", [])
        ]
    except Exception as e:
        print(f"[REKOGNITION ERROR] {e}")
        return []


def _check_text_openai(title: str, description: str, tags: list[str]) -> list[dict]:
    """Layer 2: OpenAI text moderation."""
    if not OPENAI_API_KEY:
        return []
    text = f"Title: {title}\nDescription: {description}\nTags: {', '.join(tags)}"
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/moderations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text},
            timeout=10,
        )
        result = resp.json()
        categories = result["results"][0]["categories"]
        scores     = result["results"][0]["category_scores"]
        flags = []
        for cat, flagged in categories.items():
            score = scores.get(cat, 0)
            if flagged or score > 0.30:
                flags.append({
                    "type":       cat.replace("/", "_").replace("-", "_"),
                    "confidence": round(score, 3),
                    "source":     "OpenAI",
                })
        return flags
    except Exception as e:
        print(f"[OPENAI MODERATION ERROR] {e}")
        return []


def _check_heuristics(title: str, description: str) -> list[dict]:
    """Layer 3: Keyword heuristics — zero API cost."""
    text  = (title + " " + description).lower()
    flags = []
    for kw in BANNED_KEYWORDS:
        if kw in text:
            flags.append({"type": "banned_keyword", "confidence": 1.0, "source": "Heuristic", "keyword": kw})
    for kw in FLAG_KEYWORDS:
        if kw in text:
            flags.append({"type": "flagged_keyword", "confidence": 0.75, "source": "Heuristic", "keyword": kw})
    return flags


def _decide(flags: list[dict]) -> tuple[str, SafetyLevel]:
    """
    Given all flags, decide auto-approve / flag-for-human / auto-reject.
    Returns (decision, safety_level).
    """
    if not flags:
        return "auto_approve", SafetyLevel.safe_for_all

    # Any banned keyword or Rekognition >90% = auto reject
    if any(f["confidence"] >= REJECT_THRESHOLD or f["type"] == "banned_keyword" for f in flags):
        return "auto_reject", SafetyLevel.no_ads

    # Any flag above threshold = send to human review
    if any(f["confidence"] >= FLAG_THRESHOLD for f in flags):
        return "flag", SafetyLevel.standard

    # Low-confidence flags — approve but downgrade safety level
    return "auto_approve", SafetyLevel.standard


# ─────────────────────────────────────────────
# RUN AUTOMATED MODERATION
# ─────────────────────────────────────────────

@router.post("/run/{video_id}")
def run_moderation(
    video_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Trigger automated moderation for a video.
    Called automatically after transcoding completes.
    """
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    background_tasks.add_task(_run_moderation_pipeline, video_id)
    return {"message": "Moderation started", "video_id": video_id}


def _run_moderation_pipeline(video_id: str):
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        video = db.query(Video).filter(Video.id == video_id).first()
        if not video:
            return

        tags = json.loads(video.tags) if video.tags else []

        # Run all three automated layers
        flags = []
        flags += _check_heuristics(video.title, video.description or "")
        flags += _check_text_openai(video.title, video.description or "", tags)
        if video.thumbnail_url:
            thumbnail_key = video.thumbnail_url.split(S3_BUCKET + "/")[-1] if S3_BUCKET in (video.thumbnail_url or "") else ""
            flags += _check_thumbnail_rekognition(thumbnail_key)

        # Remove duplicates by type
        seen = set()
        unique_flags = []
        for f in flags:
            if f["type"] not in seen:
                seen.add(f["type"])
                unique_flags.append(f)

        decision, safety = _decide(unique_flags)
        video.mod_flags = json.dumps(unique_flags)

        if decision == "auto_approve":
            video.mod_status  = ModerationStatus.auto_approved
            video.status      = VideoStatus.published
            video.safety_level = safety
            video.published_at = datetime.utcnow()

            # Email creator
            creator_user = db.query(User).join(CreatorProfile).filter(
                CreatorProfile.id == video.creator_id
            ).first()
            if creator_user:
                send_video_approved_email(creator_user.email, creator_user.full_name, video.title)

        elif decision == "auto_reject":
            video.mod_status = ModerationStatus.rejected
            video.status     = VideoStatus.mod_rejected
            creator_user = db.query(User).join(CreatorProfile).filter(
                CreatorProfile.id == video.creator_id
            ).first()
            if creator_user:
                send_video_rejected_email(
                    creator_user.email, creator_user.full_name, video.title,
                    "Content violated Flint's community guidelines (automated detection)."
                )

        else:  # flag for human review
            video.mod_status = ModerationStatus.flagged
            video.status     = VideoStatus.mod_pending

        db.commit()

    except Exception as e:
        print(f"[MODERATION PIPELINE ERROR] {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────
# HUMAN REVIEW QUEUE
# ─────────────────────────────────────────────

@router.get("/queue")
def get_queue(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    videos = db.query(Video).filter(
        Video.mod_status == ModerationStatus.flagged
    ).order_by(Video.created_at).all()

    return {
        "count": len(videos),
        "videos": [
            {
                "id":           v.id,
                "title":        v.title,
                "description":  v.description,
                "category":     v.category,
                "channel_name": v.creator.channel_name if v.creator else "",
                "thumbnail_url": v.thumbnail_url,
                "flags":        json.loads(v.mod_flags) if v.mod_flags else [],
                "created_at":   v.created_at.isoformat(),
                "video_type":   v.video_type.value,
            }
            for v in videos
        ]
    }


# ─────────────────────────────────────────────
# APPROVE
# ─────────────────────────────────────────────

@router.post("/approve/{video_id}")
def approve_video(
    video_id: str,
    req: ApproveRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    safety_map = {
        "safe_for_all": SafetyLevel.safe_for_all,
        "standard":     SafetyLevel.standard,
        "mature_ok":    SafetyLevel.mature_ok,
        "limited_ads":  SafetyLevel.limited_ads,
        "no_ads":       SafetyLevel.no_ads,
    }
    video.safety_level    = safety_map.get(req.safety_level, SafetyLevel.standard)
    video.mod_status      = ModerationStatus.approved
    video.status          = VideoStatus.published
    video.published_at    = datetime.utcnow()
    video.mod_reviewed_by = admin.id
    video.mod_reviewed_at = datetime.utcnow()
    video.mod_notes       = req.notes

    db.commit()

    creator_user = db.query(User).join(CreatorProfile).filter(
        CreatorProfile.id == video.creator_id
    ).first()
    if creator_user:
        send_video_approved_email(creator_user.email, creator_user.full_name, video.title)

    return {"approved": True, "safety_level": req.safety_level}


# ─────────────────────────────────────────────
# REJECT
# ─────────────────────────────────────────────

@router.post("/reject/{video_id}")
def reject_video(
    video_id: str,
    req: RejectRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    video.mod_status      = ModerationStatus.rejected
    video.status          = VideoStatus.mod_rejected
    video.mod_reviewed_by = admin.id
    video.mod_reviewed_at = datetime.utcnow()
    video.mod_notes       = req.notes

    db.commit()

    creator_user = db.query(User).join(CreatorProfile).filter(
        CreatorProfile.id == video.creator_id
    ).first()
    if creator_user:
        send_video_rejected_email(
            creator_user.email, creator_user.full_name,
            video.title, req.reason
        )

    return {"rejected": True}


# ─────────────────────────────────────────────
# CREATOR APPEAL
# ─────────────────────────────────────────────

@router.post("/appeal/{video_id}")
def submit_appeal(
    video_id: str,
    req: AppealRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Creator submits an appeal for a rejected video. One appeal per video."""
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    video   = db.query(Video).filter(
        Video.id == video_id, Video.creator_id == creator.id
    ).first()

    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.mod_status not in (ModerationStatus.rejected,):
        raise HTTPException(status_code=400, detail="This video has not been rejected")
    if video.appeal_statement:
        raise HTTPException(status_code=400, detail="You have already submitted an appeal for this video")
    if len(req.statement.strip()) < 20:
        raise HTTPException(status_code=422, detail="Appeal statement must be at least 20 characters")

    video.appeal_statement = req.statement.strip()
    video.appeal_at        = datetime.utcnow()
    video.mod_status       = ModerationStatus.appealed

    db.commit()
    return {"message": "Appeal submitted. We'll review it within 48 hours and email you the outcome."}


# ─────────────────────────────────────────────
# APPEALS QUEUE
# ─────────────────────────────────────────────

@router.get("/appeals")
def get_appeals(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    videos = db.query(Video).filter(
        Video.mod_status == ModerationStatus.appealed
    ).order_by(Video.appeal_at).all()

    return {
        "count": len(videos),
        "appeals": [
            {
                "id":                v.id,
                "title":             v.title,
                "channel_name":      v.creator.channel_name if v.creator else "",
                "original_flags":    json.loads(v.mod_flags) if v.mod_flags else [],
                "appeal_statement":  v.appeal_statement,
                "appeal_at":         v.appeal_at.isoformat() if v.appeal_at else None,
                "rejected_at":       v.mod_reviewed_at.isoformat() if v.mod_reviewed_at else None,
                "mod_notes":         v.mod_notes,
            }
            for v in videos
        ]
    }


# ─────────────────────────────────────────────
# RESOLVE APPEAL
# ─────────────────────────────────────────────

@router.post("/appeal/{video_id}/resolve")
def resolve_appeal(
    video_id: str,
    req: ResolveAppealRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if req.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="Decision must be 'approved' or 'rejected'")

    video = db.query(Video).filter(
        Video.id == video_id, Video.mod_status == ModerationStatus.appealed
    ).first()
    if not video:
        raise HTTPException(status_code=404, detail="Appeal not found")

    creator_user = db.query(User).join(CreatorProfile).filter(
        CreatorProfile.id == video.creator_id
    ).first()

    if req.decision == "approved":
        video.mod_status  = ModerationStatus.appeal_approved
        video.status      = VideoStatus.published
        video.safety_level = SafetyLevel.standard
        video.published_at = datetime.utcnow()
        if creator_user:
            send_video_approved_email(creator_user.email, creator_user.full_name, video.title)
    else:
        video.mod_status = ModerationStatus.appeal_rejected
        video.status     = VideoStatus.mod_rejected
        if creator_user:
            send_video_rejected_email(
                creator_user.email, creator_user.full_name,
                video.title,
                f"Appeal rejected. {req.notes or 'The original decision has been upheld.'}"
            )

    video.mod_reviewed_at = datetime.utcnow()
    video.mod_notes       = req.notes
    db.commit()

    return {"resolved": True, "decision": req.decision}


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────

@router.get("/stats")
def mod_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from sqlalchemy import func

    total     = db.query(func.count(Video.id)).scalar()
    approved  = db.query(func.count(Video.id)).filter(Video.mod_status == ModerationStatus.approved).scalar()
    auto_app  = db.query(func.count(Video.id)).filter(Video.mod_status == ModerationStatus.auto_approved).scalar()
    flagged   = db.query(func.count(Video.id)).filter(Video.mod_status == ModerationStatus.flagged).scalar()
    rejected  = db.query(func.count(Video.id)).filter(Video.mod_status == ModerationStatus.rejected).scalar()
    appealed  = db.query(func.count(Video.id)).filter(Video.mod_status == ModerationStatus.appealed).scalar()

    pass_rate = round(((approved + auto_app) / total * 100), 1) if total else 0

    return {
        "total":           total,
        "auto_approved":   auto_app,
        "human_approved":  approved,
        "flagged":         flagged,
        "rejected":        rejected,
        "appealed":        appealed,
        "pass_rate_pct":   pass_rate,
    }
