"""
FlintX Connected Apps — Creator tool integrations
9 tools across 7 categories. OAuth where available, direct connection otherwise.
"""
import os, json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from ..database.connection import get_db
from ..database.models import User
from ..auth.routes import require_verified

router = APIRouter(prefix="/studio/apps", tags=["Connected Apps"])

SUPPORTED_APPS = {
    "canva":         {"name":"Canva",           "category":"Design",       "oauth_url":"https://www.canva.com/api/oauth/authorize",    "env":"CANVA_CLIENT_ID"},
    "capcut":        {"name":"CapCut",           "category":"Editing",      "oauth_url":"https://www.capcut.com",                       "env":""},
    "descript":      {"name":"Descript",         "category":"Editing",      "oauth_url":"https://web.descript.com/oauth/authorize",     "env":"DESCRIPT_CLIENT_ID"},
    "notion":        {"name":"Notion",           "category":"Planning",     "oauth_url":"https://api.notion.com/v1/oauth/authorize",    "env":"NOTION_CLIENT_ID"},
    "google_trends": {"name":"Google Trends",    "category":"Research",     "oauth_url":"https://accounts.google.com/o/oauth2/auth",   "env":"GOOGLE_CLIENT_ID"},
    "tubebuddy":     {"name":"TubeBuddy",        "category":"SEO",          "oauth_url":"https://www.tubebuddy.com/oauth",              "env":"TUBEBUDDY_CLIENT_ID"},
    "spotify":       {"name":"Spotify Podcasts", "category":"Distribution", "oauth_url":"https://accounts.spotify.com/authorize",      "env":"SPOTIFY_CLIENT_ID"},
    "twitter":       {"name":"X / Twitter",      "category":"Social",       "oauth_url":"https://twitter.com/i/oauth2/authorize",      "env":"TWITTER_CLIENT_ID"},
    "discord":       {"name":"Discord",          "category":"Community",    "oauth_url":"https://discord.com/api/oauth2/authorize",    "env":"DISCORD_CLIENT_ID"},
}

class ConnectRequest(BaseModel):
    app_id: str
    token:  str = ""

def _get_connected(user):
    try: return json.loads(getattr(user,'connected_apps','{}') or '{}')
    except: return {}

@router.get("/")
def list_apps(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    connected = _get_connected(user)
    return {
        "connected": connected,
        "supported": [
            {
                "id": k, "name": v["name"], "category": v["category"],
                "connected": k in connected,
                "connected_at": connected.get(k, {}).get("connected_at") if k in connected else None,
            }
            for k, v in SUPPORTED_APPS.items()
        ]
    }

@router.post("/connect")
def connect_app(
    req: ConnectRequest,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    if req.app_id not in SUPPORTED_APPS:
        raise HTTPException(400, f"Unknown app: {req.app_id}")
    connected = _get_connected(user)
    connected[req.app_id] = {
        "connected_at": datetime.utcnow().isoformat(),
        "name": SUPPORTED_APPS[req.app_id]["name"],
    }
    user.connected_apps = json.dumps(connected)
    db.commit()
    return {"connected": True, "app_id": req.app_id, "app_name": SUPPORTED_APPS[req.app_id]["name"]}

@router.delete("/disconnect/{app_id}")
def disconnect_app(
    app_id: str,
    user: User = Depends(require_verified),
    db: Session = Depends(get_db),
):
    connected = _get_connected(user)
    connected.pop(app_id, None)
    user.connected_apps = json.dumps(connected)
    db.commit()
    return {"disconnected": True, "app_id": app_id}

@router.get("/oauth-url/{app_id}")
def get_oauth_url(app_id: str, user: User = Depends(require_verified)):
    if app_id not in SUPPORTED_APPS:
        raise HTTPException(404, "App not found")
    app = SUPPORTED_APPS[app_id]
    client_id = os.getenv(app["env"], "") if app["env"] else ""
    redirect   = os.getenv("FRONTEND_URL","https://vdm-technology.vercel.app") + "/oauth/callback"
    return {
        "app_id":       app_id,
        "app_name":     app["name"],
        "oauth_url":    app["oauth_url"],
        "redirect_uri": redirect,
        "client_id":    client_id,
        "ready":        bool(client_id),
        "note":         f"Add {app['env']} to Railway Variables to enable OAuth" if not client_id else None,
    }
