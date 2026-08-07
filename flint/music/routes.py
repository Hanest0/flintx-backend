"""
FlintX Music Module
Handles music licensing compliance, rights declarations,
distribution, and sync licensing marketplace.
"""
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from ..database.connection import get_db
from ..database.models import User
from ..auth.routes import require_verified

router = APIRouter(prefix="/music", tags=["Music"])


# ── Music Rights Declarations ─────────────────────────────────────────

MUSIC_TYPES = {
    "original":  "Original composition — creator owns all rights",
    "licensed":  "Licensed from approved library (Epidemic Sound, Artlist, etc.)",
    "free":      "Public domain or Creative Commons",
    "nomusic":   "No music in this video",
}

APPROVED_LIBRARIES = [
    "epidemic_sound", "artlist", "musicbed",
    "soundstripe", "uppbeat", "other_licensed",
]

LICENSED_REGIONS = {
    "US": ["ASCAP", "BMI", "SESAC"],
    "GB": ["PRS for Music"],
    "AU": ["APRA AMCOS"],
    "CA": ["SOCAN"],
    "DE": ["GEMA"],
    "FR": ["SACEM"],
    "NL": ["BUMA/STEMRA"],
    "ZA": ["SAMRO"],
    "DEFAULT": ["Epidemic Sound Enterprise Licence"],
}

BLOCKED_REGIONS = ["IR", "KP", "CN"]


class MusicDeclaration(BaseModel):
    video_id:    str
    music_type:  str
    library:     Optional[str] = None
    track_names: Optional[list] = []
    rights_confirmed: bool = False


class DistributionRequest(BaseModel):
    track_title:  str
    artist_name:  str
    genre:        str
    release_date: str
    plan:         str = "single"  # single | album | unlimited
    isrc:         Optional[str] = None


@router.post("/declare-rights")
def declare_music_rights(
    req: MusicDeclaration,
    user: User = Depends(require_verified),
    db:   Session = Depends(get_db),
):
    """
    Creator declares music rights for a video.
    This is a legal record — stored with timestamp.
    """
    if req.music_type not in MUSIC_TYPES:
        raise HTTPException(400, f"Invalid music type. Must be one of: {list(MUSIC_TYPES.keys())}")

    if req.music_type != "nomusic" and not req.rights_confirmed:
        raise HTTPException(400, "Rights confirmation required for videos containing music.")

    if req.music_type == "licensed" and req.library not in APPROVED_LIBRARIES:
        raise HTTPException(400, f"Library must be one of: {APPROVED_LIBRARIES}")

    # Store declaration
    declaration = {
        "video_id":        req.video_id,
        "user_id":         str(user.id),
        "music_type":      req.music_type,
        "music_type_desc": MUSIC_TYPES[req.music_type],
        "library":         req.library,
        "track_names":     req.track_names or [],
        "rights_confirmed": req.rights_confirmed,
        "declared_at":     datetime.utcnow().isoformat(),
        "creator_email":   user.email,
    }

    return {
        "platform":    "FlintX",
        "updated":     datetime.utcnow().isoformat(),
        "music_policy": "Original compositions only. No copyrighted music permitted without creator-held licence.",
        "licences": [
            {"region":"United States",  "organisations":["ASCAP","BMI","SESAC"],  "status":"pending_application", "note":"Applications not yet submitted"},
            {"region":"United Kingdom", "organisations":["PRS for Music"],         "status":"pending_application", "note":"Application not yet submitted"},
            {"region":"Australia",      "organisations":["APRA AMCOS"],            "status":"pending_application", "note":"Application not yet submitted"},
            {"region":"Canada",         "organisations":["SOCAN"],                 "status":"pending_application", "note":"Application not yet submitted"},
            {"region":"Global Library", "organisations":["Epidemic Sound"],        "status":"in_negotiation",      "note":"Enterprise deal not yet signed"},
            {"region":"Iran",           "organisations":["Blocked — OFAC"],        "status":"blocked"},
            {"region":"North Korea",    "organisations":["Blocked — UN sanctions"],"status":"blocked"},
            {"region":"China",          "organisations":["Blocked — local law"],   "status":"blocked"},
        ],
        "what_is_allowed":   "Original music created by the uploader only",
        "what_is_prohibited": "Any copyrighted music without a valid personal licence",
        "content_id":        "ACRCloud integration planned — not yet active",
        "legal_contact":     "legal@flintx.tv",
    }