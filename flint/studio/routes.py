"""
Flint — Studio API Routes
AI tools for creators. All wrap external APIs.

POST /api/studio/script         — generate a video script (OpenAI)
POST /api/studio/voice          — generate voiceover audio (ElevenLabs)
POST /api/studio/opportunity    — find trending topics (OpenAI)
POST /api/studio/predict        — revenue prediction (pure Flint data, no AI)
GET  /api/studio/plan           — current studio plan
POST /api/studio/plan/upgrade   — upgrade studio plan (starts PayPal flow)
GET  /api/studio/tools          — tools available/locked for this creator
"""

import os
import json
import httpx
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from fastapi import Query

from ..database.connection import get_db
from ..database.models import User, CreatorProfile, StudioPlan
from ..auth.routes import require_verified

router = APIRouter(prefix="/studio", tags=["Studio"])

OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_IDS = {
    "Professional Male":   "21m00Tcm4TlvDq8ikWAM",
    "Professional Female": "EXAVITQu4vr4xnSDxMaL",
    "Energetic Male":      "AZnzlk1XvdvUeBnXmlld",
    "Calm Narrator":       "VR6AewLTigWG4xSOukaG",
    "British Female":      "MF3mGyEYCl7XYWbV9V6O",
}

# Which tools are included in each plan
PLAN_TOOLS = {
    StudioPlan.none:   ["script", "voice", "predictor"],
    StudioPlan.basic:  ["script", "voice", "predictor", "opportunity", "thumbnail"],
    StudioPlan.pro:    ["script", "voice", "predictor", "opportunity", "thumbnail", "editor", "brand"],
    StudioPlan.agency: ["script", "voice", "predictor", "opportunity", "thumbnail", "editor", "brand", "clip_gen", "translation"],
}

# All available tools with metadata
ALL_TOOLS = [
    {"id": "script",      "name": "Script Writer",     "price_usd_cents": 0,    "icon": "✍️",  "free": True},
    {"id": "voice",       "name": "Voice Generator",   "price_usd_cents": 0,    "icon": "🎙️", "free": True},
    {"id": "predictor",   "name": "Revenue Predictor", "price_usd_cents": 0,    "icon": "🔮",  "free": True},
    {"id": "opportunity", "name": "Opportunity AI",    "price_usd_cents": 1400, "icon": "🔍"},
    {"id": "thumbnail",   "name": "Thumbnail Maker",   "price_usd_cents": 900,  "icon": "🖼️"},
    {"id": "editor",      "name": "Video Editor",      "price_usd_cents": 1900, "icon": "✂️"},
    {"id": "brand",       "name": "Brand Studio",      "price_usd_cents": 1100, "icon": "🎨"},
    {"id": "clip_gen",    "name": "Clip Generator",    "price_usd_cents": 1600, "icon": "📱"},
    {"id": "translation", "name": "Translation AI",    "price_usd_cents": 2200, "icon": "🌐"},
]

# Revenue thresholds to unlock tools automatically
UNLOCK_AT_PENCE = {
    "editor":      100_000,   # $1,000/mo
    "clip_gen":    150_000,   # $1,500/mo
    "translation": 250_000,   # $2,500/mo
}

# Niche CPM data — imported from currency service (USD cents)
from ..currency.service import NICHE_CPM_USD_CENTS as NICHE_CPM, CurrencyService, SUBSCRIPTION_PRICES_USD


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class ScriptRequest(BaseModel):
    topic:      str
    style:      str = "long_form"     # long_form | short_form | medium
    niche:      str = ""
    audience:   str = "general"

class VoiceRequest(BaseModel):
    script:     str
    voice_name: str = "Professional Male"
    speed:      float = 1.0

class OpportunityRequest(BaseModel):
    niche:   str
    channel: str = ""

class PredictRequest(BaseModel):
    niche:        str
    subscribers:  int
    duration_min: int = 10
    uploads_week: int = 2
    quality:      str = "good"        # viral | great | good | average | weak

class PlanUpgradeRequest(BaseModel):
    plan:   str     # basic | pro | agency
    period: str = "monthly"   # monthly | annual


# ─────────────────────────────────────────────
# HELPER — check creator has tool access
# ─────────────────────────────────────────────

def _get_creator(user: User, db: Session) -> CreatorProfile:
    if user.role not in ("creator", "both", "admin"):
        raise HTTPException(status_code=403, detail="Creator account required")
    creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator profile not found")
    return creator


def _has_tool(creator: CreatorProfile, tool_id: str) -> bool:
    plan_tools = PLAN_TOOLS.get(creator.studio_plan, PLAN_TOOLS[StudioPlan.none])
    return tool_id in plan_tools


# ─────────────────────────────────────────────
# SCRIPT WRITER
# ─────────────────────────────────────────────

@router.post("/script")
def generate_script(
    req: ScriptRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = _get_creator(user, db)
    # Script writer is free — no plan check needed

    if not OPENAI_API_KEY:
        # Dev mode — return a template script
        return _demo_script(req.topic, req.style)

    style_instructions = {
        "long_form":  "Write a detailed 8–12 minute YouTube video script with a strong hook, clear sections, examples, and a CTA. Include timestamps.",
        "short_form": "Write a punchy 45–60 second vertical video script. Lead with the hook in the first 3 words. High energy throughout.",
        "medium":     "Write a focused 4–6 minute video script. Tight structure, one main point per section.",
    }.get(req.style, "Write a video script.")

    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "system",
                        "content": f"""You are an expert YouTube scriptwriter who specialises in high-performing
                        {req.niche or 'educational'} content. You write in a direct, engaging, conversational tone.
                        Your scripts consistently achieve above-average completion rates because they respect the viewer's time.
                        {style_instructions}
                        Format: Use clear section headers in [BRACKETS]. Include estimated timestamps.
                        End with a quality score (0–100) and estimated video duration."""
                    },
                    {
                        "role": "user",
                        "content": f"Write a complete script for: {req.topic}"
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.7,
            },
            timeout=30,
        )
        result = resp.json()
        script = result["choices"][0]["message"]["content"]
        return {"script": script, "topic": req.topic, "style": req.style}
    except Exception as e:
        print(f"[OPENAI SCRIPT ERROR] {e}")
        return _demo_script(req.topic, req.style)


def _demo_script(topic: str, style: str) -> dict:
    return {
        "script": f"""[HOOK — 0:00–0:15]
What if I told you most people approach "{topic}" completely wrong?

[INTRO — 0:15–1:30]
I've spent two years testing every strategy in this space. Today I'm giving you only what actually works — no filler, no padding.

[MAIN CONTENT — 1:30–9:00]
Let's start with what nobody in this space wants to admit...
[Full script generated here in production — connect your OpenAI API key]

[CTA — 11:30–11:42]
Subscribe — I post every Tuesday. Drop your biggest question in the comments.

✓ Quality Score: 91/100 · ~12:00 estimated duration""",
        "topic": topic,
        "style": style,
        "note": "Demo output — add OPENAI_API_KEY to .env for real generation",
    }


# ─────────────────────────────────────────────
# VOICE GENERATOR
# ─────────────────────────────────────────────

@router.post("/voice")
def generate_voice(
    req: VoiceRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = _get_creator(user, db)

    word_count = len(req.script.split())
    if word_count > 5000:
        raise HTTPException(status_code=422, detail="Script too long — maximum 5,000 words")

    voice_id = ELEVENLABS_VOICE_IDS.get(req.voice_name, list(ELEVENLABS_VOICE_IDS.values())[0])

    if not ELEVENLABS_API_KEY:
        # Dev mode
        return {
            "audio_url": None,
            "duration_s": word_count // 2,
            "voice":      req.voice_name,
            "word_count": word_count,
            "note":       "Demo mode — add ELEVENLABS_API_KEY to .env for real audio",
        }

    try:
        resp = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={
                "text": req.script[:5000],
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "speed": req.speed},
            },
            timeout=60,
        )

        if resp.status_code != 200:
            raise Exception(f"ElevenLabs error: {resp.status_code}")

        # In production: upload audio bytes to S3 and return presigned URL
        # For now return success with metadata
        return {
            "audio_url":  None,           # set after S3 upload in production
            "audio_bytes": len(resp.content),
            "duration_s": word_count // 2,
            "voice":      req.voice_name,
            "word_count": word_count,
        }

    except Exception as e:
        print(f"[ELEVENLABS ERROR] {e}")
        raise HTTPException(status_code=500, detail="Voice generation failed. Try again.")


# ─────────────────────────────────────────────
# OPPORTUNITY AI
# ─────────────────────────────────────────────

@router.post("/opportunity")
def find_opportunities(
    req: OpportunityRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    creator = _get_creator(user, db)

    if not OPENAI_API_KEY:
        return _demo_opportunities(req.niche)

    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "system",
                        "content": """You are a YouTube growth strategist who identifies high-opportunity video topics.
                        For each topic you identify:
                        - Trend score (1–100): how much search/interest momentum it has right now
                        - Competition (Low/Medium/High): how saturated YouTube is with this topic
                        - Ad value (Low/Medium/High): CPM category based on advertiser spend
                        - Hook: the exact first line that would make someone click
                        - Recommendation: CREATE NOW, CREATE, or WAIT

                        Respond ONLY with a JSON array of 5 objects with keys:
                        rank, topic, trend, competition, ad_value, hook, recommendation
                        No other text."""
                    },
                    {
                        "role": "user",
                        "content": f"Find 5 high-opportunity video topics for a {req.niche} creator in 2025. Channel context: {req.channel or 'general ' + req.niche + ' channel'}."
                    }
                ],
                "max_tokens": 1000,
                "temperature": 0.6,
            },
            timeout=20,
        )
        raw  = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(raw.strip().lstrip("```json").rstrip("```"))
        return {"opportunities": data, "niche": req.niche}

    except Exception as e:
        print(f"[OPPORTUNITY AI ERROR] {e}")
        return _demo_opportunities(req.niche)


def _demo_opportunities(niche: str) -> dict:
    return {
        "opportunities": [
            {"rank": 1, "topic": f"AI Tools for Beginners in {niche}", "trend": 94, "competition": "Medium", "ad_value": "High", "hook": f'"I tested 12 AI tools — only 3 worth your time"', "recommendation": "CREATE NOW"},
            {"rank": 2, "topic": f"{niche} Mistakes That Cost People Money", "trend": 87, "competition": "Low", "ad_value": "High", "hook": f'"The {niche} mistake that cost me $5,000"', "recommendation": "CREATE NOW"},
            {"rank": 3, "topic": f"{niche} in 2025 — What Actually Changed", "trend": 82, "competition": "Medium", "ad_value": "Medium", "hook": f'"Everything you knew about {niche} is outdated"', "recommendation": "CREATE"},
        ],
        "niche": niche,
        "note": "Demo output — add OPENAI_API_KEY to .env for real results",
    }


# ─────────────────────────────────────────────
# REVENUE PREDICTOR
# ─────────────────────────────────────────────

@router.post("/predict")
def predict_revenue(
    req: PredictRequest,
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
):
    """
    Pure Flint data — no external AI. Built from platform CPM data.
    All amounts stored as USD cents internally. Display in any currency via ?currency=GBP.
    The only revenue predictor using actual Flint earnings data.
    """
    currency = currency.upper()
    # Look up CPM — handle both exact match and case-insensitive match
    cpm_usd = NICHE_CPM.get(req.niche)
    if cpm_usd is None:
        # Try case-insensitive match
        niche_lower = req.niche.lower()
        cpm_usd = next((v for k,v in NICHE_CPM.items() if k.lower()==niche_lower), 380)

    subs = max(req.subscribers, 100)
    if subs < 10_000:       view_rate = 0.12
    elif subs < 100_000:    view_rate = 0.07
    elif subs < 500_000:    view_rate = 0.05
    else:                   view_rate = 0.03

    estimated_views = int(subs * view_rate)

    q_mult = {"viral": 2.4, "great": 1.5, "good": 1.0, "average": 0.7, "weak": 0.45}.get(req.quality, 1.0)

    dur = max(req.duration_min, 1)
    if dur < 5:       d_mult = 0.60
    elif dur < 8:     d_mult = 0.85
    elif dur < 15:    d_mult = 1.00
    elif dur < 20:    d_mult = 1.20
    else:             d_mult = 1.35

    flint_premium   = 1.18   # FlintX Pass opted-in viewers = better ad completion

    adjusted_views  = int(estimated_views * q_mult * d_mult)
    gross_usd       = int((adjusted_views / 1000) * cpm_usd * flint_premium)
    creator_usd     = int(gross_usd * 0.80)
    youtube_usd     = int((adjusted_views / 1000) * cpm_usd * 0.55)   # YouTube 55%, no premium

    low  = int(creator_usd * 0.65)
    mid  = creator_usd
    high = int(creator_usd * 1.60)

    uploads_month   = req.uploads_week * 4
    monthly_mid     = mid * uploads_month
    annual_estimate = monthly_mid * 12

    score = min(100, int(
        min(subs / 100_000, 1) * 30 +
        (25 if 8 <= dur <= 20 else 15 if dur >= 5 else 8) +
        (25 if req.uploads_week >= 2 else 15 if req.uploads_week >= 1 else 5) +
        (20 if req.quality == "viral" else 16 if req.quality == "great" else 12 if req.quality == "good" else 7)
    ))

    def fmt(usd_cents: int) -> dict:
        return CurrencyService.to_display(usd_cents, currency)

    return {
        "currency":           currency,
        "niche":              req.niche,
        "cpm":                fmt(int(cpm_usd * flint_premium)),
        "estimated_views":    adjusted_views,
        "per_video": {
            "low":   fmt(low),
            "mid":   fmt(mid),
            "high":  fmt(high),
        },
        "monthly_estimate":   fmt(monthly_mid),
        "annual_estimate":    fmt(annual_estimate),
        "vs_youtube": {
            "flint_per_video":   fmt(mid),
            "youtube_per_video": fmt(youtube_usd),
            "advantage":         fmt(mid - youtube_usd),
            "advantage_pct":     round((mid - youtube_usd) / max(youtube_usd, 1) * 100, 1),
        },
        "breakdowns": {
            "adjusted_views":    adjusted_views,
            "effective_cpm":     fmt(int(cpm_usd * flint_premium)),
            "gross_revenue":     fmt(gross_usd),
            "creator_share_80":  fmt(creator_usd),
            "platform_share_20": fmt(gross_usd - creator_usd),
        },
        "revenue_score": score,
        "score_advice": (
            "Strong. Your niche CPM, audience, and upload frequency are working together."
            if score >= 80 else
            "Good. Upload more consistently or target 10+ minute videos to improve."
            if score >= 60 else
            "Growing. Focus on reaching 10K subscribers and uploading at least twice a week."
        ),
    }


# ─────────────────────────────────────────────
# PLAN MANAGEMENT
# ─────────────────────────────────────────────

# All prices in USD cents
PLAN_PRICES = {
    ("basic",  "monthly"): 2900,    # $29.00
    ("pro",    "monthly"): 5900,    # $59.00
    ("agency", "monthly"): 14900,   # $149.00
    ("basic",  "annual"):  23200,   # $232.00 ($19.33/mo)
    ("pro",    "annual"):  47200,   # $472.00 ($39.33/mo)
    ("agency", "annual"):  119200,  # $1,192.00 ($99.33/mo)
}


@router.get("/plan")
def get_plan(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    creator = _get_creator(user, db)
    return {
        "plan":         creator.studio_plan.value,
        "expires":      creator.studio_plan_expires.isoformat() if creator.studio_plan_expires else None,
        "tools_active": (creator.tools_active or "").split(","),
    }


@router.post("/plan/upgrade")
def upgrade_plan(
    req: PlanUpgradeRequest,
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Returns PayPal subscription details. PayPal charges in USD.
    Display price converts to user's local currency for reference.
    """
    creator  = _get_creator(user, db)
    currency = currency.upper()

    if req.plan not in ("basic", "pro", "agency"):
        raise HTTPException(status_code=422, detail="Invalid plan")
    if req.period not in ("monthly", "annual"):
        raise HTTPException(status_code=422, detail="Invalid period")

    price_usd = PLAN_PRICES.get((req.plan, req.period))
    if not price_usd:
        raise HTTPException(status_code=422, detail="Invalid plan/period combination")

    return {
        "plan":              req.plan,
        "period":            req.period,
        "price_usd_cents":   price_usd,
        "price_usd":         CurrencyService.format(price_usd, "USD"),
        "price_local":       CurrencyService.format(price_usd, currency),
        "local_currency":    currency,
        "paypal_plan_id":    os.getenv(f"PAYPAL_PLAN_STUDIO_{req.plan.upper()}_{req.period[0].upper()}", ""),
        "note":              "PayPal charges in USD. Your bank may apply a currency conversion fee.",
    }


# ─────────────────────────────────────────────
# TOOLS LIST
# ─────────────────────────────────────────────

@router.get("/tools")
def get_tools(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    creator  = _get_creator(user, db)
    plan     = creator.studio_plan
    unlocked = PLAN_TOOLS.get(plan, PLAN_TOOLS[StudioPlan.none])
    rev_monthly = creator.monthly_ad_revenue   # pence

    tools = []
    for t in ALL_TOOLS:
        unlock_threshold = UNLOCK_AT_PENCE.get(t["id"])
        auto_unlocked    = unlock_threshold and rev_monthly >= unlock_threshold
        owned            = t["id"] in unlocked or auto_unlocked

        tools.append({
            **t,
            "owned":          owned,
            "auto_unlocked":  auto_unlocked,
            "unlock_at":      unlock_threshold,
            "your_revenue":   rev_monthly,
            "qualifies":      unlock_threshold and rev_monthly >= (unlock_threshold * 0.8),
        })

    return {"tools": tools, "plan": plan.value, "monthly_revenue_pence": rev_monthly}
