"""
Flint — Payment Routes

PayPal (incoming): subscriptions, course purchases, advertiser top-ups, viewer cashouts
Wise Business (outgoing): creator payouts on 1st and 15th

POST /api/payments/paypal/webhook     — PayPal webhook handler
POST /api/payments/paypal/subscription/create — create a subscription
GET  /api/payments/wise/recipients    — list Wise recipient accounts
POST /api/payments/wise/payout        — send a payout (admin only)
GET  /api/payments/admin/pending      — pending payout requests (admin)
POST /api/payments/admin/process/{id} — process a payout (admin)
GET  /api/payments/admin/stats        — payment stats (admin)
"""

import os
import hmac
import hashlib
import json
import httpx
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    User, CreatorProfile, AdvertiserProfile,
    PayoutRequest, Transaction, TxnType,
    StudioPlan, PassStatus,
)
from ..auth.routes import require_verified, require_admin
from ..email.service import send_payout_confirmed_email

router = APIRouter(prefix="/payments", tags=["Payments"])

# ── Config ────────────────────────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID    = os.getenv("PAYPAL_WEBHOOK_ID")
PAYPAL_MODE          = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_BASE          = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

WISE_API_KEY  = os.getenv("WISE_API_KEY")
WISE_PROFILE  = os.getenv("WISE_PROFILE_ID")
WISE_MODE     = os.getenv("WISE_MODE", "sandbox")
WISE_BASE     = "https://api.sandbox.transferwise.tech" if WISE_MODE == "sandbox" else "https://api.transferwise.com"

# PayPal plan IDs (create these in PayPal dashboard → Products & Plans)
PLAN_IDS = {
    "studio_basic_monthly":  os.getenv("PAYPAL_PLAN_STUDIO_BASIC_M"),
    "studio_pro_monthly":    os.getenv("PAYPAL_PLAN_STUDIO_PRO_M"),
    "studio_agency_monthly": os.getenv("PAYPAL_PLAN_STUDIO_AGENCY_M"),
    "studio_basic_annual":   os.getenv("PAYPAL_PLAN_STUDIO_BASIC_A"),
    "studio_pro_annual":     os.getenv("PAYPAL_PLAN_STUDIO_PRO_A"),
    "studio_agency_annual":  os.getenv("PAYPAL_PLAN_STUDIO_AGENCY_A"),
    "pass_monthly":          os.getenv("PAYPAL_PLAN_PASS_M"),
    "pass_annual":           os.getenv("PAYPAL_PLAN_PASS_A"),
}


# ─────────────────────────────────────────────
# PAYPAL TOKEN
# ─────────────────────────────────────────────

async def _paypal_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
        )
        return resp.json().get("access_token", "")


# ─────────────────────────────────────────────
# PAYPAL WEBHOOK
# ─────────────────────────────────────────────

@router.post("/paypal/webhook")
async def paypal_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    PayPal sends events here for:
    - BILLING.SUBSCRIPTION.ACTIVATED   → activate Studio plan or FlintX Pass
    - BILLING.SUBSCRIPTION.CANCELLED   → deactivate plan
    - BILLING.SUBSCRIPTION.RENEWED     → extend subscription
    - PAYMENT.CAPTURE.COMPLETED        → one-time payment (course, advertiser top-up)

    Verify the webhook signature before processing.
    PayPal docs: https://developer.paypal.com/api/rest/webhooks/
    """
    body      = await request.body()
    headers   = request.headers

    # Webhook signature verification
    if not _verify_paypal_webhook(headers, body):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(body)
    event_type = event.get("event_type", "")

    background_tasks.add_task(_handle_paypal_event, event_type, event, db)

    return {"received": True}


def _verify_paypal_webhook(headers: dict, body: bytes) -> bool:
    """
    Verify PayPal webhook signature.
    In sandbox, you can skip this for testing — set PAYPAL_SKIP_VERIFY=true in .env
    """
    if os.getenv("PAYPAL_SKIP_VERIFY", "false").lower() == "true":
        return True

    # Production verification requires calling PayPal's verify-webhook-signature endpoint
    # See: https://developer.paypal.com/api/rest/webhooks/rest/
    # For now, basic implementation — replace with full verification in production
    webhook_id = headers.get("paypal-transmission-id", "")
    return bool(webhook_id)   # In production: verify signature cryptographically


def _handle_paypal_event(event_type: str, event: dict, db: Session):
    """Process a PayPal webhook event."""
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        resource = event.get("resource", {})

        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            _activate_subscription(db, resource)

        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
            _cancel_subscription(db, resource)

        elif event_type == "BILLING.SUBSCRIPTION.RENEWED":
            _renew_subscription(db, resource)

        elif event_type == "PAYMENT.CAPTURE.COMPLETED":
            _handle_one_time_payment(db, resource)

    except Exception as e:
        print(f"[PAYPAL WEBHOOK ERROR] event_type={event_type} error={e}")
    finally:
        db.close()


def _activate_subscription(db: Session, resource: dict):
    """Activate a Studio plan or FlintX Pass subscription."""
    plan_id = resource.get("plan_id", "")
    sub_id  = resource.get("id", "")
    payer   = resource.get("subscriber", {}).get("payer_id", "")

    # Look up which plan this is
    plan_name = next((k for k, v in PLAN_IDS.items() if v == plan_id), None)
    if not plan_name:
        print(f"[PAYPAL] Unknown plan_id: {plan_id}")
        return

    # Find the user by PayPal payer info
    # In production: store payer_id during checkout flow
    # Simplified: use custom_id field which we set during subscription creation
    custom_id = resource.get("custom_id", "")
    user = db.query(User).filter(User.id == custom_id).first()
    if not user:
        return

    period = "annual" if "annual" in plan_name else "monthly"
    expires = datetime.utcnow() + (timedelta(days=365) if period == "annual" else timedelta(days=31))

    if "studio" in plan_name:
        tier = plan_name.replace("studio_", "").replace(f"_{period}", "")
        creator = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
        if creator:
            creator.studio_plan         = StudioPlan(tier)
            creator.studio_plan_expires = expires
            creator.studio_paypal_id    = sub_id

    elif "pass" in plan_name:
        user.pass_status  = PassStatus.annual if period == "annual" else PassStatus.monthly
        user.pass_expires = expires
        user.pass_paypal_id = sub_id

    # Record transaction
    price_map = {
        "studio_basic_monthly": 2900, "studio_pro_monthly": 5900,
        "studio_agency_monthly": 14900, "studio_basic_annual": 2300,
        "studio_pro_annual": 4700, "studio_agency_annual": 11900,
        "pass_monthly": 999, "pass_annual": 799,
    }
    amount = price_map.get(plan_name, 0)
    db.add(Transaction(
        user_id=user.id, type=TxnType.studio_sub if "studio" in plan_name else TxnType.pass_sub,
        amount=-amount, description=f"Subscription — {plan_name.replace('_', ' ').title()}",
        reference=sub_id,
    ))
    db.commit()


def _cancel_subscription(db: Session, resource: dict):
    """Deactivate plan when cancelled."""
    sub_id = resource.get("id", "")
    creator = db.query(CreatorProfile).filter(CreatorProfile.studio_paypal_id == sub_id).first()
    if creator:
        creator.studio_plan = StudioPlan.none
        db.commit()
        return

    user = db.query(User).filter(User.pass_paypal_id == sub_id).first()
    if user:
        user.pass_status = PassStatus.none
        db.commit()


def _renew_subscription(db: Session, resource: dict):
    """Extend subscription on renewal."""
    sub_id = resource.get("id", "")
    creator = db.query(CreatorProfile).filter(CreatorProfile.studio_paypal_id == sub_id).first()
    if creator and creator.studio_plan_expires:
        creator.studio_plan_expires += timedelta(days=31)
        db.commit()
        return

    user = db.query(User).filter(User.pass_paypal_id == sub_id).first()
    if user and user.pass_expires:
        user.pass_expires += timedelta(days=31)
        db.commit()


def _handle_one_time_payment(db: Session, resource: dict):
    """Handle one-time captures (advertiser top-up, course purchase)."""
    amount_str = resource.get("amount", {}).get("value", "0")
    amount_pence = int(float(amount_str) * 100)
    custom_id    = resource.get("custom_id", "")  # "advertiser:{adv_id}" or "course:{course_id}:{buyer_id}"

    if custom_id.startswith("advertiser:"):
        adv_id = custom_id.split(":")[1]
        adv = db.query(AdvertiserProfile).filter(AdvertiserProfile.id == adv_id).first()
        if adv:
            adv.budget_balance += amount_pence
            db.add(Transaction(
                user_id=adv.user_id, type=TxnType.advertiser_topup,
                amount=amount_pence, description=f"Advertiser budget top-up",
            ))
            db.commit()


# ─────────────────────────────────────────────
# WISE PAYOUT
# ─────────────────────────────────────────────

def _wise_headers():
    return {
        "Authorization": f"Bearer {WISE_API_KEY}",
        "Content-Type":  "application/json",
    }


@router.get("/wise/quote")
def wise_quote(target_currency: str = "GBP", amount: float = 0, admin: User = Depends(require_admin)):
    """Get a Wise transfer quote."""
    if not WISE_API_KEY:
        return {"rate": 1.0, "fee": 0.5, "note": "Demo — add WISE_API_KEY to .env"}
    try:
        resp = httpx.post(
            f"{WISE_BASE}/v3/profiles/{WISE_PROFILE}/quotes",
            headers=_wise_headers(),
            json={
                "sourceCurrency": "GBP",
                "targetCurrency": target_currency,
                "sourceAmount":   amount,
                "targetAmount":   None,
            },
        )
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# ADMIN — PENDING PAYOUTS
# ─────────────────────────────────────────────

@router.get("/admin/pending")
def get_pending_payouts(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    pending = db.query(PayoutRequest).filter(
        PayoutRequest.status == "pending"
    ).order_by(PayoutRequest.requested_at).all()

    results = []
    for p in pending:
        user = db.query(User).filter(User.id == p.user_id).first()
        results.append({
            "id":           p.id,
            "user_id":      p.user_id,
            "user_name":    user.full_name if user else "",
            "user_email":   user.email if user else "",
            "amount":       p.amount,
            "amount_display": f"£{p.amount/100:.2f}",
            "method":       p.method,
            "status":       p.status,
            "requested_at": p.requested_at.isoformat(),
        })

    return {"count": len(results), "payouts": results}


# ─────────────────────────────────────────────
# ADMIN — PROCESS A PAYOUT
# ─────────────────────────────────────────────

class ProcessPayoutRequest(BaseModel):
    wise_transfer_id:  str = ""
    paypal_txn_id:     str = ""
    notes:             str = ""


@router.post("/admin/process/{payout_id}")
def process_payout(
    payout_id: str,
    req: ProcessPayoutRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payout = db.query(PayoutRequest).filter(PayoutRequest.id == payout_id).first()
    if not payout:
        raise HTTPException(status_code=404, detail="Payout not found")
    if payout.status != "pending":
        raise HTTPException(status_code=400, detail="Payout is not in pending status")

    payout.status           = "paid"
    payout.paid_at          = datetime.utcnow()
    payout.wise_transfer_id = req.wise_transfer_id
    payout.paypal_txn_id    = req.paypal_txn_id
    payout.notes            = req.notes

    db.commit()

    # Email the creator/viewer
    user = db.query(User).filter(User.id == payout.user_id).first()
    if user:
        send_payout_confirmed_email(user.email, user.full_name, payout.amount, payout.method)

    return {"processed": True, "payout_id": payout_id, "amount": payout.amount}


# ─────────────────────────────────────────────
# ADMIN — PAYMENT STATS
# ─────────────────────────────────────────────

@router.get("/admin/stats")
def payment_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    from sqlalchemy import func

    total_in = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.amount > 0
    ).scalar() or 0

    total_out = abs(db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.amount < 0
    ).scalar() or 0)

    pending_count  = db.query(PayoutRequest).filter(PayoutRequest.status == "pending").count()
    pending_amount = db.query(func.coalesce(func.sum(PayoutRequest.amount), 0)).filter(
        PayoutRequest.status == "pending"
    ).scalar() or 0

    return {
        "total_in":        total_in,
        "total_out":       total_out,
        "net_profit":      total_in - total_out,
        "pending_payouts": pending_count,
        "pending_amount":  pending_amount,
    }
