"""
FlintX Backend
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

ENVIRONMENT  = os.getenv("ENVIRONMENT", "development")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://vdm-technology.vercel.app")

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
from .connected_apps.routes import router as connected_apps_router
from .child_safety.routes   import router as child_safety_router
from .music.routes           import router as music_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    from .database.connection import SessionLocal as _SL
    _db = _SL()
    try:
        seed_platform_products(_db)
    except Exception as e:
        print(f"[FlintX] Seed warning: {e}")
    finally:
        _db.close()
    print(f"[FlintX] Ready — {ENVIRONMENT}")
    if ENVIRONMENT == "production":
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from .algorithm.scorer import score_all_videos
            from .referral.routes import run_monthly_badge_credits
            scheduler = AsyncIOScheduler()
            scheduler.add_job(score_all_videos, "interval", hours=1)
            scheduler.add_job(run_monthly_badge_credits, "cron", day=1, hour=0)
            scheduler.start()
        except Exception as e:
            print(f"[FlintX] Scheduler warning: {e}")
    yield


app = FastAPI(title="FlintX API", version="1.0.0", lifespan=lifespan)


# ── CORS — wildcard, no credentials ──────────────────────────────────
# Simple and always works. Frontend uses Bearer token not cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Explicit OPTIONS handler — catches preflight before routers ───────
@app.options("/{rest_of_path:path}")
async def preflight_handler(request: Request, rest_of_path: str) -> Response:
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age":       "86400",
        },
    )


# ── Routes ────────────────────────────────────────────────────────────
app.include_router(auth_router,           prefix="/api")
app.include_router(videos_router,         prefix="/api")
app.include_router(studio_router,         prefix="/api")
app.include_router(wallet_router,         prefix="/api")
app.include_router(moderation_router,     prefix="/api")
app.include_router(payments_router,       prefix="/api")
app.include_router(advertiser_router,     prefix="/api")
app.include_router(currency_router,       prefix="/api")
app.include_router(workspace_router,      prefix="/api")
app.include_router(referral_router,       prefix="/api")
app.include_router(livestream_router,     prefix="/api")
app.include_router(payouts_router,        prefix="/api")
app.include_router(affiliate_router,      prefix="/api")
app.include_router(affiliate_router,      prefix="")
app.include_router(connected_apps_router, prefix="/api")
app.include_router(child_safety_router,   prefix="/api")
app.include_router(music_router,          prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok", "platform": "FlintX", "environment": ENVIRONMENT}


@app.get("/")
def root():
    return {"message": "FlintX API — /docs"}
