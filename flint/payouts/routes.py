"""
FlintX — Phased Payout System API Routes

Public:
  GET  /api/payouts/phase             — current phase + progress (public milestone tracker)
  GET  /api/payouts/milestones        — full milestone history

Creator:
  GET  /api/payouts/my-earnings       — my earnings breakdown by phase
  GET  /api/payouts/my-credits        — my credit ledger
  POST /api/payouts/request           — request cash payout (phase-gated)
  GET  /api/payouts/founding          — my founding creator status + bonus

Viewer:
  GET  /api/payouts/viewer-credits    — my viewer Pass credit balance
  POST /api/payouts/viewer-cashout    — cash out viewer credits (phase-gated)

Internal:
  POST /api/payouts/record-view       — record a unique viewer (called from video view endpoint)
  POST /api/payouts/trigger-phase     — check and trigger phase transition (background job)

Admin:
  GET  /api/payouts/admin/phase       — full phase dashboard
  POST /api/payouts/admin/override    — manually set phase (emergency use only)
  POST /api/payouts/admin/convert-all — trigger Phase 3 credit conversion
  GET  /api/payouts/admin/liability   — total locked credits liability
"""

import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel

from ..database.connection import get_db
from ..database.models import (
    User, CreatorProfile, Transaction, TxnType, Video, VideoStatus,
)
from ..auth.routes import require_verified, require_admin
from ..currency.service import CurrencyService
from ..security.crypto import encrypt_field, decrypt_field
from .models import (
    PlatformState, ViewerMilestone, UniqueViewerRecord,
    CreatorCreditLedger, FoundingCreator, PhaseTransitionLog,
    PlatformPhase, CreditStatus, MilestoneType,
)

router = APIRouter(prefix="/payouts", tags=["Phased Payouts"])

# ─────────────────────────────────────────────
# PHASE CONFIGURATION
# ─────────────────────────────────────────────

PHASE_CONFIG = {
    PlatformPhase.foundation: {
        "name":           "Foundation",
        "phase_num":      1,
        "viewer_range":   "0 – 25,000",
        "creator_share":  40,
        "cash_portion":   0,      # % of earnings paid as cash
        "credit_portion": 100,    # % locked as credits
        "min_payout":     0,      # no cash payouts
        "viewer_min":     0,      # no viewer cashouts
        "description":    "Building the foundation. All creator earnings accrue as locked credits visible in your wallet.",
        "colour":         "#6A6A7A",
    },
    PlatformPhase.momentum: {
        "name":           "Momentum",
        "phase_num":      2,
        "viewer_range":   "25,001 – 75,000",
        "creator_share":  60,
        "cash_portion":   50,
        "credit_portion": 50,
        "min_payout":     1000,   # $10.00
        "viewer_min":     1000,   # $10.00
        "description":    "Partial payouts active. 50% of earnings paid as cash, 50% locked until Phase 3.",
        "colour":         "#FF9E2C",
    },
    PlatformPhase.full_launch: {
        "name":           "Full Launch",
        "phase_num":      3,
        "viewer_range":   "75,001 – 150,000",
        "creator_share":  70,
        "cash_portion":   100,
        "credit_portion": 0,
        "min_payout":     3000,   # $30.00
        "viewer_min":     2000,   # $20.00
        "description":    "Full cash payouts active. All Phase 1 locked credits converted automatically.",
        "colour":         "#FF5C00",
    },
    PlatformPhase.standard: {
        "name":           "Standard",
        "phase_num":      4,
        "viewer_range":   "150,000+",
        "creator_share":  80,
        "cash_portion":   100,
        "credit_portion": 0,
        "min_payout":     5000,   # $50.00
        "viewer_min":     2000,   # $20.00
        "description":    "Full FlintX model. 80% creator share. Standard payout terms.",
        "colour":         "#00E5A0",
    },
}

PHASE_THRESHOLDS = {
    PlatformPhase.momentum:    25_000,
    PlatformPhase.full_launch: 75_000,
    PlatformPhase.standard:    150_000,
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_state(db: Session) -> PlatformState:
    state = db.query(PlatformState).filter(PlatformState.id == 1).first()
    if not state:
        state = PlatformState(id=1)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _current_creator_share(phase: PlatformPhase) -> int:
    return PHASE_CONFIG[phase]["creator_share"]


def _phase_dict(state: PlatformState, currency: str = "USD") -> dict:
    phase    = state.phase
    config   = PHASE_CONFIG[phase]
    viewers  = state.collective_viewers

    # Progress to next phase
    thresholds = list(PHASE_THRESHOLDS.values())
    next_threshold = None
    progress_pct   = 100

    if phase == PlatformPhase.foundation:
        next_threshold = 25_000
        progress_pct   = min(100, int(viewers / 25_000 * 100))
    elif phase == PlatformPhase.momentum:
        next_threshold = 75_000
        progress_pct   = min(100, int((viewers - 25_000) / 50_000 * 100))
    elif phase == PlatformPhase.full_launch:
        next_threshold = 150_000
        progress_pct   = min(100, int((viewers - 75_000) / 75_000 * 100))

    viewers_needed = max(0, next_threshold - viewers) if next_threshold else 0

    return {
        "phase":            phase.value,
        "phase_name":       config["name"],
        "phase_number":     config["phase_num"],
        "description":      config["description"],
        "colour":           config["colour"],
        "collective_viewers": viewers,
        "collective_viewers_formatted": f"{viewers:,}",
        "next_threshold":   next_threshold,
        "viewers_needed":   viewers_needed,
        "viewers_needed_formatted": f"{viewers_needed:,}" if viewers_needed else "Target reached",
        "progress_pct":     progress_pct,
        "creator_share":    config["creator_share"],
        "cash_portion":     config["cash_portion"],
        "credit_portion":   config["credit_portion"],
        "min_payout":       CurrencyService.format(config["min_payout"], currency),
        "min_payout_cents": config["min_payout"],
        "viewer_cashout_min": CurrencyService.format(config["viewer_min"], currency),
        "cash_payouts_active": phase != PlatformPhase.foundation,
        "viewer_cashout_active": config["viewer_min"] > 0,
        "total_locked_credits": CurrencyService.format(state.total_locked_credits, currency),
        "total_paid_out":   CurrencyService.format(state.total_paid_out, currency),
        "phase_activated_at": state.phase_activated_at.isoformat() if state.phase_activated_at else None,
        "phases": [
            {
                "phase":        p.value,
                "name":         PHASE_CONFIG[p]["name"],
                "num":          PHASE_CONFIG[p]["phase_num"],
                "threshold":    PHASE_THRESHOLDS.get(p, 0),
                "share":        PHASE_CONFIG[p]["creator_share"],
                "colour":       PHASE_CONFIG[p]["colour"],
                "active":       p == phase,
                "completed":    PHASE_CONFIG[p]["phase_num"] < config["phase_num"],
            }
            for p in [PlatformPhase.foundation, PlatformPhase.momentum,
                      PlatformPhase.full_launch, PlatformPhase.standard]
        ],
    }


# ─────────────────────────────────────────────
# PUBLIC ENDPOINTS
# ─────────────────────────────────────────────

@router.get("/phase")
def get_phase(currency: str = "USD", db: Session = Depends(get_db)):
    """
    Public milestone tracker. Every creator and viewer can see this.
    This is the trust mechanism — the counter is live and cannot be faked.
    """
    state = _get_state(db)
    return _phase_dict(state, currency)


@router.get("/milestones")
def get_milestones(db: Session = Depends(get_db)):
    """Full milestone history — immutable public record."""
    milestones = db.query(ViewerMilestone).order_by(
        ViewerMilestone.achieved_at
    ).all()

    transitions = db.query(PhaseTransitionLog).order_by(
        PhaseTransitionLog.transitioned_at
    ).all()

    return {
        "milestones": [
            {
                "type":        m.milestone_type.value,
                "description": m.description,
                "viewers":     m.viewer_count,
                "phase":       m.phase_triggered.value if m.phase_triggered else None,
                "achieved_at": m.achieved_at.isoformat(),
            }
            for m in milestones
        ],
        "transitions": [
            {
                "from":        t.from_phase.value if t.from_phase else None,
                "to":          t.to_phase.value,
                "viewers":     t.viewer_count,
                "credits_unlocked": CurrencyService.format(t.locked_credits_converted, "USD"),
                "bonus_paid":  CurrencyService.format(t.bonus_paid, "USD"),
                "at":          t.transitioned_at.isoformat(),
            }
            for t in transitions
        ],
    }


# ─────────────────────────────────────────────
# CREATOR EARNINGS
# ─────────────────────────────────────────────

@router.get("/my-earnings")
def get_my_earnings(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    state  = _get_state(db)
    phase  = state.phase
    config = PHASE_CONFIG[phase]

    # All credits by status
    ledger = db.query(CreatorCreditLedger).filter(
        CreatorCreditLedger.creator_id == user.id
    ).all()

    locked    = sum(l.credit_portion for l in ledger if l.status == CreditStatus.locked)
    available = sum(l.cash_portion   for l in ledger if l.status in (CreditStatus.partial, CreditStatus.available))
    paid_out  = sum(l.amount_cents   for l in ledger if l.status == CreditStatus.paid_out)
    total     = sum(l.amount_cents   for l in ledger)

    # By phase
    by_phase = {}
    for p in PlatformPhase:
        phase_credits = [l for l in ledger if l.phase_earned == p]
        if phase_credits:
            by_phase[p.value] = CurrencyService.format(sum(l.amount_cents for l in phase_credits), currency)

    # Founding creator status
    founding = db.query(FoundingCreator).filter(
        FoundingCreator.user_id == user.id
    ).first()

    return {
        "currency":       currency,
        "current_phase":  phase.value,
        "creator_share":  f"{config['creator_share']}%",
        "earnings": {
            "total":      CurrencyService.to_display(total, currency),
            "locked":     CurrencyService.to_display(locked, currency),
            "available":  CurrencyService.to_display(available, currency),
            "paid_out":   CurrencyService.to_display(paid_out, currency),
        },
        "by_phase":       by_phase,
        "cash_payouts_active": phase != PlatformPhase.foundation,
        "min_payout":     CurrencyService.format(config["min_payout"], currency),
        "is_founding_creator": bool(founding),
        "founding_bonus_applied": founding.bonus_applied if founding else False,
        "founding_bonus_amount": CurrencyService.format(founding.bonus_amount, currency) if founding else None,
        "next_phase_info": _next_phase_info(state, currency),
    }


def _next_phase_info(state: PlatformState, currency: str) -> dict:
    phase = state.phase
    if phase == PlatformPhase.standard:
        return {"message": "Maximum phase reached. Full 80% creator share active."}

    next_phases = {
        PlatformPhase.foundation:  (PlatformPhase.momentum,    25_000,  "60% share, partial cash payouts unlock"),
        PlatformPhase.momentum:    (PlatformPhase.full_launch,  75_000,  "70% share, full cash + all locked credits convert"),
        PlatformPhase.full_launch: (PlatformPhase.standard,    150_000, "80% share — full FlintX model"),
    }
    next_phase, threshold, unlock = next_phases[phase]
    needed = max(0, threshold - state.collective_viewers)

    return {
        "next_phase":   next_phase.value,
        "viewers_needed": f"{needed:,}",
        "unlocks":      unlock,
        "threshold":    f"{threshold:,}",
    }


@router.get("/my-credits")
def get_my_credits(
    currency: str = "USD",
    page: int = 1,
    limit: int = 20,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    limit  = min(limit, 50)
    offset = (page - 1) * limit

    credits = db.query(CreatorCreditLedger).filter(
        CreatorCreditLedger.creator_id == user.id
    ).order_by(desc(CreatorCreditLedger.earned_at)).offset(offset).limit(limit).all()

    total = db.query(CreatorCreditLedger).filter(
        CreatorCreditLedger.creator_id == user.id
    ).count()

    return {
        "currency": currency,
        "credits":  [
            {
                "id":             c.id,
                "amount":         CurrencyService.to_display(c.amount_cents, currency),
                "phase_earned":   c.phase_earned.value,
                "status":         c.status.value,
                "cash_portion":   CurrencyService.to_display(c.cash_portion, currency),
                "credit_portion": CurrencyService.to_display(c.credit_portion, currency),
                "source":         c.source,
                "founding_bonus": CurrencyService.to_display(c.conversion_bonus, currency) if c.conversion_bonus else None,
                "earned_at":      c.earned_at.isoformat(),
                "converted_at":   c.converted_at.isoformat() if c.converted_at else None,
            }
            for c in credits
        ],
        "total": total,
        "page":  page,
        "pages": (total // limit) + 1,
    }


# ─────────────────────────────────────────────
# PAYOUT REQUEST
# ─────────────────────────────────────────────

class PayoutRequest(BaseModel):
    method:       str   # wise | paypal
    email:        str
    currency:     str = "USD"


@router.post("/request")
def request_payout(
    req: PayoutRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    state  = _get_state(db)
    phase  = state.phase
    config = PHASE_CONFIG[phase]

    if phase == PlatformPhase.foundation:
        return {
            "blocked": True,
            "reason":  "Cash payouts are not yet active.",
            "current_phase": "Phase 1 — Foundation",
            "unlocks_at":    f"25,000 collective viewers",
            "current_viewers": f"{state.collective_viewers:,}",
            "viewers_needed": f"{max(0, 25_000 - state.collective_viewers):,}",
            "your_locked_credits": CurrencyService.format(
                db.query(func.coalesce(func.sum(CreatorCreditLedger.amount_cents), 0)).filter(
                    CreatorCreditLedger.creator_id == user.id,
                    CreatorCreditLedger.status == CreditStatus.locked,
                ).scalar() or 0, req.currency
            ),
            "message": "Your earnings are accumulating in your wallet. Cash payouts activate at 25,000 collective viewers.",
        }

    min_payout = config["min_payout"]

    # Calculate available cash (Phase 2: cash_portion only; Phase 3/4: full balance)
    if phase == PlatformPhase.momentum:
        available = db.query(func.coalesce(func.sum(CreatorCreditLedger.cash_portion), 0)).filter(
            CreatorCreditLedger.creator_id == user.id,
            CreatorCreditLedger.status.in_([CreditStatus.partial, CreditStatus.available]),
        ).scalar() or 0
    else:
        available = user.wallet_balance

    if available < min_payout:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum payout in Phase {config['phase_num']} is {CurrencyService.format(min_payout, req.currency)}. "
                   f"Available: {CurrencyService.format(available, req.currency)}"
        )

    # Check for pending payout
    from ..database.models import PayoutRequest as PayoutRequestModel
    existing = db.query(PayoutRequestModel).filter(
        PayoutRequestModel.user_id == user.id,
        PayoutRequestModel.status  == "pending",
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="You already have a pending payout request")

    # Apply founding creator bonus on first withdrawal
    founding = db.query(FoundingCreator).filter(
        FoundingCreator.user_id == user.id,
        FoundingCreator.bonus_applied == False,
    ).first()

    bonus = 0
    if founding and phase.value in ("full_launch", "standard"):
        bonus = int(available * 0.10)   # 10% bonus
        founding.bonus_applied = True
        founding.bonus_amount  = bonus
        founding.bonus_paid_at = datetime.utcnow()
        available += bonus
        state.total_locked_credits -= bonus   # reduce liability

    payout = PayoutRequestModel(
        user_id = user.id,
        amount  = available,
        method  = req.method,
        status  = "pending",
        email   = encrypt_field(req.email),  # encrypted at rest
    )
    db.add(payout)

    # Deduct from wallet
    user.wallet_balance -= (available - bonus)
    state.total_paid_out += available

    # Mark credits as paid out
    credits_to_mark = db.query(CreatorCreditLedger).filter(
        CreatorCreditLedger.creator_id == user.id,
        CreatorCreditLedger.status.in_([CreditStatus.partial, CreditStatus.available]),
    ).all()
    for c in credits_to_mark:
        c.status = CreditStatus.paid_out

    db.add(Transaction(
        user_id      = user.id,
        type         = TxnType.payout_wise if req.method == "wise" else TxnType.payout_paypal,
        amount       = -available,
        balance_after = user.wallet_balance,
        description  = f"Payout — Phase {config['phase_num']} {config['name']}",
    ))

    db.commit()

    result = {
        "payout_id":     payout.id,
        "amount":        CurrencyService.to_display(available, req.currency),
        "method":        req.method,
        "status":        "pending",
        "phase":         phase.value,
        "message":       "Payout request received. Processed within 1 business day.",
    }
    if bonus:
        result["founding_bonus"] = CurrencyService.format(bonus, req.currency)
        result["founding_message"] = "🎉 10% Founding Creator bonus applied to your first withdrawal!"

    return result


# ─────────────────────────────────────────────
# FOUNDING CREATOR STATUS
# ─────────────────────────────────────────────

@router.get("/founding")
def get_founding_status(
    currency: str = "USD",
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    founding = db.query(FoundingCreator).filter(
        FoundingCreator.user_id == user.id
    ).first()
    state = _get_state(db)

    if not founding:
        return {
            "is_founding_creator": False,
            "message": "You joined after Phase 1. Standard payout terms apply.",
        }

    return {
        "is_founding_creator":   True,
        "joined_at":             founding.joined_at.isoformat(),
        "viewers_at_join":       f"{founding.viewer_count_at_join:,}",
        "bonus_rate":            "10%",
        "bonus_applied":         founding.bonus_applied,
        "bonus_amount":          CurrencyService.format(founding.bonus_amount, currency),
        "bonus_paid_at":         founding.bonus_paid_at.isoformat() if founding.bonus_paid_at else None,
        "locked_earnings":       CurrencyService.format(founding.earnings_locked, currency),
        "perks": [
            "10% bonus on first withdrawal — locked credits convert at 110%",
            "FlintX Founding Creator badge on your profile",
            "Priority access to premium advertiser category exclusivity deals",
            "Early access to new platform features",
            "Founding Creator leaderboard recognition",
        ],
        "current_phase": state.phase.value,
        "bonus_activates": "Phase 3 (Full Launch) or Phase 4 (Standard)",
    }


# ─────────────────────────────────────────────
# VIEWER CREDIT CASHOUT
# ─────────────────────────────────────────────

class ViewerCashoutRequest(BaseModel):
    method: str   # paypal
    email:  str
    currency: str = "USD"


@router.post("/viewer-cashout")
def viewer_cashout(
    req: ViewerCashoutRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    state  = _get_state(db)
    phase  = state.phase
    config = PHASE_CONFIG[phase]
    viewer_min = config["viewer_min"]

    if viewer_min == 0:
        return {
            "blocked": True,
            "reason":  "Viewer cashouts are not yet active.",
            "unlocks_at": "25,000 collective viewers (Phase 2)",
            "current_viewers": f"{state.collective_viewers:,}",
            "your_credits": CurrencyService.format(user.wallet_balance, req.currency),
            "message": "Your credits are accumulating. Cashout activates at Phase 2.",
        }

    if user.wallet_balance < viewer_min:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum cashout is {CurrencyService.format(viewer_min, req.currency)}. "
                   f"Balance: {CurrencyService.format(user.wallet_balance, req.currency)}"
        )

    amount = user.wallet_balance
    user.wallet_balance = 0
    state.total_paid_out += amount

    from ..database.models import PayoutRequest as PayoutRequestModel
    payout = PayoutRequestModel(
        user_id = user.id,
        amount  = amount,
        method  = req.method,
        status  = "pending",
    )
    db.add(payout)

    db.add(Transaction(
        user_id      = user.id,
        type         = TxnType.payout_paypal,
        amount       = -amount,
        balance_after = 0,
        description  = "FlintX Pass viewer credit cashout",
    ))
    db.commit()

    return {
        "cashed_out": True,
        "amount":     CurrencyService.to_display(amount, req.currency),
        "method":     req.method,
        "message":    "Viewer credit cashout requested. Processed within 1 business day via PayPal.",
    }


# ─────────────────────────────────────────────
# RECORD UNIQUE VIEWER (called from video view route)
# ─────────────────────────────────────────────

def record_unique_viewer(db: Session, user_id: str, watch_seconds: int, background_tasks=None):
    """
    Called when a registered user watches 60+ seconds of content.
    Increments the collective viewer count if this is their first qualifying watch.
    Triggers phase check after every increment.
    """
    if not user_id:
        return  # anonymous views don't count

    existing = db.query(UniqueViewerRecord).filter(
        UniqueViewerRecord.user_id == user_id
    ).first()

    if existing:
        existing.total_watch_s += watch_seconds
        db.commit()
        return  # already counted

    if watch_seconds < 60:
        return  # minimum 60 seconds

    # New unique viewer
    record = UniqueViewerRecord(
        user_id       = user_id,
        total_watch_s = watch_seconds,
    )
    db.add(record)

    # Increment platform counter
    state = _get_state(db)
    state.collective_viewers += 1
    db.commit()

    # Check if phase should advance
    if background_tasks:
        background_tasks.add_task(_check_phase_transition, state.collective_viewers)
    else:
        _check_phase_transition_sync(db, state.collective_viewers)


def _check_phase_transition_sync(db: Session, viewer_count: int):
    """Synchronous phase check — called inline."""
    state = _get_state(db)
    current = state.phase

    new_phase = current
    if viewer_count >= 150_000 and current != PlatformPhase.standard:
        new_phase = PlatformPhase.standard
    elif viewer_count >= 75_000 and current in (PlatformPhase.foundation, PlatformPhase.momentum):
        new_phase = PlatformPhase.full_launch
    elif viewer_count >= 25_000 and current == PlatformPhase.foundation:
        new_phase = PlatformPhase.momentum

    if new_phase != current:
        _execute_phase_transition(db, current, new_phase, viewer_count)


def _check_phase_transition(viewer_count: int):
    """Background task version."""
    from ..database.connection import SessionLocal
    db = SessionLocal()
    try:
        _check_phase_transition_sync(db, viewer_count)
    except Exception as e:
        print(f"[PHASE CHECK ERROR] {e}")
    finally:
        db.close()


def _execute_phase_transition(db: Session, from_phase: PlatformPhase, to_phase: PlatformPhase, viewer_count: int):
    """Execute a phase transition — convert credits, log the event, send notifications."""
    state = _get_state(db)
    state.phase = to_phase
    state.phase_activated_at = datetime.utcnow()

    credits_converted = 0
    bonus_paid = 0

    # Phase 3: convert all locked Phase 1 credits to available cash
    if to_phase == PlatformPhase.full_launch:
        locked = db.query(CreatorCreditLedger).filter(
            CreatorCreditLedger.status == CreditStatus.locked
        ).all()

        for credit in locked:
            # Check if founding creator
            founding = db.query(FoundingCreator).filter(
                FoundingCreator.user_id == credit.creator_id
            ).first()
            is_founding = bool(founding)

            # Convert locked credit
            credit.status       = CreditStatus.available
            credit.cash_portion = credit.credit_portion
            credit.credit_portion = 0
            credit.converted_at = datetime.utcnow()
            credits_converted  += credit.amount_cents

            # Add to wallet balance
            creator = db.query(User).filter(User.id == credit.creator_id).first()
            if creator:
                creator.wallet_balance += credit.cash_portion

        state.total_locked_credits = 0

    # Log the milestone
    db.add(ViewerMilestone(
        milestone_type  = MilestoneType(f"phase_{PHASE_CONFIG[to_phase]['phase_num']}_unlock"),
        viewer_count    = viewer_count,
        phase_triggered = to_phase,
        description     = f"Phase {PHASE_CONFIG[to_phase]['phase_num']} — {PHASE_CONFIG[to_phase]['name']} activated at {viewer_count:,} collective viewers",
    ))

    # Log the transition
    db.add(PhaseTransitionLog(
        from_phase               = from_phase,
        to_phase                 = to_phase,
        viewer_count             = viewer_count,
        locked_credits_converted = credits_converted,
        bonus_paid               = bonus_paid,
    ))

    db.commit()
    print(f"[PHASE TRANSITION] {from_phase.value} → {to_phase.value} at {viewer_count:,} viewers")

    # In production: send email to all creators, post to social, etc.
    _announce_phase_transition(to_phase, viewer_count, credits_converted)


def _announce_phase_transition(phase: PlatformPhase, viewers: int, credits_unlocked: int):
    """In production: send emails, push notifications, social posts."""
    print(f"[ANNOUNCE] Phase {PHASE_CONFIG[phase]['phase_num']} activated — {viewers:,} viewers — ${credits_unlocked/100:,.2f} unlocked")


# ─────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────

@router.get("/admin/phase")
def admin_phase_dashboard(
    currency: str = "USD",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    state = _get_state(db)

    # Creator earnings by phase
    by_phase = {}
    for p in PlatformPhase:
        total = db.query(func.coalesce(func.sum(CreatorCreditLedger.amount_cents), 0)).filter(
            CreatorCreditLedger.phase_earned == p
        ).scalar() or 0
        by_phase[p.value] = CurrencyService.format(total, currency)

    # Locked credits liability
    locked = db.query(func.coalesce(func.sum(CreatorCreditLedger.amount_cents), 0)).filter(
        CreatorCreditLedger.status == CreditStatus.locked
    ).scalar() or 0

    # Founding creators
    founding_count = db.query(func.count(FoundingCreator.id)).scalar() or 0
    founding_pending_bonus = db.query(
        func.coalesce(func.sum(CreatorCreditLedger.amount_cents * 0.10), 0)
    ).join(FoundingCreator, FoundingCreator.user_id == CreatorCreditLedger.creator_id).filter(
        FoundingCreator.bonus_applied == False,
        CreatorCreditLedger.status == CreditStatus.locked,
    ).scalar() or 0

    return {
        "current_phase":      _phase_dict(state, currency),
        "earnings_by_phase":  by_phase,
        "locked_liability":   CurrencyService.format(locked, currency),
        "total_paid_out":     CurrencyService.format(state.total_paid_out, currency),
        "founding_creators":  founding_count,
        "pending_bonus_liability": CurrencyService.format(int(founding_pending_bonus), currency),
        "projections": {
            "phase_2_at":  "25,000 viewers",
            "phase_3_at":  "75,000 viewers",
            "phase_4_at":  "150,000 viewers",
            "current":     f"{state.collective_viewers:,} viewers",
        }
    }


@router.get("/admin/liability")
def admin_liability(
    currency: str = "USD",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    state = _get_state(db)

    locked = db.query(func.coalesce(func.sum(CreatorCreditLedger.amount_cents), 0)).filter(
        CreatorCreditLedger.status == CreditStatus.locked
    ).scalar() or 0

    partial_cash = db.query(func.coalesce(func.sum(CreatorCreditLedger.cash_portion), 0)).filter(
        CreatorCreditLedger.status == CreditStatus.partial
    ).scalar() or 0

    partial_locked = db.query(func.coalesce(func.sum(CreatorCreditLedger.credit_portion), 0)).filter(
        CreatorCreditLedger.status == CreditStatus.partial
    ).scalar() or 0

    total_liability = locked + partial_locked

    return {
        "currency":           currency,
        "phase_1_locked":     CurrencyService.format(locked, currency),
        "phase_2_cash_owed":  CurrencyService.format(partial_cash, currency),
        "phase_2_still_locked": CurrencyService.format(partial_locked, currency),
        "total_locked_liability": CurrencyService.format(total_liability, currency),
        "total_paid_out":     CurrencyService.format(state.total_paid_out, currency),
        "collective_viewers": state.collective_viewers,
        "viewers_to_phase_2": max(0, 25_000 - state.collective_viewers),
        "viewers_to_phase_3": max(0, 75_000 - state.collective_viewers),
        "viewers_to_phase_4": max(0, 150_000 - state.collective_viewers),
    }


class OverrideRequest(BaseModel):
    phase:  str
    reason: str


@router.post("/admin/override")
def admin_override_phase(
    req: OverrideRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Emergency phase override. Logged immutably."""
    if req.phase not in [p.value for p in PlatformPhase]:
        raise HTTPException(status_code=422, detail="Invalid phase")

    state = _get_state(db)
    old_phase = state.phase
    new_phase = PlatformPhase(req.phase)
    state.phase = new_phase
    state.phase_activated_at = datetime.utcnow()

    db.add(PhaseTransitionLog(
        from_phase   = old_phase,
        to_phase     = new_phase,
        viewer_count = state.collective_viewers,
        notes        = f"ADMIN OVERRIDE by {admin.email}: {req.reason}",
    ))
    db.commit()

    return {"overridden": True, "from": old_phase.value, "to": new_phase.value}


@router.post("/admin/convert-all")
def admin_convert_all_credits(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Manually trigger Phase 3 credit conversion. Use when Phase 3 activates."""
    state = _get_state(db)
    if state.phase not in (PlatformPhase.full_launch, PlatformPhase.standard):
        raise HTTPException(status_code=400, detail="Phase 3 or 4 must be active to convert credits")

    locked = db.query(CreatorCreditLedger).filter(
        CreatorCreditLedger.status == CreditStatus.locked
    ).all()

    converted = 0
    for credit in locked:
        credit.status       = CreditStatus.available
        credit.cash_portion = credit.credit_portion
        credit.credit_portion = 0
        credit.converted_at = datetime.utcnow()
        converted          += credit.amount_cents

        creator = db.query(User).filter(User.id == credit.creator_id).first()
        if creator:
            creator.wallet_balance += credit.cash_portion

    state.total_locked_credits = 0
    db.commit()

    return {
        "converted":       len(locked),
        "total_converted": CurrencyService.format(converted, "USD"),
    }
