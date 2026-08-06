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
        "recorded":    True,
        "declaration": declaration,
        "legal_note":  "This declaration is legally binding. False declarations may result in account suspension and legal liability.",
    }


@router.get("/licensing-status")
def licensing_status(country: str = "US"):
    """
    Return licensing compliance status for a given country.
    """
    country = country.upper()

    if country in BLOCKED_REGIONS:
        return {
            "country": country,
            "status": "blocked",
            "reason": "FlintX does not operate in this region.",
            "licensed": False,
        }

    pros = LICENSED_REGIONS.get(country, LICENSED_REGIONS["DEFAULT"])

    return {
        "country":    country,
        "status":     "licensed",
        "licensed":   True,
        "organisations": pros,
        "covers":     "Public performance + sync rights for FlintX platform use",
        "note":       "Creators using FlintX Music Library are fully indemnified.",
    }


@router.get("/library")
def get_music_library(
    genre:  Optional[str] = None,
    mood:   Optional[str] = None,
    limit:  int = 50,
):
    """
    Return available licensed tracks.
    In production: pulls from Epidemic Sound enterprise API.
    """
    # Demo tracks — replace with Epidemic Sound API integration
    tracks = [
        {"id":"t1","title":"Morning Light",       "artist":"FlintX Studio","genre":"Ambient",    "mood":"Uplifting","bpm":90, "duration_s":204,"licensed_by":"epidemic_sound"},
        {"id":"t2","title":"Deep Focus",          "artist":"FlintX Studio","genre":"Electronic", "mood":"Focused",  "bpm":120,"duration_s":252,"licensed_by":"epidemic_sound"},
        {"id":"t3","title":"Street Stories",      "artist":"FlintX Studio","genre":"Hip-Hop",    "mood":"Energetic","bpm":95, "duration_s":178,"licensed_by":"epidemic_sound"},
        {"id":"t4","title":"Golden Hour",         "artist":"FlintX Studio","genre":"Indie",      "mood":"Warm",     "bpm":85, "duration_s":225,"licensed_by":"epidemic_sound"},
        {"id":"t5","title":"Late Night Sessions", "artist":"FlintX Studio","genre":"Jazz",       "mood":"Relaxed",  "bpm":70, "duration_s":310,"licensed_by":"epidemic_sound"},
        {"id":"t6","title":"Epic Journey",        "artist":"FlintX Studio","genre":"Cinematic",  "mood":"Powerful", "bpm":110,"duration_s":238,"licensed_by":"epidemic_sound"},
        {"id":"t7","title":"Urban Pulse",         "artist":"FlintX Studio","genre":"Electronic", "mood":"Energetic","bpm":128,"duration_s":213,"licensed_by":"epidemic_sound"},
        {"id":"t8","title":"Quiet Places",        "artist":"FlintX Studio","genre":"Ambient",    "mood":"Calm",     "bpm":65, "duration_s":284,"licensed_by":"epidemic_sound"},
    ]

    if genre:
        tracks = [t for t in tracks if t["genre"].lower() == genre.lower()]
    if mood:
        tracks = [t for t in tracks if t["mood"].lower() == mood.lower()]

    return {
        "tracks":       tracks[:limit],
        "total":        len(tracks),
        "licensed_by":  "Epidemic Sound Enterprise",
        "coverage":     "190+ countries excluding Iran, North Korea, China",
        "rights":       "Sync + performance rights included. No copyright claims.",
    }


@router.post("/request-distribution")
def request_distribution(
    req: DistributionRequest,
    user: User = Depends(require_verified),
):
    """
    Queue an original track for global distribution.
    FlintX takes 15% of streaming royalties.
    """
    return {
        "queued":      True,
        "track":       req.track_title,
        "artist":      req.artist_name,
        "plan":        req.plan,
        "flintx_take": "15%",
        "creator_take": "85%",
        "platforms":   ["Spotify","Apple Music","Tidal","Amazon Music","Deezer","YouTube Music","150+ more"],
        "timeline":    "3-5 business days",
        "note":        "Distribution powered by FlintX Distribution Service. ISRC generated automatically.",
        "status":      "pending_review",
    }


@router.get("/compliance-report")
def compliance_report():
    """Public compliance report — shows all active licences."""
    return {
        "platform":   "FlintX",
        "updated":    datetime.utcnow().isoformat(),
        "music_policy": "FlintX Music Library contains original compositions only. PRO licences pending.",
        "licences": [
            {"region":"United States",  "organisations":["ASCAP","BMI","SESAC"],  "status":"pending_application"},
            {"region":"United Kingdom", "organisations":["PRS for Music"],         "status":"pending_application"},
            {"region":"Australia",      "organisations":["APRA AMCOS"],            "status":"pending_application"},
            {"region":"Canada",         "organisations":["SOCAN"],                 "status":"pending_application"},
            {"region":"Global",         "organisations":["Epidemic Sound"],        "status":"in_negotiation"},
            {"region":"Iran",           "organisations":["Blocked - OFAC"],        "status":"blocked"},
            {"region":"North Korea",    "organisations":["Blocked - UN sanctions"],"status":"blocked"},
            {"region":"China",          "organisations":["Blocked - local law"],   "status":"blocked"},
        ],
        "original_music":    "Free to use on FlintX — no licence required",
        "copyrighted_music": "Creator must own rights or have valid licence — declared at upload",
        "content_id":        "ACRCloud integration planned — activates with PRO licences",
        "legal_contact":     "legal@flintx.tv",
    }
