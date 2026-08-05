"""
FlintX Child Safety Policy
════════════════════════════════════════════════════════════════════════
THIS IS THE HIGHEST LAW IN FLINTX.
No feature, no revenue, no business decision overrides child safety.
════════════════════════════════════════════════════════════════════════

Legal frameworks:
  COPPA (US) | GDPR-K (EU) | UK Children's Code
  Australia Online Safety Act | France Digital Age Consent
  Denmark Social Media Ban | KOSA (US) | UN Convention on Rights of the Child

Blocked countries — permanent, non-negotiable:
  Iran (OFAC sanctions) | North Korea (UN+US sanctions) | China (incompatible laws)
"""

from datetime import date
from typing import Optional


# ── Age Thresholds ────────────────────────────────────────────────────
class AgeThreshold:
    MINIMUM_ACCOUNT_AGE = 13    # COPPA absolute — no account under 13 anywhere
    MINIMUM_CREATOR_AGE = 16    # No content upload under 16 — universal
    MINIMUM_PAYOUT_AGE  = 18    # No payments to anyone under 18 — no exceptions globally
    KIDS_CORNER_MAX_AGE = 12    # Kids Corner: content for 12 and under only


# ── Blocked Countries — Cannot be overridden by any business decision ─
BLOCKED_COUNTRIES = {
    "IR": "Iran — OFAC US sanctions make it illegal to process payments or operate.",
    "KP": "North Korea — UN and US sanctions. Cannot operate legally.",
    "CN": "China — data localisation laws and firewall incompatible with FlintX.",
}


# ── Regional Child Protection Laws (2026) ─────────────────────────────
ENHANCED_CHILD_PROTECTION = {
    "AU": {"min_social_age": 16, "law": "Australia Online Safety Act — under-16 ban effective Dec 2025"},
    "FR": {"min_social_age": 15, "law": "France Digital Age Consent — mandatory age verification from Sep 2026"},
    "DK": {"min_social_age": 15, "law": "Denmark Social Media Ban — effective mid-2026"},
    "GB": {"min_social_age": 13, "law": "UK Children's Code (AADC) — Ofcom enforcement active Jul 2025"},
    "US": {"min_social_age": 13, "law": "COPPA + KOSA (passed 91-3 Senate vote)"},
    "DE": {"min_social_age": 16, "law": "GDPR-K strict interpretation"},
    "NL": {"min_social_age": 16, "law": "GDPR-K Netherlands"},
}


# ── Kids Corner — Allowed Content Niches ─────────────────────────────
KIDS_CORNER_NICHES = [
    "Education", "Science", "Mathematics", "Art", "Music",
    "Language Learning", "Sports", "Animals & Nature",
    "Stories & Books", "DIY & Crafts",
]


# ── Kids Corner — Absolute Rules (no exceptions, no overrides) ────────
KIDS_CORNER_RULES = {
    "no_advertising":         True,
    "no_social_features":     True,
    "no_live_streaming":      True,
    "no_external_links":      True,
    "no_algorithmic_recs":    True,
    "no_personal_data":       True,
    "human_review_required":  True,
    "ai_moderation_first":    True,
    "no_payments_to_minors":  True,
    "can_auto_approve":       False,
}


# ── Payment Age by Country ────────────────────────────────────────────
PAYMENT_AGE_BY_COUNTRY = {
    "DEFAULT": 18,
    "AE": 21,   # UAE — age of majority 21
}


# ── Disqualifying Terms for Kids Corner ──────────────────────────────
DISQUALIFYING_TERMS = [
    "violence", "violent", "weapon", "gun", "knife", "blood",
    "sex", "sexual", "nude", "naked", "porn", "adult",
    "alcohol", "drug", "smoking", "vaping", "gambling",
    "horror", "scary", "death", "kill", "murder", "hate",
    "racist", "suicide", "self-harm", "18+", "mature",
]


def check_country_allowed(country_code: str) -> tuple:
    code = country_code.upper()
    if code in BLOCKED_COUNTRIES:
        return False, BLOCKED_COUNTRIES[code]
    return True, None


def get_min_payment_age(country_code: str) -> int:
    return PAYMENT_AGE_BY_COUNTRY.get(country_code.upper(), PAYMENT_AGE_BY_COUNTRY["DEFAULT"])


def check_account_eligibility(dob: date, country_code: str) -> dict:
    today = date.today()
    age = (today - dob).days // 365
    code = country_code.upper()

    # Blocked country — no access at all
    allowed, block_reason = check_country_allowed(code)
    if not allowed:
        return {"can_create_account": False, "can_create_content": False,
                "can_receive_payments": False, "age": age, "reason": block_reason}

    # Regional minimum age
    enhanced = ENHANCED_CHILD_PROTECTION.get(code, {})
    min_social = enhanced.get("min_social_age", AgeThreshold.MINIMUM_ACCOUNT_AGE)

    if age < AgeThreshold.MINIMUM_ACCOUNT_AGE:
        return {"can_create_account": False, "can_create_content": False,
                "can_receive_payments": False, "age": age,
                "reason": f"FlintX requires users to be at least {AgeThreshold.MINIMUM_ACCOUNT_AGE} years old."}

    if age < min_social:
        law = enhanced.get("law", "")
        return {"can_create_account": False, "can_create_content": False,
                "can_receive_payments": False, "age": age,
                "reason": f"In your region users must be {min_social}+ to join. ({law})"}

    if age < AgeThreshold.MINIMUM_CREATOR_AGE:
        return {"can_create_account": True, "can_create_content": False,
                "can_receive_payments": False, "age": age,
                "reason": "Viewer account only. Creators must be 16+.",
                "restricted_mode": True}

    if age < AgeThreshold.MINIMUM_PAYOUT_AGE:
        return {"can_create_account": True, "can_create_content": True,
                "can_receive_payments": False, "age": age,
                "reason": "Content creation allowed. Payouts require age 18+. Earnings held safely until then.",
                "earnings_held": True}

    min_pay = get_min_payment_age(code)
    if age < min_pay:
        return {"can_create_account": True, "can_create_content": True,
                "can_receive_payments": False, "age": age,
                "reason": f"In your region payouts require age {min_pay}+. Earnings held until then.",
                "earnings_held": True}

    return {"can_create_account": True, "can_create_content": True,
            "can_receive_payments": True, "age": age, "reason": None}


def check_kids_corner_eligibility(title: str, description: str, niche: str, tags: list) -> dict:
    eligible_niche = niche in KIDS_CORNER_NICHES
    text = f"{title} {description} {' '.join(tags or [])}".lower()
    flags = [t for t in DISQUALIFYING_TERMS if t in text]
    return {
        "eligible": eligible_niche and len(flags) == 0,
        "eligible_niche": eligible_niche,
        "flags": flags,
        "requires_human_review": True,
        "can_auto_approve": False,
    }
