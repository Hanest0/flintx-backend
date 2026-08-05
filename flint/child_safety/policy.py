"""
FlintX Child Safety Policy
════════════════════════════════════════════════════════════════════════
THIS IS THE HIGHEST LAW IN FLINTX.
No feature, no revenue, no business decision overrides child safety.
════════════════════════════════════════════════════════════════════════

MINOR CREATOR MODEL — Updated:
  Under 13  — Creator with FULL parental consent + Level 5 moderation
              AI + 2 human reviewers + parent approves EACH video
  13–15     — Viewer by default. Creator with parental consent + Level 4
  16–17     — Creator with parental consent + Level 3
  18+       — Full creator, standard moderation

  ALL minors earn 50% (parent receives payouts).
  FlintX keeps extra 30% vs adult rate as platform fee — not a trust.
  Rate auto-upgrades to 80% on 18th birthday.

MODERATION LEVELS:
  Level 5 (Under 13) : AI + 2 humans + parent approves each video
  Level 4 (13–15)    : AI + 2 human reviewers
  Level 3 (16–17)    : AI + 1 human reviewer
  Level 1 (18+)      : AI standard moderation

Legal frameworks: COPPA | GDPR-K | UK Children's Code |
  Australia Online Safety Act | France Digital Age Consent |
  Denmark Social Media Ban | KOSA | UN Convention on Rights of the Child

Blocked: Iran | North Korea | China — permanent, no override possible.
"""

from datetime import date
from typing import Optional


class AgeThreshold:
    PARENTAL_CONSENT_AGE = 18
    MINIMUM_PAYOUT_AGE   = 18
    KIDS_CORNER_MAX_AGE  = 12


class ModerationLevel:
    STANDARD  = 1
    ELEVATED  = 2
    HIGH      = 3
    VERY_HIGH = 4
    MAXIMUM   = 5

    @staticmethod
    def for_age(age: int) -> int:
        if age < 13: return 5
        if age < 16: return 4
        if age < 18: return 3
        return 1

    @staticmethod
    def description(level: int) -> str:
        descriptions = {
            1: "AI moderation",
            2: "AI + human review on flags",
            3: "AI + 1 human reviewer before publishing",
            4: "AI + 2 human reviewers before publishing",
            5: "AI + 2 human reviewers + parent approves each video",
        }
        return descriptions.get(level, "Unknown")


class RevenueShare:
    @staticmethod
    def for_age(age: int) -> dict:
        if age < 18:
            return {
                "creator_rate": 0.50,
                "flintx_rate":  0.50,
                "payout_to":    "parent_guardian",
                "note":         "50% to parent/guardian. Auto-upgrades to 80% on 18th birthday.",
            }
        return {
            "creator_rate": 0.80,
            "flintx_rate":  0.20,
            "payout_to":    "creator",
            "note":         "Standard 80% creator rate.",
        }


BLOCKED_COUNTRIES = {
    "IR": "Iran — OFAC sanctions. FlintX cannot legally operate here.",
    "KP": "North Korea — UN and US sanctions. FlintX cannot legally operate here.",
    "CN": "China — data localisation laws incompatible with FlintX.",
}

ENHANCED_CHILD_PROTECTION = {
    "AU": {"min_social_age": 16, "law": "Australia Online Safety Act"},
    "FR": {"min_social_age": 15, "law": "France Digital Age Consent"},
    "DK": {"min_social_age": 15, "law": "Denmark Social Media Ban"},
    "GB": {"min_social_age": 13, "law": "UK Children's Code"},
    "US": {"min_social_age": 0,  "law": "COPPA — parental consent required under 13"},
    "DE": {"min_social_age": 16, "law": "GDPR-K strict"},
    "NL": {"min_social_age": 16, "law": "GDPR-K Netherlands"},
}

KIDS_CORNER_NICHES = [
    "Education", "Science", "Mathematics", "Art", "Music",
    "Language Learning", "Sports", "Animals & Nature",
    "Stories & Books", "DIY & Crafts",
]

KIDS_CORNER_RULES = {
    "no_advertising":           True,
    "no_social_features":       True,
    "no_live_streaming":        True,
    "no_external_links":        True,
    "no_algorithmic_recs":      True,
    "no_personal_data":         True,
    "human_review_required":    True,
    "ai_moderation_first":      True,
    "no_payments_to_minors":    True,
    "parent_approval_required": True,
    "can_auto_approve":         False,
}

PAYMENT_AGE_BY_COUNTRY = {
    "DEFAULT": 18,
    "AE": 21,
}

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
    return PAYMENT_AGE_BY_COUNTRY.get(country_code.upper(), 18)


def check_account_eligibility(
    dob: date,
    country_code: str,
    parental_consent: bool = False,
    parent_age: Optional[int] = None,
) -> dict:
    today = date.today()
    age = (today - dob).days // 365
    code = country_code.upper()

    # Step 1: Country check
    allowed, block_reason = check_country_allowed(code)
    if not allowed:
        return {
            "can_create_account":   False,
            "can_create_content":   False,
            "can_receive_payments": False,
            "age": age, "reason": block_reason,
            "moderation_level": 0,
        }

    # Step 2: Regional minimum age
    enhanced = ENHANCED_CHILD_PROTECTION.get(code, {})
    min_social = enhanced.get("min_social_age", 0)
    law = enhanced.get("law", "")

    if age < min_social and not parental_consent:
        return {
            "can_create_account":    False,
            "can_create_content":    False,
            "can_receive_payments":  False,
            "age": age,
            "reason": f"In your region users must be {min_social}+ without parental consent. ({law}) Parents can consent for younger creators.",
            "requires_parental_consent": True,
            "moderation_level": ModerationLevel.MAXIMUM,
        }

    # Step 3: Under 13 — parental consent required
    if age < 13:
        if not parental_consent:
            return {
                "can_create_account":    False,
                "can_create_content":    False,
                "can_receive_payments":  False,
                "age": age,
                "reason": "Parental consent required for creators under 13. A parent or guardian must verify their identity and approve all content before it is published.",
                "requires_parental_consent": True,
                "moderation_level": ModerationLevel.MAXIMUM,
            }
        if not parent_age or parent_age < 18:
            return {
                "can_create_account":   False,
                "can_create_content":   False,
                "can_receive_payments": False,
                "age": age,
                "reason": "The parent or guardian providing consent must be 18 or older.",
                "moderation_level": 0,
            }
        mod = ModerationLevel.MAXIMUM
        return {
            "can_create_account":       True,
            "can_create_content":       True,
            "can_receive_payments":     False,
            "parent_receives_payout":   True,
            "age":                      age,
            "reason":                   None,
            "revenue_share":            RevenueShare.for_age(age),
            "moderation_level":         mod,
            "moderation_description":   ModerationLevel.description(mod),
            "content_restrictions": [
                "parent_approves_each_video_before_publish",
                "kids_corner_eligible_content_only",
                "no_live_streaming",
                "no_direct_messaging",
                "no_social_features",
                "no_personal_data_collected",
            ],
            "note": "All content: AI screening + 2 human reviews + parent approval. Nothing publishes without all three passing.",
        }

    # Step 4: 13–15
    if age < 16:
        if not parental_consent:
            return {
                "can_create_account":    True,
                "can_create_content":    False,
                "can_receive_payments":  False,
                "age": age,
                "reason": "Viewer account only. Parental consent required to create content.",
                "requires_parental_consent": True,
                "moderation_level": ModerationLevel.STANDARD,
                "restricted_mode": True,
            }
        mod = ModerationLevel.VERY_HIGH
        return {
            "can_create_account":       True,
            "can_create_content":       True,
            "can_receive_payments":     False,
            "parent_receives_payout":   True,
            "age":                      age,
            "reason":                   None,
            "revenue_share":            RevenueShare.for_age(age),
            "moderation_level":         mod,
            "moderation_description":   ModerationLevel.description(mod),
        }

    # Step 5: 16–17
    if age < 18:
        if not parental_consent:
            return {
                "can_create_account":    True,
                "can_create_content":    True,
                "can_receive_payments":  False,
                "age": age,
                "reason": "Parental consent required for payouts under 18.",
                "requires_parental_consent": True,
                "moderation_level": ModerationLevel.HIGH,
            }
        mod = ModerationLevel.HIGH
        return {
            "can_create_account":       True,
            "can_create_content":       True,
            "can_receive_payments":     False,
            "parent_receives_payout":   True,
            "age":                      age,
            "reason":                   None,
            "revenue_share":            RevenueShare.for_age(age),
            "moderation_level":         mod,
            "moderation_description":   ModerationLevel.description(mod),
        }

    # Step 6: 18+ full access
    mod = ModerationLevel.STANDARD
    return {
        "can_create_account":       True,
        "can_create_content":       True,
        "can_receive_payments":     True,
        "age":                      age,
        "reason":                   None,
        "revenue_share":            RevenueShare.for_age(age),
        "moderation_level":         mod,
        "moderation_description":   ModerationLevel.description(mod),
    }


def check_kids_corner_eligibility(title: str, description: str,
                                   niche: str, tags: list) -> dict:
    eligible_niche = niche in KIDS_CORNER_NICHES
    text = f"{title} {description} {' '.join(tags or [])}".lower()
    flags = [t for t in DISQUALIFYING_TERMS if t in text]
    return {
        "eligible":               eligible_niche and len(flags) == 0,
        "eligible_niche":         eligible_niche,
        "flags":                  flags,
        "requires_ai_review":     True,
        "requires_human_review":  True,
        "requires_parent_approval": True,
        "can_auto_approve":       False,
    }


def get_minor_revenue_share(age: int) -> dict:
    return RevenueShare.for_age(age)
