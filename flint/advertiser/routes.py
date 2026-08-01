from ..currency.service import CurrencyService, MIN_ADVERTISER_BUDGET_USD
"""
Flint — Advertiser API Routes

POST /api/advertiser/apply          — submit advertiser application
GET  /api/advertiser/profile        — get advertiser profile
GET  /api/advertiser/dashboard      — full dashboard stats
POST /api/advertiser/campaigns      — create a new campaign
GET  /api/advertiser/campaigns      — list campaigns
PATCH /api/advertiser/campaigns/{id} — update campaign (pause/resume/budget)
GET  /api/advertiser/campaigns/{id}/stats — campaign detailed stats
GET  /api/advertiser/reporting      — monthly report data
POST /api/advertiser/topup          — add budget (PayPal)

Admin only:
GET  /api/advertiser/admin/applications — pending applications
POST /api/advertiser/admin/approve/{id} — approve an advertiser
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    User, AdvertiserProfile, AdCampaign, AdImpression,
    CampaignStatus, AdFormat, Transaction, TxnType,
)
from ..auth.routes import require_verified, require_admin
from ..email.service import send_advertiser_approved_email, send_monthly_report_email

router = APIRouter(prefix="/advertiser", tags=["Advertiser"])

MIN_BUDGET_USD = 50000    # $500 minimum monthly budget


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class ApplyRequest(BaseModel):
    company_name:  str
    website:       str
    industry:      str
    description:   str
    contact_email: str
    monthly_budget: int         # USD cents (e.g. 50000 = $500.00)
    ad_format:     str = "preroll"
    target_niches: list[str] = []
    goal:          str = "Brand awareness"
    billing_method: str = "paypal"


class CreateCampaignRequest(BaseModel):
    name:           str
    format:         str = "preroll"
    target_niches:  list[str] = []
    goal:           str = "Brand awareness"
    budget_pence:   int
    click_url:      str = ""
    safety_required: str = "safe_for_all"
    cpm_pence:      int = 480   # default £4.80 CPM


class UpdateCampaignRequest(BaseModel):
    name:         Optional[str] = None
    status:       Optional[str] = None   # active | paused
    budget_pence: Optional[int] = None


class TopupRequest(BaseModel):
    amount_pence: int     # minimum 50_000 (£500)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_advertiser(user: User, db: Session, require_approved: bool = False) -> AdvertiserProfile:
    if user.role != "advertiser":
        raise HTTPException(status_code=403, detail="Advertiser account required")
    adv = db.query(AdvertiserProfile).filter(AdvertiserProfile.user_id == user.id).first()
    if not adv:
        raise HTTPException(status_code=404, detail="Advertiser profile not found")
    if require_approved and not adv.approved:
        raise HTTPException(status_code=403, detail="Advertiser account pending approval")
    return adv


def _campaign_dict(c: AdCampaign) -> dict:
    return {
        "id":             c.id,
        "name":           c.name,
        "format":         c.format.value,
        "target_niches":  json.loads(c.target_niches) if c.target_niches else [],
        "goal":           c.goal,
        "status":         c.status.value,
        "budget_pence":   c.budget_pence,
        "spent_pence":    c.spent_pence,
        "remaining":      c.budget_pence - c.spent_pence,
        "cpm_pence":      c.cpm_pence,
        "impressions":    c.impressions,
        "clicks":         c.clicks,
        "completions":    c.completions,
        "ctr":            round(c.clicks / c.impressions * 100, 2) if c.impressions else 0,
        "completion_rate": round(c.completions / c.impressions * 100, 2) if c.impressions else 0,
        "created_at":     c.created_at.isoformat(),
    }


# ─────────────────────────────────────────────
# APPLY
# ─────────────────────────────────────────────

@router.post("/apply", status_code=201)
def apply(
    req: ApplyRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    if user.role != "advertiser":
        raise HTTPException(status_code=403, detail="Sign up with the Advertiser role to apply")

    adv = db.query(AdvertiserProfile).filter(AdvertiserProfile.user_id == user.id).first()
    if not adv:
        raise HTTPException(status_code=404, detail="Advertiser profile not found")

    if req.monthly_budget < MIN_BUDGET_USD:
        raise HTTPException(
            status_code=422,
            detail="Minimum monthly budget is $500 USD"
        )

    adv.company_name  = req.company_name
    adv.website       = req.website
    adv.industry      = req.industry
    adv.description   = req.description
    adv.contact_email = req.contact_email
    db.commit()

    return {
        "message":      "Application received. We review all applications within 24 hours.",
        "company":      req.company_name,
        "contact":      req.contact_email,
        "next_steps": [
            "Brand safety review (24 hrs)",
            "Account setup email with login credentials",
            "Upload your creative in the dashboard",
            "Campaigns go live within hours",
        ]
    }


# ─────────────────────────────────────────────
# PROFILE
# ─────────────────────────────────────────────

@router.get("/profile")
def get_profile(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    adv = _get_advertiser(user, db, require_approved=False)
    return {
        "id":             adv.id,
        "company_name":   adv.company_name,
        "website":        adv.website,
        "industry":       adv.industry,
        "approved":       adv.approved,
        "contact_email":  adv.contact_email,
        "budget_balance": adv.budget_balance,
        "total_spent":    adv.total_spent,
    }


# ─────────────────────────────────────────────
# DASHBOARD STATS
# ─────────────────────────────────────────────

@router.get("/dashboard")
def get_dashboard(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    adv = _get_advertiser(user, db)

    campaigns = db.query(AdCampaign).filter(AdCampaign.advertiser_id == adv.id).all()

    total_impressions = sum(c.impressions for c in campaigns)
    total_clicks      = sum(c.clicks for c in campaigns)
    total_completions = sum(c.completions for c in campaigns)
    total_budget      = sum(c.budget_pence for c in campaigns)
    total_spent       = sum(c.spent_pence for c in campaigns)

    return {
        "summary": {
            "budget_balance":  adv.budget_balance,
            "total_budget":    total_budget,
            "total_spent":     total_spent,
            "total_impressions": total_impressions,
            "total_clicks":    total_clicks,
            "overall_ctr":     round(total_clicks / total_impressions * 100, 2) if total_impressions else 0,
            "completion_rate": round(total_completions / total_impressions * 100, 2) if total_impressions else 0,
        },
        "campaigns": [_campaign_dict(c) for c in campaigns],
        "next_report_date": (datetime.utcnow().replace(day=1) + timedelta(days=32)).replace(day=1).strftime("%B 1, %Y"),
    }


# ─────────────────────────────────────────────
# CREATE CAMPAIGN
# ─────────────────────────────────────────────

@router.post("/campaigns", status_code=201)
def create_campaign(
    req: CreateCampaignRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    adv = _get_advertiser(user, db)

    if req.budget_pence < MIN_BUDGET_USD:
        raise HTTPException(status_code=422, detail=f"Minimum campaign budget is $500 USD")

    if adv.budget_balance < req.budget_pence:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient budget. Balance: {CurrencyService.format(adv.budget_balance, 'USD')}, Required: {CurrencyService.format(req.budget_pence, 'USD')}"
        )

    fmt_map = {"preroll": AdFormat.preroll, "midroll": AdFormat.midroll, "display": AdFormat.display, "sponsored": AdFormat.sponsored}

    campaign = AdCampaign(
        advertiser_id   = adv.id,
        name            = req.name,
        format          = fmt_map.get(req.format, AdFormat.preroll),
        target_niches   = json.dumps(req.target_niches),
        goal            = req.goal,
        status          = CampaignStatus.active,
        budget_pence    = req.budget_pence,
        click_url       = req.click_url,
        cpm_pence       = req.cpm_pence,
        starts_at       = datetime.utcnow(),
    )
    db.add(campaign)

    # Reserve budget from advertiser balance
    adv.budget_balance -= req.budget_pence

    db.commit()
    db.refresh(campaign)

    return _campaign_dict(campaign)


# ─────────────────────────────────────────────
# LIST CAMPAIGNS
# ─────────────────────────────────────────────

@router.get("/campaigns")
def list_campaigns(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    adv       = _get_advertiser(user, db)
    campaigns = db.query(AdCampaign).filter(
        AdCampaign.advertiser_id == adv.id
    ).order_by(desc(AdCampaign.created_at)).all()
    return {"campaigns": [_campaign_dict(c) for c in campaigns]}


# ─────────────────────────────────────────────
# UPDATE CAMPAIGN
# ─────────────────────────────────────────────

@router.patch("/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: str,
    req: UpdateCampaignRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    adv      = _get_advertiser(user, db)
    campaign = db.query(AdCampaign).filter(
        AdCampaign.id == campaign_id, AdCampaign.advertiser_id == adv.id
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if req.name:
        campaign.name = req.name
    if req.status:
        status_map = {"active": CampaignStatus.active, "paused": CampaignStatus.paused}
        campaign.status = status_map.get(req.status, campaign.status)

    db.commit()
    return _campaign_dict(campaign)


# ─────────────────────────────────────────────
# CAMPAIGN STATS
# ─────────────────────────────────────────────

@router.get("/campaigns/{campaign_id}/stats")
def campaign_stats(
    campaign_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    adv      = _get_advertiser(user, db)
    campaign = db.query(AdCampaign).filter(
        AdCampaign.id == campaign_id, AdCampaign.advertiser_id == adv.id
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Top performing videos
    top_videos = db.query(
        AdImpression.video_id,
        func.count(AdImpression.id).label("impressions"),
        func.sum(AdImpression.completed.cast(int)).label("completions"),
        func.sum(AdImpression.clicked.cast(int)).label("clicks"),
    ).filter(
        AdImpression.campaign_id == campaign_id
    ).group_by(AdImpression.video_id).order_by(desc("impressions")).limit(10).all()

    return {
        "campaign":   _campaign_dict(campaign),
        "top_videos": [
            {
                "video_id":    row.video_id,
                "impressions": row.impressions,
                "completions": row.completions,
                "clicks":      row.clicks,
                "ctr":         round(row.clicks / row.impressions * 100, 2) if row.impressions else 0,
            }
            for row in top_videos
        ]
    }


# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

@router.get("/reporting")
def get_reporting(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    adv       = _get_advertiser(user, db)
    campaigns = db.query(AdCampaign).filter(AdCampaign.advertiser_id == adv.id).all()

    total_impressions = sum(c.impressions for c in campaigns)
    total_clicks      = sum(c.clicks for c in campaigns)
    total_completions = sum(c.completions for c in campaigns)
    total_spent       = sum(c.spent_pence for c in campaigns)

    return {
        "month": datetime.utcnow().strftime("%B %Y"),
        "stats": {
            "Total Impressions":   f"{total_impressions:,}",
            "Total Clicks":        f"{total_clicks:,}",
            "Overall CTR":         f"{round(total_clicks/total_impressions*100,2) if total_impressions else 0}%",
            "Avg Completion Rate": f"{round(total_completions/total_impressions*100,2) if total_impressions else 0}%",
            "Total Spend":         CurrencyService.format(total_spent, "USD"),
            "Avg CPM":             CurrencyService.format(int(total_spent/total_impressions*1000), "USD") if total_impressions else "$0",
        },
        "campaigns": [_campaign_dict(c) for c in campaigns],
    }


# ─────────────────────────────────────────────
# BUDGET TOP-UP
# ─────────────────────────────────────────────

@router.post("/topup")
def topup_budget(
    req: TopupRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """
    Returns PayPal order details for the advertiser to complete.
    After PayPal confirms payment, webhook at /api/payments/paypal/webhook
    credits the advertiser's budget_balance.
    """
    adv = _get_advertiser(user, db)

    if req.amount_pence < MIN_BUDGET_USD:
        raise HTTPException(status_code=422, detail=f"Minimum top-up is $500 USD")

    return {
        "amount_pence":  req.amount_pence,
        "amount_display": CurrencyService.format(req.amount_pence, "USD"),
        "paypal_order":  "Create PayPal order and redirect advertiser to approve",
        "return_url":    f"{os.getenv('FRONTEND_URL', 'https://flintx.tv')}/advertiser/topup/success",
        "cancel_url":    f"{os.getenv('FRONTEND_URL', 'https://flintx.tv')}/advertiser/topup/cancel",
        "message":       "Integrate PayPal Orders API to complete this flow",
    }


# ─────────────────────────────────────────────
# ADMIN — APPLICATIONS + APPROVE
# ─────────────────────────────────────────────

@router.get("/admin/applications")
def get_applications(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    pending = db.query(AdvertiserProfile).filter(
        AdvertiserProfile.approved == False,
        AdvertiserProfile.company_name.isnot(None),
    ).all()
    return {
        "count": len(pending),
        "applications": [
            {
                "id":           a.id,
                "company_name": a.company_name,
                "website":      a.website,
                "industry":     a.industry,
                "description":  a.description,
                "contact":      a.contact_email,
                "created_at":   a.created_at.isoformat(),
            }
            for a in pending
        ]
    }


@router.post("/admin/approve/{advertiser_id}")
def approve_advertiser(
    advertiser_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    adv = db.query(AdvertiserProfile).filter(AdvertiserProfile.id == advertiser_id).first()
    if not adv:
        raise HTTPException(status_code=404, detail="Advertiser not found")

    adv.approved    = True
    adv.approved_at = datetime.utcnow()
    db.commit()

    # Email them
    adv_user = db.query(User).filter(User.id == adv.user_id).first()
    if adv_user:
        send_advertiser_approved_email(adv_user.email, adv.company_name or "")

    return {"approved": True, "company": adv.company_name}
