"""
FlintX Connected Apps — Creator tool integrations
Stores OAuth connection status for Canva, CapCut, Descript, Notion etc.
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
    "canva":         {"name":"Canva",           "category":"Design"},
    "capcut":        {"name":"CapCut",           "category":"Editing"},
    "descript":      {"name":"Descript",         "category":"Editing"},
    "notion":        {"name":"Notion",           "category":"Planning"},
    "google_trends": {"name":"Google Trends",    "category":"Research"},
    "tubebuddy":     {"name":"TubeBuddy",        "category":"SEO"},
    "spotify":       {"name":"Spotify Podcasts", "category":"Distribution"},
    "twitter":       {"name":"X / Twitter",      "category":"Social"},
    "discord":       {"name":"Discord",          "category":"Community"},
}

class ConnectRequest(BaseModel):
    app_id: str
    token:  str = ""

@router.get("/")
def list_apps(user: User = Depends(require_verified), db: Session = Depends(get_db)):
    try:
        connected = json.loads(getattr(user,'connected_apps','{}') or '{}')
    except:
        connected = {}
    return {
        "connected": connected,
        "supported": [{"id":k,"name":v["name"],"category":v["category"],"connected":k in connected} for k,v in SUPPORTED_APPS.items()]
    }

@router.post("/connect")
def connect_app(req: ConnectRequest, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    if req.app_id not in SUPPORTED_APPS:
        raise HTTPException(400, f"Unknown app: {req.app_id}")
    try:
        connected = json.loads(getattr(user,'connected_apps','{}') or '{}')
    except:
        connected = {}
    connected[req.app_id] = {"connected_at": datetime.utcnow().isoformat(), "name": SUPPORTED_APPS[req.app_id]["name"]}
    user.connected_apps = json.dumps(connected)
    db.commit()
    return {"connected": True, "app_id": req.app_id, "app_name": SUPPORTED_APPS[req.app_id]["name"]}

@router.delete("/disconnect/{app_id}")
def disconnect_app(app_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    try:
        connected = json.loads(getattr(user,'connected_apps','{}') or '{}')
    except:
        connected = {}
    connected.pop(app_id, None)
    user.connected_apps = json.dumps(connected)
    db.commit()
    return {"disconnected": True}
