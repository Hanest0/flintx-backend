"""
Flint — Currency API Routes

GET  /api/currencies              — all supported currencies
GET  /api/currencies/rates        — live USD exchange rates
POST /api/currencies/convert      — convert an amount between currencies
GET  /api/currencies/prices       — Flint subscription prices in user's currency
GET  /api/currencies/cpm          — CPM rates by niche in user's currency
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..currency.service import (
    NICHE_SUPERCATEGORIES,
    CurrencyService, NICHE_CPM_USD_CENTS, SUBSCRIPTION_PRICES_USD,
    get_rate, CURRENCIES, FALLBACK_RATES,
)

router = APIRouter(prefix="/currencies", tags=["Currencies"])


@router.get("")
def list_currencies():
    """All supported currencies with symbol, name, and current rate vs USD."""
    rates = {}
    for code in CURRENCIES:
        rates[code] = get_rate(code)

    return {
        "base":       "USD",
        "currencies": [
            {
                "code":    code,
                "name":    meta[0],
                "symbol":  meta[1],
                "decimals": meta[2],
                "rate_from_usd": rates.get(code, 1.0),
                "example": CurrencyService.format(10000, code),   # what $100 looks like
            }
            for code, meta in CURRENCIES.items()
        ]
    }


@router.get("/rates")
def get_rates():
    """Live exchange rates (base: USD). Refreshed every 6 hours."""
    rates = {}
    for code in CURRENCIES:
        rates[code] = get_rate(code)
    return {"base": "USD", "rates": rates}


class ConvertRequest(BaseModel):
    amount:        float
    from_currency: str
    to_currency:   str


@router.post("/convert")
def convert(req: ConvertRequest):
    """Convert an amount between any two supported currencies."""
    from_c = req.from_currency.upper()
    to_c   = req.to_currency.upper()

    if not CurrencyService.is_supported(from_c):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {from_c}")
    if not CurrencyService.is_supported(to_c):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {to_c}")

    # Convert to USD cents first, then to target
    usd_cents  = CurrencyService.to_usd_cents(req.amount, from_c)
    result     = CurrencyService.to_display(usd_cents, to_c)

    return {
        "from":           {"amount": req.amount, "currency": from_c},
        "to":             result,
        "rate":           round(get_rate(to_c) / get_rate(from_c), 6),
    }


@router.get("/prices")
def subscription_prices(currency: str = Query(default="USD", description="3-letter currency code")):
    """
    Flint subscription prices in the user's local currency.
    Pass ?currency=GBP, ?currency=EUR, etc.
    Prices are converted from USD at the current exchange rate.
    """
    currency = currency.upper()
    if not CurrencyService.is_supported(currency):
        currency = "USD"

    return {
        "currency": currency,
        "prices": {
            key: {
                "usd_cents":  usd_cents,
                "display":    CurrencyService.format(usd_cents, currency),
                "amount":     CurrencyService.convert(usd_cents, currency),
            }
            for key, usd_cents in SUBSCRIPTION_PRICES_USD.items()
        },
        "note": "Prices shown are indicative. PayPal charges in USD; your bank may apply a conversion fee.",
    }


@router.get("/cpm")
def niche_cpms(currency: str = Query(default="USD", description="3-letter currency code")):
    """
    Flint's CPM rates by content niche, in the user's local currency.
    CPMs are per 1,000 ad impressions — this is what advertisers pay and creators earn from.
    """
    currency = currency.upper()
    if not CurrencyService.is_supported(currency):
        currency = "USD"

    return {
        "currency": currency,
        "niches": {
            niche: {
                "cpm_display":    CurrencyService.format(cpm_usd, currency),
                "cpm_usd":        f"${cpm_usd/100:.2f}",
                "per_impression": CurrencyService.format(cpm_usd // 1000, currency),
            }
            for niche, cpm_usd in NICHE_CPM_USD_CENTS.items()
        },
        "note": "CPM = cost per 1,000 ad impressions. Creator receives 80%.",
    }


@router.get("/niches")
def get_all_niches(currency: str = Query(default="USD", description="3-letter currency code")):
    """
    All 66 FlintX niches with CPM data, grouped by supercategory.
    Used by creator signup, channel setup, workspace, and revenue predictor.
    """
    currency = currency.upper()
    if not CurrencyService.is_supported(currency):
        currency = "USD"

    result = {}
    for supercategory, niches in NICHE_SUPERCATEGORIES.items():
        result[supercategory] = []
        for niche in niches:
            cpm_usd = NICHE_CPM_USD_CENTS.get(niche, 300)
            result[supercategory].append({
                "name":           niche,
                "cpm_display":    CurrencyService.format(int(cpm_usd * 1.18), currency),
                "cpm_usd":        f"${cpm_usd/100:.2f}",
                "tier":           "premium" if cpm_usd >= 550 else "high" if cpm_usd >= 400 else "standard" if cpm_usd >= 300 else "emerging",
            })

    # Flat list sorted by CPM for pickers that need it
    flat = []
    for supercategory, niches in NICHE_SUPERCATEGORIES.items():
        for niche in niches:
            cpm_usd = NICHE_CPM_USD_CENTS.get(niche, 300)
            flat.append({
                "name":          niche,
                "supercategory": supercategory,
                "cpm_display":   CurrencyService.format(int(cpm_usd * 1.18), currency),
                "cpm_usd_cents": cpm_usd,
                "tier":          "premium" if cpm_usd >= 550 else "high" if cpm_usd >= 400 else "standard" if cpm_usd >= 300 else "emerging",
            })
    flat.sort(key=lambda x: -x["cpm_usd_cents"])

    return {
        "total":          len(flat),
        "currency":       currency,
        "supercategories": result,
        "flat":           flat,
        "note":           "CPM shown includes 1.18× FlintX Pass opted-in viewer premium. Creator earns 80%.",
    }
