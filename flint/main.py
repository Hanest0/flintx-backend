"""
Flint — FastAPI Application
Complete backend entry point.

Run locally:
    uvicorn flint.main:app --reload --port 8000

API docs (auto-generated):
    http://localhost:8000/docs

All environment variables:
    See .env.example in the project root
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from dotenv import load_dotenv

from .database.connection import create_tables
from .auth.routes       import router as auth_router
from .videos.routes     import router as videos_router
from .studio.routes     import router as studio_router
from .wallet.routes     import router as wallet_router
from .moderation.routes import router as moderation_router
from .payments.routes   import router as payments_router
from .advertiser.routes import router as advertiser_router
from .currency.routes   import router as currency_router
from .workspace.routes  import router as workspace_router
from .referral.routes   import router as referral_router
from .livestream.routes  import router as livestream_router
from .payouts.routes     import router as payouts_router
from .affiliate.routes   import router as affiliate_router, seed_platform_products

load_dotenv()

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
ENVIRONMENT  = os.getenv("ENVIRONMENT", "development")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    create_tables()
    from .database.connection import SessionLocal as _SL
    _db = _SL()
    try:
        from .affiliate.routes import seed_platform_products
        seed_platform_products(_db)
    finally:
        _db.close()
    print(f"[FlintX] Database ready — {ENVIRONMENT}")

    # Start background algorithm scorer in production
    if ENVIRONMENT == "production":
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from .algorithm.scorer import score_all_videos
        from .referral.routes import run_monthly_badge_credits
        scheduler = AsyncIOScheduler()
        scheduler.add_job(score_all_videos, "interval", hours=1, id="algo_scorer")
        scheduler.add_job(run_monthly_badge_credits, "cron", day=1, hour=0, minute=0, id="badge_credits")
        scheduler.start()
        print("[FlintX] Algorithm scorer started (hourly)")

    yield

    # Shutdown
    print("[FlintX] Shutting down")


app = FastAPI(
    title       = "FlintXX API",
    version     = "1.0.0",
    description = """
Flint — The platform that pays creators AND viewers.

## Auth
All protected endpoints require `Authorization: Bearer <access_token>` header.
Access tokens expire in 30 minutes. Use `/api/auth/refresh` with the `flintx_refresh`
httpOnly cookie to get a new one.

## Roles
- **viewer** — browse, watch, earn with FlintX Pass
- **creator** — upload videos, use AI tools, earn ad revenue
- **both** — full creator and viewer access
- **advertiser** — create campaigns, view reporting
- **admin** — access Command Centre, moderation queue, payouts

## Revenue flow
1. Advertiser pre-funds budget via PayPal
2. Campaign matches to video by category + safety level
3. Viewer watches ad → impression recorded
4. Creator gets 80% of CPM automatically
5. Pass viewers get 15% of Flint's 20% share as credits
6. Creator/viewer cashes out on 1st or 15th via Wise or PayPal
    """,
    lifespan    = lifespan,
    docs_url    = "/docs" if ENVIRONMENT != "production" else None,
    redoc_url   = None,
)

# ── CORS ──────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = [FRONTEND_URL, "http://localhost:3000"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────
app.include_router(auth_router,        prefix="/api")
app.include_router(videos_router,      prefix="/api")
app.include_router(studio_router,      prefix="/api")
app.include_router(wallet_router,      prefix="/api")
app.include_router(moderation_router,  prefix="/api")
app.include_router(payments_router,    prefix="/api")
app.include_router(advertiser_router,  prefix="/api")
app.include_router(currency_router,    prefix="/api")
app.include_router(workspace_router,   prefix="/api")
app.include_router(referral_router,    prefix="/api")
app.include_router(livestream_router,   prefix="/api")
app.include_router(payouts_router,      prefix="/api")
app.include_router(affiliate_router,    prefix="/api")
app.include_router(affiliate_router,    prefix="")


# ── Health check ──────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status":      "ok",
        "platform":    "FlintX",
        "environment": ENVIRONMENT,
        "version":     "1.0.0",
    }


@app.get("/")
def root():
    return {"message": "FlintX API. Docs at /docs"}
