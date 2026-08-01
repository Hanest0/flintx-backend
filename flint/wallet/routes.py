"""
Flint — Wallet API Routes
All amounts stored as USD cents. Display converts to user's requested currency.

GET  /api/wallet/balance       — balance + breakdown in requested currency
GET  /api/wallet/transactions  — transaction history
POST /api/wallet/payout        — request a payout
GET  /api/wallet/payouts       — payout history
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Literal

from ..database.connection import get_db
from ..database.models import (
    User, Transaction, TxnType, CreatorProfile, ViewerProfile, PayoutRequest,
)
from ..auth.routes import require_verified
from ..currency.service import (
    CurrencyService, MIN_PAYOUT_CREATOR_USD, MIN_PAYOUT_VIEWER_USD,
)

router = APIRouter(prefix="/wallet", tags=["Wallet"])


class PayoutRequestSchema(BaseModel):
    method:       Literal["wise", "paypal"]
    wise_email:   str = ""
    paypal_email: str = ""


@router.get("/balance")
def get_balance(
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    currency = currency.upper()
    balance  = user.wallet_balance   # USD cents

    ad_total     = _sum_type(db, user.id, TxnType.ad_revenue)
    course_total = _sum_type(db, user.id, TxnType.course_sale)
    tip_total    = _sum_type(db, user.id, TxnType.tip)
    credit_total = _sum_type(db, user.id, TxnType.viewer_credit)
    paid_out     = abs(_sum_type(db, user.id, TxnType.payout_wise) +
                       _sum_type(db, user.id, TxnType.payout_paypal))

    is_creator = user.role in ("creator", "both")
    is_viewer  = user.role in ("viewer", "both")

    pending_payout = 0
    if is_creator:
        c = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
        if c:
            pending_payout = c.pending_payout

    min_payout = MIN_PAYOUT_CREATOR_USD if is_creator else MIN_PAYOUT_VIEWER_USD

    return {
        "currency":      currency,
        "balance":       CurrencyService.to_display(balance, currency),
        "balance_usd_cents": balance,
        "breakdown": {
            "ad_revenue":     CurrencyService.to_display(ad_total, currency) if is_creator else None,
            "course_sales":   CurrencyService.to_display(course_total, currency) if is_creator else None,
            "tips":           CurrencyService.to_display(tip_total, currency) if is_creator else None,
            "viewer_credits": CurrencyService.to_display(credit_total, currency) if is_viewer else None,
        },
        "total_earned":    CurrencyService.to_display(ad_total + course_total + tip_total + credit_total, currency),
        "total_paid_out":  CurrencyService.to_display(paid_out, currency),
        "pending_payout":  CurrencyService.to_display(pending_payout, currency),
        "min_payout":      CurrencyService.to_display(min_payout, currency),
        "next_payout_date": _next_payout_date(),
    }


@router.get("/transactions")
def get_transactions(
    page: int = 1,
    limit: int = 20,
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    limit  = min(limit, 50)
    offset = (page - 1) * limit

    txns  = db.query(Transaction).filter(
        Transaction.user_id == user.id
    ).order_by(desc(Transaction.created_at)).offset(offset).limit(limit).all()
    total = db.query(Transaction).filter(Transaction.user_id == user.id).count()

    return {
        "currency":     currency.upper(),
        "transactions": [_txn_dict(t, currency) for t in txns],
        "total":        total,
        "page":         page,
        "pages":        (total // limit) + 1,
    }


@router.post("/payout")
def request_payout(
    req: PayoutRequestSchema,
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    is_creator = user.role in ("creator", "both")
    min_payout = MIN_PAYOUT_CREATOR_USD if is_creator else MIN_PAYOUT_VIEWER_USD

    if user.wallet_balance < min_payout:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum payout is {CurrencyService.format(min_payout, currency)}. "
                   f"Your balance: {CurrencyService.format(user.wallet_balance, currency)}"
        )

    if req.method == "wise" and not req.wise_email:
        raise HTTPException(status_code=422, detail="Wise email required")
    if req.method == "paypal" and not req.paypal_email:
        raise HTTPException(status_code=422, detail="PayPal email required")

    existing = db.query(PayoutRequest).filter(
        PayoutRequest.user_id == user.id, PayoutRequest.status == "pending"
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a pending payout request")

    amount = user.wallet_balance
    user.wallet_balance -= amount

    payout = PayoutRequest(user_id=user.id, amount=amount, method=req.method, status="pending")
    db.add(payout)

    txn_type = TxnType.payout_wise if req.method == "wise" else TxnType.payout_paypal
    db.add(Transaction(
        user_id      = user.id,
        type         = txn_type,
        amount       = -amount,
        balance_after = user.wallet_balance,
        description  = f"Payout via {req.method.capitalize()} — {CurrencyService.format(amount, currency)}",
    ))

    if is_creator:
        c = db.query(CreatorProfile).filter(CreatorProfile.user_id == user.id).first()
        if c:
            c.pending_payout = 0

    db.commit()

    return {
        "payout_id":     payout.id,
        "amount_usd_cents": amount,
        "amount_display":   CurrencyService.to_display(amount, currency),
        "method":        req.method,
        "status":        "pending",
        "message":       "Payout request received. Processed within 1 business day.",
    }


@router.get("/payouts")
def get_payouts(
    currency: str = Query(default="USD"),
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    payouts = db.query(PayoutRequest).filter(
        PayoutRequest.user_id == user.id
    ).order_by(desc(PayoutRequest.requested_at)).all()

    return {
        "currency": currency.upper(),
        "payouts": [
            {
                "id":             p.id,
                "amount_usd_cents": p.amount,
                "amount_display": CurrencyService.to_display(p.amount, currency),
                "method":         p.method,
                "status":         p.status,
                "requested_at":   p.requested_at.isoformat(),
                "paid_at":        p.paid_at.isoformat() if p.paid_at else None,
            }
            for p in payouts
        ]
    }


def _sum_type(db, user_id, txn_type):
    from sqlalchemy import func
    return db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.user_id == user_id, Transaction.type == txn_type
    ).scalar() or 0


def _next_payout_date():
    from datetime import timedelta
    today = datetime.utcnow()
    if today.day < 15:
        return today.replace(day=15).strftime("%B %d, %Y")
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return next_month.strftime("%B %d, %Y")


def _txn_dict(t: Transaction, currency: str) -> dict:
    return {
        "id":             t.id,
        "type":           t.type.value,
        "amount_usd_cents": t.amount,
        "amount_display": CurrencyService.to_display(abs(t.amount), currency),
        "is_credit":      t.amount > 0,
        "description":    t.description,
        "reference":      t.reference,
        "video_id":       t.video_id,
        "created_at":     t.created_at.isoformat(),
    }
