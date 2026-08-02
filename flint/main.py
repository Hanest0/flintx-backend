"""
FlintX — FastAPI Backend
Run: uvicorn flint.main:app --host 0.0.0.0 --port $PORT
Docs: http://localhost:8000/docs
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
ENVIRONMENT  = os.getenv("ENVIRONMENT", "development")

# ── Router imports ────────────────────────────────────────────────────
from .database.connection  import create_tables
from .auth.routes          import router as auth_router
from .videos.routes        import router as videos_router
from .studio.routes        import router as studio_router
from .wallet.routes        import router as wallet_router
from .moderation.routes    import router as moderation_router
from .payments.routes      import router as payments_router
from .advertiser.routes    import router as advertiser_router
from .currency.routes      import router as currency_router
from .workspace.routes     import router as workspace_router
from .referral.routes      import router as referral_router
from .livestream.routes    import router as livestream_router
from .payouts.routes       import router as payouts_router
from .affiliate.routes     import router as affiliate_router, seed_platform_products


# ── Lifespan (startup / shutdown) ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    from .database.connection import SessionLocal as _SL
    _db = _SL()
    try:
        seed_platform_products(_db)
    finally:
        _db.close()
    print(f"[FlintX] Ready — {ENVIRONMENT}")

    if ENVIRONMENT == "production":
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from .algorithm.scorer import score_all_videos
            from .referral.routes import run_monthly_badge_credits
            scheduler = AsyncIOScheduler()
            scheduler.add_job(score_all_videos, "interval", hours=1, id="algo_scorer")
            scheduler.add_job(run_monthly_badge_credits, "cron", day=1, hour=0, id="badge_credits")
            scheduler.start()
            print("[FlintX] Scheduler started")
        except Exception as e:
            print(f"[FlintX] Scheduler error (non-fatal): {e}")

    yield
    print("[FlintX] Shutting down")


# ── App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "FlintX API",
    description = "FlintX video platform backend",
    version     = "1.0.0",
    lifespan    = lifespan,
)


# ── CORS ─────────────────────────────────────────────────────────────
# IMPORTANT: specific origins required when frontend sends Authorization headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://vdm-technology.vercel.app",
        "https://vdm-technology-hanest0.vercel.app",
        "https://flintx.tv",
        "https://www.flintx.tv",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────
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
app.include_router(livestream_router,  prefix="/api")
app.include_router(payouts_router,     prefix="/api")
app.include_router(affiliate_router,   prefix="/api")
app.include_router(affiliate_router,   prefix="")    # /go/{code} redirects


# ── Health ────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "platform": "FlintX", "environment": ENVIRONMENT}


@app.get("/")
def root():
    return {"message": "FlintX API — docs at /docs"}
