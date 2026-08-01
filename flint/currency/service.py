"""
Flint — Global Currency Service

DESIGN DECISION:
  All money stored internally as INTEGER USD CENTS.
  USD is the global standard for ad CPMs and platform billing.
  Display converts to the user's local currency at the point of output.

  Why integer cents, not floats?
  Floating point arithmetic is imprecise for money.
  Integer cents = exact. $1.00 = 100 cents. €1.50 = 150 cents.

SUPPORTED CURRENCIES (40+):
  Americas:    USD, CAD, MXN, BRL, ARS, CLP, COP
  Europe:      EUR, GBP, CHF, NOK, SEK, DKK, PLN, CZK, HUF, RON
  Asia:        JPY, CNY, INR, KRW, SGD, HKD, TWD, THB, MYR, IDR, PHP, VND, PKR, BDT
  Middle East: AED, SAR, QAR, KWD, ILS, EGP, TRY
  Africa:      ZAR, NGN, KES, GHS, TZS
  Oceania:     AUD, NZD

EXCHANGE RATES:
  Refreshed every 6 hours from Open Exchange Rates API (free tier = 1,000 req/mo).
  Falls back to hardcoded rates if the API is unavailable.
  Rates are stored in memory — not in the database.

USAGE:
  from flint.currency.service import CurrencyService

  # Convert 1000 USD cents to user's currency
  amount = CurrencyService.format(1000, "GBP")   # → "£7.84"
  raw    = CurrencyService.to_display(1000, "EUR")  # → {"amount": 9.25, "currency": "EUR", "symbol": "€"}

  # Convert FROM user's currency to USD cents (for storing)
  cents  = CurrencyService.to_usd_cents(29.99, "GBP")  # → 3820
"""

import os
import time
import httpx
from typing import Optional

# ─────────────────────────────────────────────
# CURRENCY METADATA
# ─────────────────────────────────────────────

CURRENCIES = {
    # code: (name, symbol, decimal_places, symbol_before)
    "USD": ("US Dollar",        "$",   2, True),
    "EUR": ("Euro",             "€",   2, True),
    "GBP": ("British Pound",   "£",   2, True),
    "CAD": ("Canadian Dollar", "CA$", 2, True),
    "AUD": ("Australian Dollar","A$",  2, True),
    "NZD": ("New Zealand Dollar","NZ$",2, True),
    "CHF": ("Swiss Franc",     "Fr.", 2, True),
    "JPY": ("Japanese Yen",    "¥",   0, True),
    "CNY": ("Chinese Yuan",    "¥",   2, True),
    "KRW": ("Korean Won",      "₩",   0, True),
    "INR": ("Indian Rupee",    "₹",   2, True),
    "SGD": ("Singapore Dollar","S$",  2, True),
    "HKD": ("Hong Kong Dollar","HK$", 2, True),
    "TWD": ("Taiwan Dollar",   "NT$", 0, True),
    "THB": ("Thai Baht",       "฿",   2, True),
    "MYR": ("Malaysian Ringgit","RM", 2, True),
    "IDR": ("Indonesian Rupiah","Rp", 0, True),
    "PHP": ("Philippine Peso", "₱",   2, True),
    "VND": ("Vietnamese Dong", "₫",   0, False),
    "PKR": ("Pakistani Rupee", "₨",   0, True),
    "BDT": ("Bangladeshi Taka","৳",   2, True),
    "SEK": ("Swedish Krona",   "kr",  2, False),
    "NOK": ("Norwegian Krone", "kr",  2, False),
    "DKK": ("Danish Krone",    "kr",  2, False),
    "PLN": ("Polish Złoty",    "zł",  2, False),
    "CZK": ("Czech Koruna",    "Kč",  2, False),
    "HUF": ("Hungarian Forint","Ft",  0, False),
    "RON": ("Romanian Leu",    "lei", 2, False),
    "MXN": ("Mexican Peso",    "MX$", 2, True),
    "BRL": ("Brazilian Real",  "R$",  2, True),
    "ARS": ("Argentine Peso",  "AR$", 2, True),
    "CLP": ("Chilean Peso",    "CL$", 0, True),
    "COP": ("Colombian Peso",  "COL$",0, True),
    "AED": ("UAE Dirham",      "AED", 2, True),
    "SAR": ("Saudi Riyal",     "SAR", 2, True),
    "QAR": ("Qatari Riyal",    "QAR", 2, True),
    "KWD": ("Kuwaiti Dinar",   "KD",  3, True),
    "ILS": ("Israeli Shekel",  "₪",   2, True),
    "EGP": ("Egyptian Pound",  "EGP", 2, True),
    "TRY": ("Turkish Lira",    "₺",   2, True),
    "ZAR": ("South African Rand","R", 2, True),
    "NGN": ("Nigerian Naira",  "₦",   2, True),
    "KES": ("Kenyan Shilling", "KSh", 2, True),
    "GHS": ("Ghanaian Cedi",   "GH₵", 2, True),
    "TZS": ("Tanzanian Shilling","TSh",0, True),
}

# Hardcoded fallback exchange rates (USD = 1.0 base)
# Updated: July 2025 — refresh periodically in .env if you want static rates
FALLBACK_RATES: dict[str, float] = {
    "USD": 1.000, "EUR": 0.920, "GBP": 0.785, "CAD": 1.362, "AUD": 1.530,
    "NZD": 1.660, "CHF": 0.897, "JPY": 150.50, "CNY": 7.240, "KRW": 1325.0,
    "INR": 83.50, "SGD": 1.346, "HKD": 7.812, "TWD": 32.10, "THB": 35.20,
    "MYR": 4.710, "IDR": 15850.0, "PHP": 56.50, "VND": 24650.0, "PKR": 278.0,
    "BDT": 109.5, "SEK": 10.45, "NOK": 10.62, "DKK": 6.880, "PLN": 4.020,
    "CZK": 22.80, "HUF": 358.0, "RON": 4.580, "MXN": 17.10, "BRL": 4.970,
    "ARS": 912.0, "CLP": 920.0, "COP": 3960.0, "AED": 3.673, "SAR": 3.751,
    "QAR": 3.641, "KWD": 0.307, "ILS": 3.700, "EGP": 30.90, "TRY": 32.10,
    "ZAR": 18.80, "NGN": 1480.0, "KES": 129.0, "GHS": 12.20, "TZS": 2680.0,
}

# Open Exchange Rates API (free tier — 1,000 requests/month)
# Sign up: openexchangerates.org — free tier is plenty
OXR_APP_ID  = os.getenv("OPEN_EXCHANGE_RATES_APP_ID", "")
OXR_URL     = "https://openexchangerates.org/api/latest.json"
RATE_TTL_S  = 6 * 3600   # refresh every 6 hours

# In-memory rate cache
_rate_cache: dict[str, float] = {}
_cache_ts: float = 0.0


def _fetch_rates() -> dict[str, float]:
    """Fetch fresh exchange rates from Open Exchange Rates API."""
    global _rate_cache, _cache_ts

    if _rate_cache and (time.time() - _cache_ts) < RATE_TTL_S:
        return _rate_cache

    if not OXR_APP_ID:
        return FALLBACK_RATES

    try:
        resp = httpx.get(OXR_URL, params={"app_id": OXR_APP_ID, "base": "USD"}, timeout=5)
        if resp.status_code == 200:
            rates       = resp.json().get("rates", {})
            _rate_cache = rates
            _cache_ts   = time.time()
            return rates
    except Exception as e:
        print(f"[CURRENCY] Rate fetch failed: {e}. Using fallback rates.")

    return FALLBACK_RATES


def get_rate(currency: str) -> float:
    """Get USD → currency exchange rate. Returns 1.0 for unknown currencies."""
    rates = _fetch_rates()
    return rates.get(currency.upper(), 1.0)


# ─────────────────────────────────────────────
# CURRENCY SERVICE
# ─────────────────────────────────────────────

class CurrencyService:

    @staticmethod
    def supported_currencies() -> list[dict]:
        """Return all supported currencies with metadata."""
        return [
            {
                "code":     code,
                "name":     meta[0],
                "symbol":   meta[1],
                "decimals": meta[2],
            }
            for code, meta in CURRENCIES.items()
        ]

    @staticmethod
    def is_supported(currency: str) -> bool:
        return currency.upper() in CURRENCIES

    @staticmethod
    def to_display(usd_cents: int, currency: str = "USD") -> dict:
        """
        Convert USD cents to a display-ready dict in the user's currency.

        Example:
            to_display(2900, "GBP")
            → {"amount": 22.77, "cents": 2277, "currency": "GBP", "symbol": "£",
               "formatted": "£22.77", "usd_cents": 2900}
        """
        currency = currency.upper()
        if currency not in CURRENCIES:
            currency = "USD"

        rate    = get_rate(currency)
        usd_amt = usd_cents / 100
        local   = usd_amt * rate

        meta    = CURRENCIES[currency]
        symbol  = meta[1]
        decimals = meta[2]
        sym_before = meta[3]

        # Round to currency's decimal places
        rounded = round(local, decimals)
        if decimals == 0:
            fmt_amt = f"{int(rounded):,}"
        else:
            fmt_amt = f"{rounded:,.{decimals}f}"

        formatted = f"{symbol}{fmt_amt}" if sym_before else f"{fmt_amt} {symbol}"

        return {
            "amount":     rounded,
            "currency":   currency,
            "symbol":     symbol,
            "formatted":  formatted,
            "usd_cents":  usd_cents,
        }

    @staticmethod
    def format(usd_cents: int, currency: str = "USD") -> str:
        """Shorthand — returns just the formatted string. e.g. '£22.77'"""
        return CurrencyService.to_display(usd_cents, currency)["formatted"]

    @staticmethod
    def to_usd_cents(local_amount: float, from_currency: str) -> int:
        """
        Convert a local currency amount to USD cents for storage.

        Example:
            to_usd_cents(29.99, "GBP")  → 3820 (USD cents)
            to_usd_cents(100, "EUR")    → 10870 (USD cents)
        """
        from_currency = from_currency.upper()
        rate = get_rate(from_currency)
        if rate == 0:
            return 0
        usd = local_amount / rate
        return int(round(usd * 100))

    @staticmethod
    def convert(usd_cents: int, to_currency: str) -> float:
        """Convert USD cents to a float in the target currency."""
        rate = get_rate(to_currency.upper())
        return round((usd_cents / 100) * rate, CURRENCIES.get(to_currency.upper(), ("", "", 2))[2])

    @staticmethod
    def cpm_for_currency(base_cpm_usd_cents: int, currency: str) -> dict:
        """
        Display a CPM in the user's currency with context.
        CPMs in Flint are stored as USD cents per 1,000 impressions.
        """
        return {
            "cpm_display":    CurrencyService.format(base_cpm_usd_cents, currency),
            "per_impression": CurrencyService.format(base_cpm_usd_cents // 1000, currency),
            "currency":       currency.upper(),
        }


# ─────────────────────────────────────────────
# NICHE CPM DATA (USD cents)
# ─────────────────────────────────────────────
# These are Flint's actual CPMs. Stored as USD cents per 1,000 impressions.
# Display converts to user's local currency.

# Complete FlintX niche CPM data (USD cents per 1,000 ad impressions)
# FlintX Pass opted-in viewer premium adds 1.18× to effective CPM
NICHE_CPM_USD_CENTS = {
    # Finance & Business
    "Personal Finance":      650,
    "Investing":             680,
    "Cryptocurrency":        590,
    "Entrepreneurship":      620,
    "Real Estate":           670,
    "Tax & Accounting":      700,
    "Insurance":             640,
    "Banking & Credit":      610,
    # Technology
    "AI & Machine Learning": 520,
    "Software Development":  510,
    "Cybersecurity":         540,
    "Gaming":                330,
    "Hardware & Reviews":    480,
    "Mobile Apps":           460,
    "Web Development":       500,
    "Data Science":          530,
    # Education
    "Online Learning":       490,
    "Language Learning":     440,
    "Science & Nature":      420,
    "History":               390,
    "Mathematics":           460,
    "Philosophy":            380,
    "Psychology":            470,
    "Law & Legal":           650,
    # Health & Lifestyle
    "Fitness & Workout":     410,
    "Nutrition & Diet":      400,
    "Mental Health":         430,
    "Yoga & Meditation":     370,
    "Medicine & Health":     550,
    "Parenting":             380,
    # Creative Arts
    "Music":                 305,
    "Drawing & Art":         310,
    "Photography":           340,
    "Video Production":      360,
    "Graphic Design":        350,
    "Animation":             330,
    "Writing":               320,
    "Podcasting":            290,
    # Food & Drink
    "Cooking & Recipes":     360,
    "Baking":                340,
    "Restaurants & Food":    330,
    "Wine & Spirits":        420,
    "Coffee":                310,
    # Travel & Outdoors
    "Travel Vlogs":          460,
    "Adventure & Hiking":    390,
    "Luxury Travel":         520,
    "Camping & Survival":    340,
    # Entertainment
    "Comedy":                280,
    "Movie Reviews":         290,
    "Anime":                 260,
    "Sports":                350,
    "True Crime":            310,
    "News & Politics":       330,
    "Book Reviews":          300,
    # Fashion & Beauty
    "Fashion":               360,
    "Beauty & Makeup":       370,
    "Skincare":              390,
    "Luxury & Lifestyle":    480,
    # Home & Family
    "Home Improvement":      430,
    "Interior Design":       420,
    "DIY & Crafts":          330,
    "Pets & Animals":        320,
    "Sustainability":        380,
    # Cars & Transport
    "Cars & Automotive":     450,
    "Electric Vehicles":     490,
    "Motorcycles":           380,
}

# Supercategory grouping for frontend category browser
NICHE_SUPERCATEGORIES: dict[str, list[str]] = {
    "Finance & Business":  ["Personal Finance","Investing","Cryptocurrency","Entrepreneurship","Real Estate","Tax & Accounting","Insurance","Banking & Credit"],
    "Technology":          ["AI & Machine Learning","Software Development","Cybersecurity","Gaming","Hardware & Reviews","Mobile Apps","Web Development","Data Science"],
    "Education":           ["Online Learning","Language Learning","Science & Nature","History","Mathematics","Philosophy","Psychology","Law & Legal"],
    "Health & Lifestyle":  ["Fitness & Workout","Nutrition & Diet","Mental Health","Yoga & Meditation","Medicine & Health","Parenting"],
    "Creative Arts":       ["Music","Drawing & Art","Photography","Video Production","Graphic Design","Animation","Writing","Podcasting"],
    "Food & Drink":        ["Cooking & Recipes","Baking","Restaurants & Food","Wine & Spirits","Coffee"],
    "Travel & Outdoors":   ["Travel Vlogs","Adventure & Hiking","Luxury Travel","Camping & Survival"],
    "Entertainment":       ["Comedy","Movie Reviews","Anime","Sports","True Crime","News & Politics","Book Reviews"],
    "Fashion & Beauty":    ["Fashion","Beauty & Makeup","Skincare","Luxury & Lifestyle"],
    "Home & Family":       ["Home Improvement","Interior Design","DIY & Crafts","Pets & Animals","Sustainability"],
    "Cars & Transport":    ["Cars & Automotive","Electric Vehicles","Motorcycles"],
}


# ─────────────────────────────────────────────
# SUBSCRIPTION PRICES (USD cents)
# ─────────────────────────────────────────────
# All subscription pricing is in USD. Display converts to local currency.
# PayPal handles actual multi-currency billing — these are for display.

SUBSCRIPTION_PRICES_USD = {
    "pass_monthly":          999,    # $9.99
    "pass_annual":           7999,   # $79.99
    "studio_basic_monthly":  2900,   # $29.00
    "studio_pro_monthly":    5900,   # $59.00
    "studio_agency_monthly": 14900,  # $149.00
    "studio_basic_annual":   23200,  # $232.00 ($19.33/mo)
    "studio_pro_annual":     47200,  # $472.00 ($39.33/mo)
    "studio_agency_annual":  119200, # $1,192.00 ($99.33/mo)
}

# Minimum payout thresholds (USD cents)
MIN_PAYOUT_CREATOR_USD = 5000    # $50.00
MIN_PAYOUT_VIEWER_USD  = 2000    # $20.00
MIN_ADVERTISER_BUDGET_USD = 50000  # $500.00
