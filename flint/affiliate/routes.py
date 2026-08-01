"""
FlintX — Affiliate Marketing & AdSense Routes

Creator Affiliate:
  POST /api/affiliate/join                — creator joins the programme
  GET  /api/affiliate/dashboard           — affiliate earnings dashboard
  POST /api/affiliate/links               — create an affiliate link
  GET  /api/affiliate/links               — list all my links
  PATCH /api/affiliate/links/{id}         — update a link
  DELETE /api/affiliate/links/{id}        — deactivate a link
  GET  /api/affiliate/links/{id}/stats    — link performance stats

Public Redirect (no auth):
  GET  /go/{code}                         — redirect to affiliate URL (tracks click)
  GET  /go/p/{code}                       — platform affiliate redirect

Webhooks (from affiliate networks):
  POST /api/affiliate/webhook/amazon      — Amazon Associates conversion
  POST /api/affiliate/webhook/impact      — Impact conversion
  POST /api/affiliate/webhook/shareasale  — ShareASale conversion
  POST /api/affiliate/webhook/generic     — generic conversion webhook

AdSense:
  GET  /api/adsense/config                — get AdSense config (admin)
  POST /api/adsense/config                — set AdSense publisher ID (admin)
  GET  /api/adsense/units                 — get all ad unit IDs
  GET  /api/adsense/snippet/{placement}   — get the HTML snippet for a placement
  POST /api/adsense/sync                  — sync revenue from AdSense API (admin)
  GET  /api/adsense/revenue               — AdSense revenue report

Platform Affiliate Products:
  GET  /api/affiliate/products            — products for a niche (public)
  POST /api/affiliate/products            — add platform affiliate product (admin)
  PATCH /api/affiliate/products/{id}      — update product (admin)

Admin:
  GET  /api/affiliate/admin/overview      — full programme overview
  GET  /api/affiliate/admin/pending-pay   — unpaid creator commissions
  POST /api/affiliate/admin/pay/{id}      — mark commission paid
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import User, Transaction, TxnType, Video
from ..auth.routes import require_verified, require_admin
from ..currency.service import CurrencyService, NICHE_SUPERCATEGORIES
from .models import (
    AffiliateProgram, AffiliateLink, AffiliateClick, AffiliateEarning,
    PlatformAffiliateProduct, AdSenseConfig, AdSenseSnapshot,
    AffiliateNetwork, AffiliateProgramStatus, AffiliateClickStatus, AdSensePlacement,
)

router = APIRouter(tags=["Affiliate & AdSense"])

CREATOR_SHARE_PCT  = 85   # creator keeps 85% of affiliate commission
PLATFORM_SHARE_PCT = 15   # FlintX keeps 15%

# Preset platform affiliate products — added on launch
LAUNCH_PRODUCTS = [
    # Finance & Business
    {"name":"Trading 212",         "network":"custom",      "target_niches":["Personal Finance","Investing","Cryptocurrency"],   "description":"Commission-free investing. Stocks, ETFs, and PIE investing.",    "commission_type":"flat","commission_rate":0,"price_display":"Free"},
    {"name":"NordVPN",             "network":"impact",      "target_niches":["Technology","Cybersecurity","AI & Machine Learning","Software Development"], "description":"#1 rated VPN. 30-day money-back guarantee.", "commission_type":"percentage","commission_rate":0.40,"price_display":"From $3.99/mo"},
    {"name":"Brilliant.org",       "network":"impact",      "target_niches":["Online Learning","Mathematics","Science & Nature","Data Science"], "description":"Learn maths, science, and CS with hands-on problem solving.", "commission_type":"percentage","commission_rate":0.25,"price_display":"$15.99/mo"},
    {"name":"Skillshare",          "network":"impact",      "target_niches":["Online Learning","Drawing & Art","Photography","Writing","Video Production"], "description":"Online classes from industry experts. 2 months free.", "commission_type":"flat","commission_rate":0,"price_display":"$14/mo"},
    {"name":"Squarespace",         "network":"impact",      "target_niches":["Entrepreneurship","Web Development","Photography","Fashion"], "description":"Build a beautiful website. 14-day free trial.", "commission_type":"percentage","commission_rate":0.20,"price_display":"From $13/mo"},
    {"name":"Hostinger",           "network":"shareasale",  "target_niches":["Web Development","Software Development","Entrepreneurship"], "description":"Web hosting from $1.99/mo. 30-day money-back.", "commission_type":"percentage","commission_rate":0.60,"price_display":"From $1.99/mo"},
    {"name":"Amazon Associates",   "network":"amazon",      "target_niches":["Hardware & Reviews","Home Improvement","Cooking & Recipes","DIY & Crafts","Pets & Animals"], "description":"Millions of products. Earn up to 10% commission.", "commission_type":"percentage","commission_rate":0.06,"price_display":"Varies"},
    {"name":"Aura Identity",       "network":"impact",      "target_niches":["Personal Finance","Cybersecurity","Insurance"],    "description":"All-in-one identity protection and security.",                    "commission_type":"flat","commission_rate":0,"price_display":"$12/mo"},
    {"name":"Shopify",             "network":"impact",      "target_niches":["Entrepreneurship","Fashion","Food & Drink"],       "description":"Start your online store. 3 days free, then from $1/mo.",       "commission_type":"flat","commission_rate":0,"price_display":"From $29/mo"},
    {"name":"Notion",              "network":"partnerstack","target_niches":["Productivity","Software Development","Online Learning","Writing"],   "description":"All-in-one workspace. Notes, docs, wikis, databases.", "commission_type":"percentage","commission_rate":0.50,"price_display":"Free–$16/mo"},
]


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class CreateLinkRequest(BaseModel):
    name:                str
    original_url:        str
    network:             str = "custom"
    product_name:        str = ""
    product_description: str = ""
    product_price:       str = ""
    product_category:    str = ""
    commission_type:     str = "percentage"
    commission_rate:     float = 0.0
    commission_flat:     int = 0

class UpdateLinkRequest(BaseModel):
    name:             Optional[str] = None
    product_name:     Optional[str] = None
    product_price:    Optional[str] = None
    commission_rate:  Optional[float] = None
    active:           Optional[bool] = None

class AdSenseConfigRequest(BaseModel):
    publisher_id:         str
    mode:                 str = "auto"
    auto_ads_enabled:     bool = True
    unit_watch_sidebar:   str = ""
    unit_watch_below:     str = ""
    unit_browse_banner:   str = ""
    unit_browse_inline:   str = ""
    unit_channel_page:    str = ""
    unit_search_results:  str = ""
    unit_live_sidebar:    str = ""

class AddProductRequest(BaseModel):
    name:             str
    network:          str
    affiliate_url:    str
    image_url:        str = ""
    description:      str = ""
    price_display:    str = ""
    target_niches:    list[str] = []
    commission_type:  str = "percentage"
    commission_rate:  float = 0.0
    priority:         int = 0

class ConversionWebhook(BaseModel):
    order_id:          str
    click_reference:   str   # our session_id or short_code
    order_value:       float  # USD
    commission:        float  # USD


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _gen_short_code(length: int = 8) -> str:
    return secrets.token_urlsafe(length)[:length].replace("-","").replace("_","")


def _link_dict(link: AffiliateLink, currency: str = "USD") -> dict:
    return {
        "id":               link.id,
        "name":             link.name,
        "short_code":       link.short_code,
        "flintx_url":       f"https://flintx.tv/go/{link.short_code}",
        "original_url":     link.original_url,
        "network":          link.network.value if link.network else "custom",
        "product_name":     link.product_name,
        "product_price":    link.product_price,
        "product_category": link.product_category,
        "commission_type":  link.commission_type,
        "commission_rate":  f"{link.commission_rate*100:.0f}%" if link.commission_type=="percentage" else CurrencyService.format(link.commission_flat, currency),
        "total_clicks":     link.total_clicks,
        "total_conversions": link.total_conversions,
        "total_revenue":    CurrencyService.to_display(link.total_revenue_cents, currency),
        "your_earnings":    CurrencyService.to_display(link.creator_earned_cents, currency),
        "active":           link.active,
        "created_at":       link.created_at.isoformat(),
    }


def _get_or_create_adsense_config(db: Session) -> AdSenseConfig:
    cfg = db.query(AdSenseConfig).filter(AdSenseConfig.id == 1).first()
    if not cfg:
        cfg = AdSenseConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


# ─────────────────────────────────────────────
# CREATOR AFFILIATE PROGRAMME
# ─────────────────────────────────────────────

@router.post("/affiliate/join", status_code=201)
def join_affiliate_programme(
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    """Creator joins the FlintX affiliate programme."""
    if user.role not in ("creator", "both", "admin"):
        raise HTTPException(status_code=403, detail="Creator account required")

    existing = db.query(AffiliateProgram).filter(
        AffiliateProgram.creator_id == user.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="You already have an affiliate programme account")

    programme = AffiliateProgram(creator_id=user.id)
    db.add(programme)
    db.commit()
    db.refresh(programme)

    return {
        "joined":       True,
        "creator_share": f"{programme.creator_share_pct}%",
        "platform_share": f"{programme.platform_share_pct}%",
        "message": "You're in. Create affiliate links below. FlintX wraps every link with its own tracker — when a viewer clicks and buys, you earn 85% of the commission.",
        "how_it_works": [
            "Create a link below and paste the affiliate URL from your network (Amazon, Impact, ShareASale, etc.)",
            "Use flintx.tv/go/{code} in your video descriptions instead of the raw affiliate URL",
            "FlintX tracks every click and conversion — no spreadsheets, no guessing",
            "Commissions split automatically: 85% to your wallet, 15% to FlintX",
            "Payouts with your regular FlintX earnings — no separate payout process",
        ],
    }


@router.get("/affiliate/dashboard")
def affiliate_dashboard(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    programme = db.query(AffiliateProgram).filter(
        AffiliateProgram.creator_id == user.id
    ).first()

    if not programme:
        return {
            "enrolled": False,
            "message": "Join the affiliate programme to start earning commissions.",
        }

    links = db.query(AffiliateLink).filter(
        AffiliateLink.creator_id == user.id,
        AffiliateLink.active == True,
    ).order_by(desc(AffiliateLink.total_revenue_cents)).all()

    # Top converting link
    top_link = links[0] if links else None

    # Recent conversions
    recent = db.query(AffiliateEarning).filter(
        AffiliateEarning.creator_id == user.id
    ).order_by(desc(AffiliateEarning.earned_at)).limit(10).all()

    return {
        "enrolled":         True,
        "currency":         currency,
        "creator_share":    f"{programme.creator_share_pct}%",
        "summary": {
            "total_links":        len(links),
            "total_clicks":       programme.total_clicks,
            "total_conversions":  programme.total_conversions,
            "conversion_rate":    f"{round(programme.total_conversions/max(programme.total_clicks,1)*100,1)}%",
            "total_earned":       CurrencyService.to_display(programme.total_earned_cents, currency),
            "platform_earned":    CurrencyService.to_display(programme.platform_earned_cents, currency),
        },
        "top_link": _link_dict(top_link, currency) if top_link else None,
        "links":    [_link_dict(l, currency) for l in links],
        "recent_conversions": [
            {
                "amount":      CurrencyService.to_display(e.creator_cents, currency),
                "order_value": CurrencyService.to_display(e.order_value_cents or 0, currency),
                "network":     e.network.value if e.network else "custom",
                "earned_at":   e.earned_at.isoformat(),
            }
            for e in recent
        ],
        "tips": [
            "Finance and technology affiliate links convert best on FlintX — these viewers have purchase intent",
            "Place affiliate links in the first 3 lines of your video description — most viewers don't scroll",
            "Mention the product naturally in your video — forced recommendations convert at 1-2%, genuine ones at 5-8%",
            "Time-limited offers (Black Friday, seasonal deals) convert 3× better than evergreen links",
        ],
    }


@router.post("/affiliate/links", status_code=201)
def create_affiliate_link(
    req: CreateLinkRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    programme = db.query(AffiliateProgram).filter(
        AffiliateProgram.creator_id == user.id
    ).first()
    if not programme:
        raise HTTPException(status_code=403, detail="Join the affiliate programme first")

    # Generate unique short code
    code = _gen_short_code()
    while db.query(AffiliateLink).filter(AffiliateLink.short_code == code).first():
        code = _gen_short_code()

    link = AffiliateLink(
        program_id          = programme.id,
        creator_id          = user.id,
        name                = req.name,
        short_code          = code,
        original_url        = req.original_url,
        network             = AffiliateNetwork(req.network) if req.network in AffiliateNetwork.__members__ else AffiliateNetwork.custom,
        product_name        = req.product_name,
        product_description = req.product_description,
        product_price       = req.product_price,
        product_category    = req.product_category,
        commission_type     = req.commission_type,
        commission_rate     = req.commission_rate,
        commission_flat     = req.commission_flat,
    )
    db.add(link)
    db.commit()
    db.refresh(link)

    return {
        **_link_dict(link),
        "message": f"Use https://flintx.tv/go/{code} in your content. Every click is tracked automatically.",
    }


@router.get("/affiliate/links")
def list_affiliate_links(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    links = db.query(AffiliateLink).filter(
        AffiliateLink.creator_id == user.id
    ).order_by(desc(AffiliateLink.total_revenue_cents)).all()
    return {"links": [_link_dict(l, currency) for l in links]}


@router.patch("/affiliate/links/{link_id}")
def update_affiliate_link(
    link_id: str,
    req: UpdateLinkRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    link = db.query(AffiliateLink).filter(
        AffiliateLink.id == link_id,
        AffiliateLink.creator_id == user.id,
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    if req.name is not None:            link.name = req.name
    if req.product_name is not None:    link.product_name = req.product_name
    if req.product_price is not None:   link.product_price = req.product_price
    if req.commission_rate is not None: link.commission_rate = req.commission_rate
    if req.active is not None:          link.active = req.active

    db.commit()
    return _link_dict(link)


@router.get("/affiliate/links/{link_id}/stats")
def link_stats(
    link_id: str,
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    link = db.query(AffiliateLink).filter(
        AffiliateLink.id == link_id,
        AffiliateLink.creator_id == user.id,
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    clicks_by_day = db.query(
        func.strftime("%Y-%m-%d", AffiliateClick.created_at).label("date"),
        func.count(AffiliateClick.id).label("clicks"),
        func.count(AffiliateClick.converted_at).label("conversions"),
    ).filter(
        AffiliateClick.link_id == link_id,
        AffiliateClick.created_at >= datetime.utcnow() - timedelta(days=30),
    ).group_by("date").all()

    return {
        **_link_dict(link, currency),
        "conversion_rate":  f"{round(link.total_conversions/max(link.total_clicks,1)*100,1)}%",
        "avg_order_value":  CurrencyService.to_display(
            int(db.query(func.avg(AffiliateEarning.order_value_cents)).filter(AffiliateEarning.link_id==link_id).scalar() or 0),
            currency
        ),
        "daily_trend": [
            {"date": r.date, "clicks": r.clicks, "conversions": r.conversions}
            for r in clicks_by_day
        ],
    }


# ─────────────────────────────────────────────
# PUBLIC REDIRECT — /go/{code}
# ─────────────────────────────────────────────

@router.get("/go/{code}", include_in_schema=False)
def affiliate_redirect(
    code: str,
    request: Request,
    video_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    The FlintX affiliate redirect.
    Called when anyone clicks flintx.tv/go/{code}.
    Tracks the click then redirects to the original affiliate URL.
    """
    link = db.query(AffiliateLink).filter(
        AffiliateLink.short_code == code,
        AffiliateLink.active == True,
    ).first()

    if not link:
        # Try platform affiliate
        product = db.query(PlatformAffiliateProduct).filter(
            PlatformAffiliateProduct.short_code == code,
            PlatformAffiliateProduct.active == True,
        ).first()
        if product:
            product.total_clicks += 1
            db.commit()
            return RedirectResponse(url=product.affiliate_url, status_code=302)
        raise HTTPException(status_code=404, detail="Link not found or expired")

    # Generate session ID for attribution
    ip_hash    = hashlib.sha256(request.client.host.encode()).hexdigest()[:16]
    session_id = secrets.token_urlsafe(16)

    click = AffiliateClick(
        link_id    = link.id,
        creator_id = link.creator_id,
        video_id   = video_id,
        ip_hash    = ip_hash,
        user_agent = request.headers.get("user-agent", ""),
        referrer   = request.headers.get("referer", ""),
        session_id = session_id,
    )
    db.add(click)
    link.total_clicks += 1

    programme = db.query(AffiliateProgram).filter(
        AffiliateProgram.id == link.program_id
    ).first()
    if programme:
        programme.total_clicks += 1

    db.commit()

    # Set attribution cookie and redirect
    response = RedirectResponse(url=link.original_url, status_code=302)
    response.set_cookie(
        "flintx_aff",
        session_id,
        max_age=30*24*3600,  # 30 days
        httponly=True,
        samesite="lax",
    )
    return response


# ─────────────────────────────────────────────
# AFFILIATE CONVERSION WEBHOOKS
# ─────────────────────────────────────────────

def _process_conversion(db: Session, session_id: str, order_id: str,
                         order_value_usd: float, commission_usd: float,
                         network: AffiliateNetwork):
    """
    Process a conversion from any affiliate network.
    Called by all network-specific webhook handlers.
    """
    # Find the click by session ID
    click = db.query(AffiliateClick).filter(
        AffiliateClick.session_id == session_id,
        AffiliateClick.status == AffiliateClickStatus.clicked,
    ).order_by(desc(AffiliateClick.created_at)).first()

    if not click:
        print(f"[AFFILIATE] Conversion received but no click found — session: {session_id}")
        return

    gross_cents   = int(commission_usd * 100)
    creator_cents = int(gross_cents * CREATOR_SHARE_PCT / 100)
    platform_cents = gross_cents - creator_cents
    order_cents   = int(order_value_usd * 100)

    # Update click
    click.status          = AffiliateClickStatus.converted
    click.order_value_cents = order_cents
    click.commission_cents  = gross_cents
    click.creator_cut_cents = creator_cents
    click.platform_cut_cents = platform_cents
    click.converted_at    = datetime.utcnow()
    click.network_order_id = order_id

    # Update link stats
    link = db.query(AffiliateLink).filter(AffiliateLink.id == click.link_id).first()
    if link:
        link.total_conversions   += 1
        link.total_revenue_cents += gross_cents
        link.creator_earned_cents += creator_cents
        link.platform_earned_cents += platform_cents

    # Update programme stats
    programme = db.query(AffiliateProgram).filter(
        AffiliateProgram.creator_id == click.creator_id
    ).first()
    if programme:
        programme.total_conversions += 1
        programme.total_earned_cents += creator_cents
        programme.platform_earned_cents += platform_cents

    # Credit creator wallet
    creator = db.query(User).filter(User.id == click.creator_id).first()
    if creator:
        creator.wallet_balance += creator_cents
        db.add(Transaction(
            user_id      = creator.id,
            type         = TxnType.ad_revenue,   # use ad_revenue type for affiliate too
            amount       = creator_cents,
            balance_after = creator.wallet_balance,
            description  = f"Affiliate commission ({network.value}) — {CREATOR_SHARE_PCT}% of ${commission_usd:.2f}",
            reference    = order_id,
        ))

    # Record earning
    db.add(AffiliateEarning(
        click_id        = click.id,
        link_id         = click.link_id,
        creator_id      = click.creator_id,
        gross_cents     = gross_cents,
        creator_cents   = creator_cents,
        platform_cents  = platform_cents,
        order_value_cents = order_cents,
        network         = network,
        network_order_id = order_id,
    ))

    db.commit()
    print(f"[AFFILIATE] Conversion: ${commission_usd:.2f} → creator ${creator_cents/100:.2f}, FlintX ${platform_cents/100:.2f}")


@router.post("/affiliate/webhook/amazon")
def webhook_amazon(req: ConversionWebhook, db: Session = Depends(get_db)):
    _process_conversion(db, req.click_reference, req.order_id,
                        req.order_value, req.commission, AffiliateNetwork.amazon)
    return {"ok": True}


@router.post("/affiliate/webhook/impact")
def webhook_impact(req: ConversionWebhook, db: Session = Depends(get_db)):
    _process_conversion(db, req.click_reference, req.order_id,
                        req.order_value, req.commission, AffiliateNetwork.impact)
    return {"ok": True}


@router.post("/affiliate/webhook/shareasale")
def webhook_shareasale(req: ConversionWebhook, db: Session = Depends(get_db)):
    _process_conversion(db, req.click_reference, req.order_id,
                        req.order_value, req.commission, AffiliateNetwork.shareasale)
    return {"ok": True}


@router.post("/affiliate/webhook/generic")
def webhook_generic(
    network: str,
    req: ConversionWebhook,
    db: Session = Depends(get_db),
):
    net = AffiliateNetwork(network) if network in AffiliateNetwork.__members__ else AffiliateNetwork.custom
    _process_conversion(db, req.click_reference, req.order_id,
                        req.order_value, req.commission, net)
    return {"ok": True}


# ─────────────────────────────────────────────
# PLATFORM AFFILIATE PRODUCTS
# ─────────────────────────────────────────────

@router.get("/affiliate/products")
def get_products_for_niche(
    niche: Optional[str] = None,
    limit: int = 6,
    db: Session = Depends(get_db),
):
    """
    Public endpoint: get contextual affiliate products for a niche.
    Used on video pages, channel pages, category pages.
    Returns top products matching the video/channel niche.
    """
    q = db.query(PlatformAffiliateProduct).filter(
        PlatformAffiliateProduct.active == True
    )
    if niche:
        # target_niches stored as JSON string — use LIKE for SQLite compatibility
        q = q.filter(PlatformAffiliateProduct.target_niches.like(f'%{niche}%'))

    products = q.order_by(
        desc(PlatformAffiliateProduct.priority)
    ).limit(min(limit, 12)).all()

    return {
        "niche":    niche,
        "products": [
            {
                "id":          p.id,
                "name":        p.name,
                "description": p.description,
                "price":       p.price_display,
                "image_url":   p.image_url,
                "affiliate_url": f"https://flintx.tv/go/p/{p.short_code}",
                "commission":  f"{p.commission_rate*100:.0f}% commission" if p.commission_type=="percentage" else "flat rate",
            }
            for p in products
        ],
    }


@router.post("/affiliate/products", status_code=201)
def add_platform_product(
    req: AddProductRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    code = f"p{_gen_short_code(7)}"
    while db.query(PlatformAffiliateProduct).filter(PlatformAffiliateProduct.short_code == code).first():
        code = f"p{_gen_short_code(7)}"

    product = PlatformAffiliateProduct(
        name            = req.name,
        network         = AffiliateNetwork(req.network) if req.network in AffiliateNetwork.__members__ else AffiliateNetwork.custom,
        affiliate_url   = req.affiliate_url,
        short_code      = code,
        image_url       = req.image_url,
        description     = req.description,
        price_display   = req.price_display,
        target_niches   = json.dumps(req.target_niches),
        commission_type = req.commission_type,
        commission_rate = req.commission_rate,
        priority        = req.priority,
    )
    db.add(product)
    db.commit()
    return {"id": product.id, "short_code": code, "flintx_url": f"https://flintx.tv/go/p/{code}"}


def seed_platform_products(db: Session):
    """Seed the launch platform affiliate products if none exist."""
    if db.query(PlatformAffiliateProduct).count() > 0:
        return
    for p in LAUNCH_PRODUCTS:
        code = f"p{_gen_short_code(7)}"
        product = PlatformAffiliateProduct(
            name            = p["name"],
            network         = AffiliateNetwork(p["network"]) if p["network"] in AffiliateNetwork.__members__ else AffiliateNetwork.custom,
            affiliate_url   = f"https://placeholder-{p['name'].lower().replace(' ','-')}.com/flintx",
            short_code      = code,
            description     = p["description"],
            price_display   = p.get("price_display",""),
            target_niches   = json.dumps(p["target_niches"]),
            commission_type = p["commission_type"],
            commission_rate = p["commission_rate"],
            priority        = 5,
        )
        db.add(product)
    db.commit()
    print(f"[AFFILIATE] Seeded {len(LAUNCH_PRODUCTS)} platform affiliate products")


# ─────────────────────────────────────────────
# ADSENSE CONFIGURATION
# ─────────────────────────────────────────────

@router.post("/adsense/config", status_code=201)
def set_adsense_config(
    req: AdSenseConfigRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set AdSense publisher ID and ad unit IDs. Admin only."""
    if not req.publisher_id.startswith("ca-pub-"):
        raise HTTPException(status_code=422, detail="Publisher ID must start with 'ca-pub-'")

    cfg = _get_or_create_adsense_config(db)
    cfg.publisher_id         = req.publisher_id
    cfg.mode                 = req.mode
    cfg.auto_ads_enabled     = req.auto_ads_enabled
    cfg.unit_watch_sidebar   = req.unit_watch_sidebar
    cfg.unit_watch_below     = req.unit_watch_below
    cfg.unit_browse_banner   = req.unit_browse_banner
    cfg.unit_browse_inline   = req.unit_browse_inline
    cfg.unit_channel_page    = req.unit_channel_page
    cfg.unit_search_results  = req.unit_search_results
    cfg.unit_live_sidebar    = req.unit_live_sidebar
    cfg.enabled              = True
    db.commit()

    return {
        "configured": True,
        "publisher_id": cfg.publisher_id,
        "mode":         cfg.mode,
        "message":      "AdSense configured. Add the auto-ads snippet to your frontend <head> to activate.",
    }


@router.get("/adsense/config")
def get_adsense_config(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = _get_or_create_adsense_config(db)
    return {
        "configured":         cfg.enabled and bool(cfg.publisher_id),
        "publisher_id":       cfg.publisher_id,
        "mode":               cfg.mode,
        "auto_ads_enabled":   cfg.auto_ads_enabled,
        "ad_units": {
            "watch_sidebar":   cfg.unit_watch_sidebar,
            "watch_below":     cfg.unit_watch_below,
            "browse_banner":   cfg.unit_browse_banner,
            "browse_inline":   cfg.unit_browse_inline,
            "channel_page":    cfg.unit_channel_page,
            "search_results":  cfg.unit_search_results,
            "live_sidebar":    cfg.unit_live_sidebar,
        },
        "total_earned":       CurrencyService.format(cfg.total_earned_cents, "USD"),
        "monthly_earned":     CurrencyService.format(cfg.monthly_earned_cents, "USD"),
        "last_synced_at":     cfg.last_synced_at.isoformat() if cfg.last_synced_at else None,
    }


@router.get("/adsense/snippet/{placement}")
def get_adsense_snippet(
    placement: str,
    db: Session = Depends(get_db),
):
    """
    Returns the HTML snippet for a specific placement.
    Frontend embeds this in the correct location.
    No auth required — snippet contains no sensitive data.
    """
    cfg = _get_or_create_adsense_config(db)
    if not cfg.enabled or not cfg.publisher_id:
        return {"snippet": "", "enabled": False}

    if cfg.auto_ads_enabled:
        # Auto ads — one snippet in <head> handles everything
        snippet = f"""<!-- FlintX AdSense Auto Ads -->
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={cfg.publisher_id}" crossorigin="anonymous"></script>"""
        return {"snippet": snippet, "enabled": True, "mode": "auto"}

    # Manual placement — get the specific ad unit
    unit_map = {
        "watch_sidebar":  cfg.unit_watch_sidebar,
        "watch_below":    cfg.unit_watch_below,
        "browse_banner":  cfg.unit_browse_banner,
        "browse_inline":  cfg.unit_browse_inline,
        "channel_page":   cfg.unit_channel_page,
        "search_results": cfg.unit_search_results,
        "live_sidebar":   cfg.unit_live_sidebar,
    }
    unit_id = unit_map.get(placement, "")
    if not unit_id:
        return {"snippet": "", "enabled": False, "reason": f"No ad unit configured for {placement}"}

    snippet = f"""<!-- FlintX AdSense — {placement} -->
<ins class="adsbygoogle"
     style="display:block"
     data-ad-client="{cfg.publisher_id}"
     data-ad-slot="{unit_id}"
     data-ad-format="auto"
     data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>"""

    return {"snippet": snippet, "enabled": True, "mode": "manual", "placement": placement}


@router.get("/adsense/revenue")
def adsense_revenue(
    currency: str = "USD",
    days: int = 30,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cfg = _get_or_create_adsense_config(db)
    snapshots = db.query(AdSenseSnapshot).order_by(
        desc(AdSenseSnapshot.date)
    ).limit(days).all()

    return {
        "currency":       currency,
        "total_earned":   CurrencyService.format(cfg.total_earned_cents, currency),
        "monthly_earned": CurrencyService.format(cfg.monthly_earned_cents, currency),
        "daily": [
            {
                "date":        s.date,
                "impressions": s.impressions,
                "clicks":      s.clicks,
                "ctr":         f"{s.ctr:.2f}%",
                "rpm":         CurrencyService.format(s.rpm_cents, currency),
                "earned":      CurrencyService.format(s.earned_cents, currency),
            }
            for s in reversed(snapshots)
        ],
        "setup_instructions": {
            "step_1": "Sign up at adsense.google.com",
            "step_2": "Add your site (flintx.tv) — verification can take 1–2 weeks",
            "step_3": "Get your Publisher ID (ca-pub-XXXXXXXXXXXXXXXXX)",
            "step_4": "Configure here: POST /api/adsense/config with your publisher_id",
            "step_5": "Add the auto-ads snippet to your frontend <head> (GET /api/adsense/snippet/head)",
            "expected_rpm": "$0.50–$3.00 for browse pages, $1.00–$5.00 for watch pages",
            "note": "AdSense review typically takes 2–4 weeks for new sites. Apply once you have 20+ pieces of content.",
        } if not cfg.enabled else None,
    }


# ─────────────────────────────────────────────
# ADMIN OVERVIEW
# ─────────────────────────────────────────────

@router.get("/affiliate/admin/overview")
def admin_affiliate_overview(
    currency: str = "USD",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total_programmes = db.query(func.count(AffiliateProgram.id)).scalar() or 0
    total_links      = db.query(func.count(AffiliateLink.id)).filter(AffiliateLink.active==True).scalar() or 0
    total_clicks     = db.query(func.coalesce(func.sum(AffiliateLink.total_clicks),0)).scalar() or 0
    total_conversions = db.query(func.coalesce(func.sum(AffiliateLink.total_conversions),0)).scalar() or 0
    total_platform   = db.query(func.coalesce(func.sum(AffiliateEarning.platform_cents),0)).scalar() or 0
    total_creator    = db.query(func.coalesce(func.sum(AffiliateEarning.creator_cents),0)).scalar() or 0

    cfg = _get_or_create_adsense_config(db)

    return {
        "currency": currency,
        "creator_affiliate": {
            "active_programmes": total_programmes,
            "active_links":      total_links,
            "total_clicks":      total_clicks,
            "total_conversions": total_conversions,
            "conversion_rate":   f"{round(total_conversions/max(total_clicks,1)*100,1)}%",
            "platform_earned":   CurrencyService.format(total_platform, currency),
            "creator_earned":    CurrencyService.format(total_creator, currency),
        },
        "platform_affiliate": {
            "products": db.query(func.count(PlatformAffiliateProduct.id)).filter(PlatformAffiliateProduct.active==True).scalar() or 0,
            "clicks":   db.query(func.coalesce(func.sum(PlatformAffiliateProduct.total_clicks),0)).scalar() or 0,
            "earned":   CurrencyService.format(db.query(func.coalesce(func.sum(PlatformAffiliateProduct.total_earned_cents),0)).scalar() or 0, currency),
        },
        "adsense": {
            "configured": cfg.enabled and bool(cfg.publisher_id),
            "publisher_id": cfg.publisher_id or "Not set",
            "monthly_earned": CurrencyService.format(cfg.monthly_earned_cents, currency),
            "total_earned":   CurrencyService.format(cfg.total_earned_cents, currency),
        },
    }


@router.get("/affiliate/admin/pending-pay")
def admin_pending_payments(
    currency: str = "USD",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Affiliate commissions owed to creators but not yet paid."""
    unpaid = db.query(AffiliateEarning).filter(
        AffiliateEarning.paid_to_creator == False
    ).order_by(desc(AffiliateEarning.earned_at)).all()

    total_owed = sum(e.creator_cents for e in unpaid)

    return {
        "total_owed": CurrencyService.format(total_owed, currency),
        "count":      len(unpaid),
        "earnings":   [
            {
                "id":          e.id,
                "creator_id":  e.creator_id,
                "amount":      CurrencyService.format(e.creator_cents, currency),
                "network":     e.network.value if e.network else "custom",
                "earned_at":   e.earned_at.isoformat(),
            }
            for e in unpaid
        ],
    }


@router.post("/affiliate/admin/pay/{earning_id}")
def mark_paid(
    earning_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    earning = db.query(AffiliateEarning).filter(AffiliateEarning.id == earning_id).first()
    if not earning:
        raise HTTPException(status_code=404, detail="Earning not found")
    earning.paid_to_creator = True
    earning.paid_at = datetime.utcnow()
    db.commit()
    return {"paid": True}
